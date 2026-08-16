from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


class GoogleDocsBrowserError(RuntimeError):
    """Raised when the browser cannot prove a safe Suggesting-mode write."""


def browser_profile_path() -> Path:
    override = os.environ.get("LLM_WIKI_GOOGLE_DOCS_BROWSER_PROFILE_DIR")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    state_override = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    state_root = (
        Path(state_override).expanduser().resolve(strict=False)
        if state_override
        else Path.home() / ".local" / "state" / "llm-wiki" / "google-docs-editing"
    )
    return state_root / "browser-profile"


def document_url(document_id: str, tab_id: str = "") -> str:
    url = f"https://docs.google.com/document/d/{urllib.parse.quote(document_id, safe='')}/edit"
    if tab_id:
        url += "?" + urllib.parse.urlencode({"tab": tab_id})
    return url


class BrowserSuggestionDriver:
    """Drive the normal Google Docs UI and fail closed before the first edit."""

    def __init__(self, profile_dir: Path | None = None, timeout_seconds: int = 90) -> None:
        self.profile_dir = (profile_dir or browser_profile_path()).resolve(strict=False)
        self.timeout_ms = max(10, timeout_seconds) * 1000

    @staticmethod
    def _chrome_executable() -> Path:
        raw = os.environ.get(
            "GOOGLE_CHROME_EXECUTABLE",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        try:
            executable = Path(raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise GoogleDocsBrowserError(f"Google Chrome executable is unavailable: {raw}") from exc
        if not executable.is_file():
            raise GoogleDocsBrowserError(f"Google Chrome executable is unavailable: {raw}")
        return executable

    def _human_authenticate(self, document_id: str, timeout_seconds: int) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.profile_dir.chmod(0o700)
        except OSError:
            pass
        command = [
            str(self._chrome_executable()),
            f"--user-data-dir={self.profile_dir}",
            "--lang=en-US",
            "--no-first-run",
            "--disable-sync",
            "--password-store=basic",
            "--use-mock-keychain",
        ]
        if os.environ.get("NONO_ACTIVE_PROFILE") == "custom-codex-google-docs":
            # Chrome's child Seatbelt cannot be nested inside nono's outer
            # Seatbelt. Keep nono as the enforcing sandbox and disable only
            # Chrome's redundant inner sandbox for this dedicated profile.
            command.append("--no-sandbox")
        command.append(document_url(document_id))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise GoogleDocsBrowserError(f"could not open normal Chrome for sign-in: {exc}") from exc
        try:
            return_code = process.wait(timeout=max(60, timeout_seconds))
        except subprocess.TimeoutExpired as exc:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            raise GoogleDocsBrowserError(
                "normal Chrome sign-in timed out; close the dedicated Chrome window after signing in"
            ) from exc
        if return_code != 0:
            raise GoogleDocsBrowserError(
                f"normal Chrome sign-in exited unexpectedly with status {return_code}"
            )

    @contextmanager
    def _context(self) -> Iterator[Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise GoogleDocsBrowserError(
                "Playwright is not installed; run .venv/bin/pip install -r requirements.txt"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.profile_dir.chmod(0o700)
        except OSError:
            pass
        manager = sync_playwright()
        playwright = manager.start()
        context = None
        try:
            launch: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "headless": False,
                "locale": "en-US",
                "args": ["--lang=en-US", "--no-first-run", "--disable-sync"],
            }
            executable = os.environ.get("GOOGLE_CHROME_EXECUTABLE")
            if executable:
                launch["executable_path"] = str(
                    Path(executable).expanduser().resolve(strict=True)
                )
            else:
                launch["channel"] = "chrome"
            context = playwright.chromium.launch_persistent_context(**launch)
            context.set_default_timeout(self.timeout_ms)
            yield context
        except GoogleDocsBrowserError:
            raise
        except Exception as exc:
            raise GoogleDocsBrowserError(f"could not run the dedicated Chrome profile: {exc}") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    # Cleanup must not hide the original launch/editor failure.
                    pass
            try:
                playwright.stop()
            except Exception:
                # A sandbox may also prevent Playwright from terminating a
                # browser that failed during startup. Preserve the actionable
                # browser exception instead of replacing it with cleanup noise.
                pass

    @staticmethod
    def _active_page(context: Any) -> Any:
        if context.pages:
            return context.pages[-1]
        return context.new_page()

    @staticmethod
    def _visible(locator: Any) -> bool:
        try:
            return locator.count() > 0 and locator.first.is_visible()
        except Exception:
            return False

    def _wait_for_editor(self, page: Any, timeout_ms: int | None = None) -> None:
        deadline = time.monotonic() + (timeout_ms or self.timeout_ms) / 1000
        editor = page.locator("#docs-editor")
        mode_switcher = page.locator("#docs-mode-switcher-select")
        while time.monotonic() < deadline:
            if "docs.google.com/document/d/" in page.url and self._visible(editor) and self._visible(mode_switcher):
                return
            page.wait_for_timeout(250)
        raise GoogleDocsBrowserError(
            "the authenticated Google Docs editor did not become ready; "
            "run adapter.py browser-auth from the codex-google-docs profile"
        )

    @staticmethod
    def _locator_text(locator: Any) -> str:
        values: list[str] = []
        for attribute in ("aria-label", "data-tooltip", "title"):
            try:
                value = locator.get_attribute(attribute)
            except Exception:
                value = None
            if value:
                values.append(value)
        try:
            values.append(locator.inner_text())
        except Exception:
            pass
        return " ".join(values).strip().lower()

    def _ensure_suggesting(self, page: Any) -> None:
        switcher = page.locator("#docs-mode-switcher-select").first
        if not self._visible(switcher):
            raise GoogleDocsBrowserError("Google Docs mode selector is unavailable; no edit was sent")
        if "suggest" not in self._locator_text(switcher):
            switcher.click()
            option = page.locator("#docs-mode-switcher-suggesting")
            if not self._visible(option):
                option = page.get_by_role("menuitem", name=re.compile(r"^Suggesting\b", re.I))
            if not self._visible(option):
                page.keyboard.press("Escape")
                raise GoogleDocsBrowserError(
                    "Suggesting mode is not available for this account/document; no edit was sent"
                )
            option.first.click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "suggest" in self._locator_text(switcher):
                return
            page.wait_for_timeout(100)
        raise GoogleDocsBrowserError("Google Docs did not confirm Suggesting mode; no edit was sent")

    def _open_find_replace(self, page: Any) -> Any:
        menu = page.locator("#docs-edit-menu")
        if self._visible(menu):
            menu.first.click()
            item = page.locator("#docs-find-and-replace")
            if not self._visible(item):
                item = page.get_by_role("menuitem", name=re.compile(r"^Find and replace\b", re.I))
            if self._visible(item):
                item.first.click()
        dialog = page.locator(".docs-findandreplacedialog")
        if not self._visible(dialog):
            page.keyboard.press("Escape")
            shortcut = "Meta+Shift+H" if sys.platform == "darwin" else "Control+H"
            page.keyboard.press(shortcut)
        dialog = page.locator(".docs-findandreplacedialog").first
        try:
            dialog.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise GoogleDocsBrowserError("Google Docs Find and replace dialog did not open; no edit was sent") from exc
        return dialog

    def _replace_unique(
        self,
        page: Any,
        edit: dict[str, Any],
        before_click: Callable[[], None],
    ) -> None:
        dialog = self._open_find_replace(page)
        find_input = dialog.locator("input.docs-findandreplacedialog-find-input")
        replace_input = dialog.locator("input.docs-findandreplacedialog-replace-input")
        if find_input.count() == 0 or replace_input.count() == 0:
            textboxes = dialog.get_by_role("textbox")
            if textboxes.count() < 2:
                page.keyboard.press("Escape")
                raise GoogleDocsBrowserError("Google Docs Find and replace fields were unavailable; no edit was sent")
            find_input = textboxes.nth(0)
            replace_input = textboxes.nth(1)

        match_case = dialog.get_by_role("checkbox", name=re.compile(r"^Match case$", re.I))
        if not self._visible(match_case):
            page.keyboard.press("Escape")
            raise GoogleDocsBrowserError(
                "Google Docs Match case control was unavailable; no edit was sent"
            )
        if not match_case.first.is_checked():
            match_case.first.check()
        for label in (r"^Match using regular expressions$", r"^Ignore Latin diacritics$"):
            option = dialog.get_by_role("checkbox", name=re.compile(label, re.I))
            if self._visible(option) and option.first.is_checked():
                option.first.uncheck()

        find_input.fill(edit["find"])
        replace_input.fill(edit["replace"])
        deadline = time.monotonic() + 5
        match_confirmed = False
        while time.monotonic() < deadline:
            try:
                dialog_text = dialog.inner_text()
            except Exception:
                dialog_text = ""
            if re.search(r"\b1\s+of\s+1\b", dialog_text, re.I):
                match_confirmed = True
                break
            page.wait_for_timeout(100)
        if not match_confirmed:
            page.keyboard.press("Escape")
            raise GoogleDocsBrowserError(
                "Google Docs did not confirm exactly one live Find and replace match; no edit was sent"
            )
        replace_button = dialog.get_by_role("button", name=re.compile(r"^Replace$", re.I))
        if replace_button.count() == 0:
            replace_button = dialog.locator(".docs-findandreplacedialog-replace-button")
        if not self._visible(replace_button):
            page.keyboard.press("Escape")
            raise GoogleDocsBrowserError("Google Docs Replace action was unavailable; no edit was sent")
        before_click()
        replace_button.first.click()
        page.wait_for_timeout(400)
        page.keyboard.press("Escape")

    def authenticate(self, document_id: str, timeout_seconds: int = 900) -> None:
        print(
            "Normal Chrome opened for Google sign-in. Sign in, confirm the document is editable, "
            "then close the dedicated Chrome window to continue verification.",
            flush=True,
        )
        self._human_authenticate(document_id, timeout_seconds)
        with self._context() as context:
            page = self._active_page(context)
            page.goto(document_url(document_id), wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._wait_for_editor(page)

    def apply(
        self,
        document_id: str,
        edits: list[dict[str, Any]],
        before_mutation: Callable[[], None],
    ) -> dict[str, Any]:
        if not edits:
            raise GoogleDocsBrowserError("approved plan contains no browser edits")
        if any(edit.get("occurrence") is not None for edit in edits):
            raise GoogleDocsBrowserError(
                "browser Suggesting mode requires each find text to be unique in the document"
            )
        with self._context() as context:
            page = self._active_page(context)
            current_tab: str | None = None
            mutation_started = False
            for edit in edits:
                tab_id = str(edit.get("tab_id", ""))
                if tab_id != current_tab:
                    page.goto(
                        document_url(document_id, tab_id),
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    self._wait_for_editor(page)
                    current_tab = tab_id
                self._ensure_suggesting(page)
                def mark_first_mutation() -> None:
                    nonlocal mutation_started
                    if not mutation_started:
                        before_mutation()
                        mutation_started = True

                self._replace_unique(page, edit, mark_first_mutation)
                self._ensure_suggesting(page)
            page.wait_for_timeout(2_000)
        return {
            "transport": "browser-suggesting-ui",
            "mode_verified": True,
            "edit_count": len(edits),
        }
