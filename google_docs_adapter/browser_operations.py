from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .browser_executor import (
    MAX_BROWSER_EDITS,
    assert_expected_document_url,
    compile_inspection_program,
    compile_source_preflight_program,
    compile_suggestion_presence_program,
    compile_suggestion_program,
    document_projection,
    document_projection_sha256,
    document_text_fragments,
    document_id_from_collaboration,
    document_id_from_expected_url,
    snapshot_sha256,
    snapshot_text_fragments,
)
from .storage import (
    canonical_json_bytes,
    load_json,
    private_artifact,
    sha256_bytes,
    sha256_file,
    write_private_json,
)

COLLABORATION_RESOURCE = "browser-collaboration:active-tab"
INSPECTION_SCHEMA = "google-docs-browser-inspection/v1"
PLAN_SCHEMA = "google-docs-browser-suggestion-plan/v1"
EDIT_SPEC_SCHEMA = "google-docs-edit-spec/v1"
BROWSER_TRANSIENT_ATTEMPTS = 5
BROWSER_TRANSIENT_RETRY_SECONDS = 2.0
TRANSIENT_BROWSER_ERRORS = {"cdp-command-failed", "cdp-command-timeout"}


class BrowserClient(Protocol):
    def collaborations(self) -> list[dict[str, str]]: ...

    def collaboration_for_url(self, raw_url: str) -> dict[str, str] | None: ...

    def run(
        self,
        program: dict[str, Any],
        *,
        private_values: dict[str, str] | None = None,
        before_mutation: Any | None = None,
    ) -> dict[str, Any]: ...


def _default_browser() -> BrowserClient:
    def load_client() -> tuple[Any, str | None]:
        from browser_executor.client import BrowserExecutorClient

        root = Path(sys.modules["browser_executor.client"].__file__).resolve().parents[1]
        manifest_path = root / ".llm-wiki-adapter.json"
        version = None
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("id") == "browser-execution":
                    version = manifest.get("version")
            except (OSError, json.JSONDecodeError):
                pass
        if version is None:
            try:
                version = distribution_version("llm-wiki-chrome")
            except PackageNotFoundError:
                pass
        return BrowserExecutorClient, version

    try:
        browser_client, version = load_client()
    except ImportError:
        browser_client, version = None, None

    if version is None:
        command = shutil.which("llm-wiki-chrome")
        client_root = None
        if command:
            for subcommand, parent_offset in (("client-path", 0), ("extension-path", 1)):
                try:
                    completed = subprocess.run(
                        [command, subcommand],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if completed.returncode != 0:
                    continue
                candidate = Path(completed.stdout.strip()).expanduser().resolve(strict=False)
                if parent_offset:
                    candidate = candidate.parent
                if (candidate / "browser_executor" / "client.py").is_file():
                    client_root = candidate
                    break
        if client_root is not None:
            sys.path.insert(0, str(client_root))
            for name in list(sys.modules):
                if name == "browser_executor" or name.startswith("browser_executor."):
                    del sys.modules[name]
            importlib.invalidate_caches()
            try:
                browser_client, version = load_client()
            except ImportError:
                browser_client, version = None, None

    if browser_client is None or version is None:
        raise RuntimeError(
            "llm-wiki-chrome 0.1.1 or later is not installed; install the matching native companion"
        )
    try:
        version_parts = tuple(int(part) for part in str(version).split("."))
    except ValueError as exc:
        raise RuntimeError("the shared browser executor version is invalid") from exc
    if version_parts < (0, 1, 1):
        raise RuntimeError("llm-wiki-chrome 0.1.1 or later is required")
    return browser_client()


def _response(operation: str, status: str, run_id: str, **values: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol": "llm-wiki-adapter/v1",
        "adapter_id": "google-docs-editing",
        "adapter_version": __version__,
        "operation": operation,
        "status": status,
        "run_id": run_id,
        "summary": {},
        "artifacts": [],
    }
    response.update(values)
    return response


def _error(operation: str, message: str, run_id: str = "failed") -> dict[str, Any]:
    value = _response(operation, "error", run_id)
    value["errors"] = [message]
    return value


def _collaboration_resource(request: dict[str, Any]) -> str:
    value = request.get("arguments", {}).get("collaboration_resource")
    if value != COLLABORATION_RESOURCE:
        raise ValueError(
            f"collaboration_resource must be the registered {COLLABORATION_RESOURCE} capability"
        )
    return value


def _live_collaboration(
    request: dict[str, Any],
    browser: BrowserClient,
) -> tuple[dict[str, str], str]:
    _collaboration_resource(request)
    expected = request.get("arguments", {}).get("expected_document_url")
    expected_document_id = document_id_from_expected_url(expected)
    matches: list[dict[str, str]] = []
    collaborations: list[dict[str, str]] = []
    for attempt in range(21):
        collaborations = browser.collaborations()
        matches = []
        for candidate in collaborations:
            try:
                if document_id_from_collaboration(candidate) == expected_document_id:
                    matches.append(candidate)
            except ValueError:
                continue
        if matches or attempt == 20:
            break
        time.sleep(0.1)
    if not collaborations:
        raise RuntimeError(
            "no page is exposed; open the requested Google Doc and click LLM Wiki for Chrome"
        )
    if not matches:
        raise RuntimeError("none of the explicitly shared tabs is the requested Google Doc")
    if len(matches) > 1:
        raise RuntimeError("more than one explicitly shared tab matches the requested Google Doc")
    collaboration = matches[0]
    document_id = assert_expected_document_url(expected, collaboration)
    return collaboration, document_id


def _browser_error_detail(result: Any) -> str:
    if not isinstance(result, dict):
        return "invalid executor result"
    error = result.get("error")
    detail = error if isinstance(error, str) and error else "invalid executor result"
    public = result.get("public")
    action_count = public.get("action_count") if isinstance(public, dict) else None
    if type(action_count) is int and action_count >= 0:
        detail += f" at bounded action {action_count}"
    return detail


def _is_transient_browser_error(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") != "ok"
        and result.get("error") in TRANSIENT_BROWSER_ERRORS
    )


def _run_inspection(
    browser: BrowserClient,
    collaboration: dict[str, str],
    document_id: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    program = compile_inspection_program(document_id, collaboration)
    result: Any = None
    projection_error: ValueError | None = None
    for attempt in range(BROWSER_TRANSIENT_ATTEMPTS):
        result = browser.run(program)
        if isinstance(result, dict) and result.get("status") == "ok":
            private = result.get("private")
            snapshot = private.get("docs.ax") if isinstance(private, dict) else None
            if not isinstance(snapshot, list):
                raise RuntimeError(
                    "browser inspection did not return the private accessibility projection"
                )
            try:
                revision = document_projection_sha256(snapshot)
            except ValueError as exc:
                projection_error = exc
                if attempt + 1 >= BROWSER_TRANSIENT_ATTEMPTS:
                    raise RuntimeError(
                        "browser inspection did not expose stable document content after bounded retries"
                    ) from exc
                time.sleep(BROWSER_TRANSIENT_RETRY_SECONDS)
                continue
            return snapshot, revision, snapshot_text_fragments(snapshot)
        if (
            not _is_transient_browser_error(result)
            or attempt + 1 >= BROWSER_TRANSIENT_ATTEMPTS
        ):
            raise RuntimeError(f"browser inspection failed: {_browser_error_detail(result)}")
        time.sleep(BROWSER_TRANSIENT_RETRY_SECONDS)
    if projection_error is not None:
        raise RuntimeError(
            "browser inspection did not expose stable document content after bounded retries"
        ) from projection_error
    raise RuntimeError("browser inspection failed without a bounded executor result")


def inspect_document(
    request: dict[str, Any],
    browser: BrowserClient,
) -> dict[str, Any]:
    collaboration, document_id = _live_collaboration(request, browser)
    snapshot, revision, fragments = _run_inspection(browser, collaboration, document_id)
    inspection = {
        "schema": INSPECTION_SCHEMA,
        "collaboration_resource": COLLABORATION_RESOURCE,
        "target_url": collaboration["url"],
        "document_id": document_id,
        "revision_id": revision,
        "text_fragments": fragments,
        "document_text_fragments": document_text_fragments(snapshot),
        "accessibility_projection": snapshot,
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "inspection.json"
    write_private_json(output_path, inspection)
    run_id = sha256_bytes(canonical_json_bytes({
        "resource": COLLABORATION_RESOURCE,
        "target_url": collaboration["url"],
        "revision": revision,
    }))[:24]
    return _response(
        "inspect",
        "ok",
        run_id,
        summary={
            "revision_sha256": revision,
            "private_text_fragment_count": len(fragments),
            "private_ax_node_count": len(snapshot),
            "private_document_text_fragment_count": len(document_text_fragments(snapshot)),
            "raw_ax_projection_sha256": snapshot_sha256(snapshot),
            "oauth_used": False,
        },
        artifacts=[private_artifact(output_path, "google-docs-browser-inspection")],
    )


def _validate_edit_spec(value: dict[str, Any]) -> list[dict[str, str]]:
    if value.get("schema") != EDIT_SPEC_SCHEMA:
        raise ValueError(f"edit spec schema must be {EDIT_SPEC_SCHEMA}")
    edits = value.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BROWSER_EDITS:
        raise ValueError(f"edit spec must contain 1-{MAX_BROWSER_EDITS} edits")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("browser edit entries must be objects")
        if set(edit) == {"append"}:
            append = edit.get("append")
            if not isinstance(append, str) or not append:
                raise ValueError("append edits require non-empty text")
            normalized.append({"append": append})
            continue
        if set(edit) != {"find", "replace"}:
            raise ValueError("browser edit entries accept an exact replacement or append")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find:
            raise ValueError("every edit requires non-empty find text")
        if not isinstance(replace, str) or replace == find:
            raise ValueError("every edit requires different string replace text")
        if find in seen:
            raise ValueError("duplicate find text is not allowed in one suggestion plan")
        if any(find in prior or prior in find for prior in seen):
            raise ValueError("overlapping find text is not allowed in one suggestion plan")
        seen.add(find)
        normalized.append({"find": find, "replace": replace})
    append_count = sum("append" in edit for edit in normalized)
    if append_count and (append_count != 1 or len(normalized) != 1):
        raise ValueError("append plans must contain exactly one append edit")
    return normalized


def _normalized_observed_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).replace("\u00a0", " ")).strip()


def _snapshot_contains_text(snapshot: list[dict[str, Any]], text: str) -> bool:
    expected = _normalized_observed_text(text)
    if not expected:
        return False
    values: list[str] = []
    for row in snapshot:
        for key in ("name", "value"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                normalized = _normalized_observed_text(value)
                if expected in normalized:
                    return True
                values.append(value)

    # Docs often exposes one logical suggestion as consecutive InlineTextBox
    # fragments. Preserve order and try both natural-space and exact adjacency
    # without weakening the comparison to unordered token matching.
    maximum = min(len(values), 96)
    for start in range(len(values)):
        spaced = ""
        adjacent = ""
        for value in values[start:start + maximum]:
            spaced = f"{spaced} {value}" if spaced else value
            adjacent += value
            if expected in _normalized_observed_text(spaced):
                return True
            if expected in _normalized_observed_text(adjacent):
                return True
            if len(spaced) > len(text) * 3 + 2048:
                break
    return False


def _planned_text(edit: dict[str, Any]) -> str:
    value = edit.get("replace", edit.get("append"))
    if not isinstance(value, str) or not value:
        raise ValueError("suggestion plan contains an invalid edit")
    return value


def _probe_unique_sources(
    browser: BrowserClient,
    collaboration: dict[str, str],
    document_id: str,
    edit_spec_sha256: str,
    edits: list[dict[str, str]],
) -> None:
    program_sha256 = sha256_bytes(canonical_json_bytes({
        "purpose": "google-docs-source-preflight",
        "collaboration_id": collaboration["collaboration_id"],
        "target_url": collaboration["url"],
        "edit_spec_sha256": edit_spec_sha256,
    }))
    program, private_values = compile_source_preflight_program(
        document_id,
        program_sha256,
        edits,
        collaboration,
    )
    result: Any = None
    for attempt in range(BROWSER_TRANSIENT_ATTEMPTS):
        crossed_boundary = False

        def mark_checked() -> None:
            nonlocal crossed_boundary
            crossed_boundary = True

        result = browser.run(
            program,
            private_values=private_values,
            before_mutation=mark_checked,
        )
        if (
            crossed_boundary
            or not _is_transient_browser_error(result)
            or attempt + 1 >= BROWSER_TRANSIENT_ATTEMPTS
        ):
            break
        time.sleep(BROWSER_TRANSIENT_RETRY_SECONDS)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError(
            "exact Docs Find source preflight did not observe one unique match for every edit: "
            + _browser_error_detail(result)
        )


def plan_suggestions(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    collaboration, document_id = _live_collaboration(request, browser)
    edit_spec_path = Path(request["arguments"]["edit_spec"]).resolve(strict=True)
    edit_spec = load_json(edit_spec_path, "edit specification")
    edits = _validate_edit_spec(edit_spec)
    snapshot, revision, _fragments = _run_inspection(browser, collaboration, document_id)
    stable_projection = document_projection(snapshot)
    missing = [
        index + 1
        for index, edit in enumerate(edits)
        if "find" in edit and not _snapshot_contains_text(stable_projection, edit["find"])
    ]
    if missing:
        source_edits = [edit for edit in edits if "find" in edit]
        _probe_unique_sources(
            browser,
            collaboration,
            document_id,
            sha256_file(edit_spec_path),
            source_edits,
        )
        # The Find probe intentionally changes only ephemeral browser UI. Read
        # the document again so planning and apply compare revisions from the
        # same post-probe state rather than hashing the stale Find announcement.
        snapshot, revision, _fragments = _run_inspection(
            browser, collaboration, document_id,
        )
    # A newly created suggestion can leave a transient card or live-region
    # layer that the first bounded Escape dismisses while it is being scanned.
    # Read once more and bind the plan to the settled post-dismissal projection
    # that apply will observe, rather than to that one-pass transition state.
    snapshot, revision, _fragments = _run_inspection(
        browser, collaboration, document_id,
    )
    plan = {
        "schema": PLAN_SCHEMA,
        "write_transport": "shared-browser-executor-suggesting-ui",
        "collaboration_resource": COLLABORATION_RESOURCE,
        "target": {
            "url": collaboration["url"],
            "collaboration_id": collaboration["collaboration_id"],
            "document_id": document_id,
        },
        "revision_id": revision,
        "edit_spec_sha256": sha256_file(edit_spec_path),
        "created_at_unix": int(time.time()),
        "edits": edits,
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "plan.json"
    write_private_json(output_path, plan)
    plan_sha256 = sha256_file(output_path)
    return _response(
        "plan",
        "ok",
        plan_sha256[:24],
        summary={
            "edit_count": len(edits),
            "plan_sha256": plan_sha256,
            "expected_revision_sha256": revision,
            "tracked_changes": True,
            "oauth_used": False,
        },
        artifacts=[private_artifact(output_path, "approved-remote-write-plan")],
    )


def _journal_path(idempotency_key: str, plan_path: Path) -> Path:
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("remote_write idempotency_key must contain 1-256 characters")
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    root = (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else plan_path.parent / ".google-docs-state"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root / "browser-journal" / f"{sha256_bytes(idempotency_key.encode('utf-8'))}.json"


def _validate_plan_request(request: dict[str, Any]) -> tuple[dict[str, Any], Path, str, str, str]:
    resource = _collaboration_resource(request)
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    plan = load_json(plan_path, "suggestion plan")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA}")
    if plan.get("write_transport") != "shared-browser-executor-suggesting-ui":
        raise ValueError("plan does not authorize the shared browser Suggesting transport")
    if plan.get("collaboration_resource") != resource:
        raise ValueError("plan collaboration_resource does not match the request")
    remote_write = request.get("remote_write")
    if not isinstance(remote_write, dict):
        raise ValueError("apply requires remote_write governance")
    plan_sha256 = sha256_file(plan_path)
    if remote_write.get("plan_sha256") != plan_sha256:
        raise ValueError("remote_write plan_sha256 does not match plan")
    expected_revision = str(remote_write.get("expected_revision", ""))
    if expected_revision != str(plan.get("revision_id", "")):
        raise ValueError("remote_write expected_revision does not match plan revision")
    idempotency_key = str(remote_write.get("idempotency_key", ""))
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("remote_write idempotency_key must contain 1-256 characters")
    return plan, plan_path, plan_sha256, expected_revision, idempotency_key


def _same_plan_collaboration(plan: dict[str, Any], collaboration: dict[str, str]) -> str:
    target = plan.get("target")
    if not isinstance(target, dict):
        raise ValueError("plan has no exact collaboration target")
    document_id = document_id_from_collaboration(collaboration)
    if (
        target.get("document_id") != document_id
        or target.get("url") != collaboration.get("url")
        or target.get("collaboration_id") != collaboration.get("collaboration_id")
    ):
        raise RuntimeError(
            "the active collaboration changed after planning; click the extension and create a new plan"
        )
    return document_id


def _same_recovery_document(plan: dict[str, Any], collaboration: dict[str, str]) -> str:
    """Accept a fresh explicit grant for read-only recovery of the same exact Doc."""
    target = plan.get("target")
    if not isinstance(target, dict):
        raise ValueError("plan has no exact collaboration target")
    document_id = document_id_from_collaboration(collaboration)
    if (
        target.get("document_id") != document_id
        or target.get("url") != collaboration.get("url")
    ):
        raise RuntimeError(
            "the planned Google Doc changed before recovery; expose the exact document again"
        )
    return document_id


def _verify_after_snapshot(
    plan: dict[str, Any],
    snapshot: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    after_revision = document_projection_sha256(snapshot)
    if after_revision == plan["revision_id"]:
        raise RuntimeError("browser read-back did not observe a changed document projection")
    missing = [
        index + 1
        for index, edit in enumerate(plan["edits"])
        if not _snapshot_contains_text(snapshot, _planned_text(edit))
    ]
    if missing:
        raise RuntimeError(
            "browser read-back did not observe replacement text for edit(s): "
            + ", ".join(map(str, missing))
        )
    return after_revision, {
        "status": "verified",
        "write_transport": "shared-browser-executor-suggesting-ui",
        "suggesting_mode_asserted_before_and_after": True,
        "unique_find_preconditions_asserted_before_mutation": all(
            "find" in edit for edit in plan["edits"]
        ),
        "append_position_precondition_asserted_before_mutation": all(
            "append" in edit for edit in plan["edits"]
        ),
        "planned_text_observed_after_mutation": True,
        "replacement_text_observed_after_mutation": all(
            "replace" in edit for edit in plan["edits"]
        ),
        "suggestion_count": len(plan["edits"]),
        "before_projection_sha256": plan["revision_id"],
        "after_projection_sha256": after_revision,
        "target_url_sha256": sha256_bytes(plan["target"]["url"].encode("utf-8")),
    }


def _is_append_plan(plan: dict[str, Any]) -> bool:
    edits = plan.get("edits")
    return (
        isinstance(edits, list)
        and len(edits) == 1
        and isinstance(edits[0], dict)
        and set(edits[0]) == {"append"}
    )


def _probe_planned_text_presence(
    browser: BrowserClient,
    plan: dict[str, Any],
    plan_sha256: str,
    collaboration: dict[str, str],
    document_id: str,
) -> None:
    program, private_values = compile_suggestion_presence_program(
        document_id,
        plan_sha256,
        list(plan["edits"]),
        collaboration,
    )
    result: Any = None
    for attempt in range(BROWSER_TRANSIENT_ATTEMPTS):
        # The probe types only into Docs' Find dialog. Its protocol boundary is
        # deliberately last, with no document-changing action after it.
        result = browser.run(
            program,
            private_values=private_values,
            before_mutation=lambda: None,
        )
        if not _is_transient_browser_error(result) or attempt + 1 >= BROWSER_TRANSIENT_ATTEMPTS:
            break
        time.sleep(BROWSER_TRANSIENT_RETRY_SECONDS)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError(
            "exact Docs Find probe did not observe every planned suggestion: "
            + _browser_error_detail(result)
        )


def _presence_probe_verification(
    plan: dict[str, Any],
    before_revision: str,
    after_revision: str,
) -> dict[str, Any]:
    edits = list(plan["edits"])
    return {
        "status": "verified",
        "write_transport": "shared-browser-executor-suggesting-ui",
        "verification_method": "exact-docs-find-probe",
        "suggesting_mode_asserted_before_and_after": True,
        "unique_find_preconditions_asserted_before_mutation": all(
            "find" in edit for edit in edits
        ),
        "append_position_precondition_asserted_before_mutation": all(
            "append" in edit for edit in edits
        ),
        "planned_text_observed_after_mutation": True,
        "replacement_text_observed_after_mutation": all(
            "replace" in edit for edit in edits
        ),
        "stable_content_projection_changed": after_revision != before_revision,
        "suggestion_count": len(edits),
        "before_projection_sha256": before_revision,
        "after_projection_sha256": after_revision,
    }


def _successful_apply_response(
    resource: str,
    plan_sha256: str,
    idempotency_key: str,
    before_revision: str,
    after_revision: str,
    verification: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    return _response(
        "apply",
        "ok",
        run_id,
        summary={
            "tracked_changes": True,
            "suggestion_count": verification["suggestion_count"],
            "verified": True,
            "oauth_used": False,
        },
        remote_receipt={
            "status": "verified",
            "resources": [resource],
            "plan_sha256": plan_sha256,
            "idempotency_key": idempotency_key,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "verification": verification,
        },
    )


def apply_suggestions(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    plan, plan_path, plan_sha256, expected_revision, idempotency_key = _validate_plan_request(request)
    resource = COLLABORATION_RESOURCE
    journal_path = _journal_path(idempotency_key, plan_path)
    run_id = sha256_bytes(idempotency_key.encode("utf-8"))[:24]
    if journal_path.is_file():
        stored = load_json(journal_path, "idempotency journal")
        if stored.get("plan_sha256") != plan_sha256 or stored.get("resource") != resource:
            raise RuntimeError("idempotency key was already used for a different remote write")
        response = stored.get("response")
        if isinstance(response, dict):
            return response
        raise RuntimeError(
            "a prior browser write crossed the mutation boundary without a verified receipt; refusing a duplicate"
        )

    target = plan.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("url"), str):
        raise ValueError("plan has no exact collaboration target")
    collaboration = browser.collaboration_for_url(target["url"])
    if collaboration is None:
        raise RuntimeError("the planned Google Doc is no longer exposed")
    document_id = _same_plan_collaboration(plan, collaboration)
    _before_snapshot, live_revision, _fragments = _run_inspection(
        browser, collaboration, document_id,
    )
    if live_revision != expected_revision:
        raise RuntimeError("the browser-visible document changed after planning; no edit was sent")

    pending_written = False

    def mark_pending() -> None:
        nonlocal pending_written
        write_private_json(journal_path, {
            "status": "pending",
            "plan_sha256": plan_sha256,
            "resource": resource,
            "expected_revision": expected_revision,
            "idempotency_key_sha256": sha256_bytes(idempotency_key.encode("utf-8")),
        })
        pending_written = True

    program, private_values = compile_suggestion_program(
        document_id,
        plan_sha256,
        list(plan["edits"]),
        collaboration,
    )
    result: Any = None
    for attempt in range(BROWSER_TRANSIENT_ATTEMPTS):
        result = browser.run(
            program,
            private_values=private_values,
            before_mutation=mark_pending,
        )
        if pending_written or not _is_transient_browser_error(result):
            break
        if attempt + 1 >= BROWSER_TRANSIENT_ATTEMPTS:
            break
        time.sleep(BROWSER_TRANSIENT_RETRY_SECONDS)
    if not pending_written:
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise RuntimeError(
                "browser suggestion preflight failed before authorization; no edit was sent: "
                + _browser_error_detail(result)
            )
        raise RuntimeError("browser executor returned without crossing the governed mutation boundary")
    result_ok = isinstance(result, dict) and result.get("status") == "ok"
    private = result.get("private") if isinstance(result, dict) else None
    after_snapshot = private.get("docs.after-ax") if isinstance(private, dict) else None
    readback_error: RuntimeError | None = None
    try:
        if not result_ok:
            raise RuntimeError(
                "browser suggestion write failed after authorization: "
                + _browser_error_detail(result)
            )
        if not isinstance(after_snapshot, list):
            raise RuntimeError("browser executor did not return the private read-back projection")
        after_revision, verification = _verify_after_snapshot(plan, after_snapshot)
    except (RuntimeError, ValueError) as initial_readback_error:
        try:
            after_snapshot, _fresh_revision, _fragments = _run_inspection(
                browser, collaboration, document_id,
            )
            after_revision, verification = _verify_after_snapshot(plan, after_snapshot)
        except (RuntimeError, ValueError) as fresh_readback_error:
            readback_error = RuntimeError(
                f"{initial_readback_error}; fresh browser read-back failed: "
                f"{fresh_readback_error}"
            )
    if readback_error is not None:
        try:
            _probe_planned_text_presence(
                browser, plan, plan_sha256, collaboration, document_id,
            )
        except RuntimeError as probe_error:
            raise RuntimeError(f"{readback_error}; {probe_error}") from probe_error
        after_snapshot, after_revision, _fragments = _run_inspection(
            browser, collaboration, document_id,
        )
        verification = _presence_probe_verification(
            plan, expected_revision, after_revision
        )
        verification["target_url_sha256"] = sha256_bytes(
            plan["target"]["url"].encode("utf-8")
        )
    response = _successful_apply_response(
        resource,
        plan_sha256,
        idempotency_key,
        expected_revision,
        after_revision,
        verification,
        run_id,
    )
    write_private_json(journal_path, {
        "plan_sha256": plan_sha256,
        "resource": resource,
        "response": response,
    })
    return response


def recover_suggestions(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    """Resolve a pending journal by proving exact planned text without reapplying it."""
    plan, plan_path, plan_sha256, expected_revision, idempotency_key = _validate_plan_request(request)
    resource = COLLABORATION_RESOURCE
    journal_path = _journal_path(idempotency_key, plan_path)
    if not journal_path.is_file():
        raise RuntimeError("no pending browser write exists for this idempotency key")
    stored = load_json(journal_path, "idempotency journal")
    if stored.get("plan_sha256") != plan_sha256 or stored.get("resource") != resource:
        raise RuntimeError("idempotency key was already used for a different remote write")
    stored_response = stored.get("response")
    if isinstance(stored_response, dict):
        recovered = dict(stored_response)
        recovered["operation"] = "recover"
        return recovered
    if stored.get("status") != "pending":
        raise RuntimeError("the browser write journal is not recoverable")

    target = plan.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("url"), str):
        raise ValueError("plan has no exact collaboration target")
    collaboration = browser.collaboration_for_url(target["url"])
    if collaboration is None:
        raise RuntimeError("the planned Google Doc is no longer exposed")
    # Recovery never reapplies the edit. A browser-extension reload creates a new
    # collaboration ID, so accept that fresh explicit grant only when its exact
    # URL and parsed document ID still match the immutable plan target.
    document_id = _same_recovery_document(plan, collaboration)
    snapshot, live_revision, _fragments = _run_inspection(
        browser, collaboration, document_id,
    )
    planned_texts = [_planned_text(edit) for edit in plan["edits"]]
    ax_text_observed = all(
        _snapshot_contains_text(snapshot, text) for text in planned_texts
    )
    if not ax_text_observed:
        _probe_planned_text_presence(
            browser, plan, plan_sha256, collaboration, document_id
        )
    verification = _presence_probe_verification(
        plan, expected_revision, live_revision
    )
    verification["verification_method"] = (
        "browser-ax-exact-text" if ax_text_observed else "exact-docs-find-probe"
    )
    verification["target_url_sha256"] = sha256_bytes(target["url"].encode("utf-8"))
    run_id = sha256_bytes(idempotency_key.encode("utf-8"))[:24]
    apply_response = _successful_apply_response(
        resource,
        plan_sha256,
        idempotency_key,
        expected_revision,
        live_revision,
        verification,
        run_id,
    )
    write_private_json(journal_path, {
        "plan_sha256": plan_sha256,
        "resource": resource,
        "response": apply_response,
    })
    recovered = dict(apply_response)
    recovered["operation"] = "recover"
    return recovered


def verify_receipt(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    resource = _collaboration_resource(request)
    receipt_path = Path(request["arguments"]["receipt"]).resolve(strict=True)
    receipt_document = load_json(receipt_path, "remote receipt")
    remote_receipt = receipt_document.get("remote_receipt")
    if not isinstance(remote_receipt, dict) or remote_receipt.get("resources") != [resource]:
        raise ValueError("receipt does not belong to the active-tab collaboration capability")
    previous = remote_receipt.get("verification")
    if not isinstance(previous, dict):
        raise ValueError("receipt has no browser verification record")
    matches = [
        value for value in browser.collaborations()
        if sha256_bytes(value["url"].encode("utf-8")) == previous.get("target_url_sha256")
    ]
    if not matches:
        raise RuntimeError("the receipted Google Doc is not explicitly shared for verification")
    if len(matches) > 1:
        raise RuntimeError("the receipted Google Doc has an ambiguous collaboration grant")
    collaboration = matches[0]
    document_id = document_id_from_collaboration(collaboration)
    snapshot, revision, _fragments = _run_inspection(browser, collaboration, document_id)
    target_matches = (
        sha256_bytes(collaboration["url"].encode("utf-8"))
        == previous.get("target_url_sha256")
    )
    receipt_projection_matches = revision == remote_receipt.get("after_revision")
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    if sha256_file(plan_path) != remote_receipt.get("plan_sha256"):
        raise ValueError("verification plan does not match the remote receipt")
    plan = load_json(plan_path, "suggestion plan")
    target = plan.get("target")
    if not isinstance(target, dict) or target.get("document_id") != document_id:
        raise ValueError("verification plan does not belong to the exposed Google Doc")
    planned_text_matches = all(
        _snapshot_contains_text(snapshot, _planned_text(edit))
        for edit in plan.get("edits", [])
    )
    if not planned_text_matches:
        _probe_planned_text_presence(
            browser,
            plan,
            str(remote_receipt.get("plan_sha256", "")),
            collaboration,
            document_id,
        )
        planned_text_matches = True
    verified = target_matches and planned_text_matches
    report = {
        "schema": "google-docs-browser-suggestion-verification/v1",
        "status": "verified" if verified else "drifted",
        "collaboration_resource": resource,
        "receipt_sha256": sha256_file(receipt_path),
        "revision_id": revision,
        "target_matches": target_matches,
        "receipt_projection_matches": receipt_projection_matches,
        "planned_text_matches": planned_text_matches,
        "private_ax_node_count": len(snapshot),
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "verification.json"
    write_private_json(output_path, report)
    response = _response(
        "verify",
        "ok" if verified else "error",
        report["receipt_sha256"][:24],
        summary={"verified": verified, "oauth_used": False},
        artifacts=[private_artifact(output_path, "google-docs-browser-suggestion-verification")],
    )
    if not verified:
        response["errors"] = ["browser receipt no longer matches the exposed document projection"]
    return response


def self_test() -> dict[str, Any]:
    return _response(
        "self-test",
        "ok",
        "synthetic-transport-self-test",
        summary={
            "tracked_changes_required": True,
            "preferred_write_transport": "google-docs-api-suggest-v1",
            "legacy_write_transport": "shared-browser-executor-suggesting-ui",
            "active_tab_collaboration": True,
            "explicit_multi_tab_workspace": True,
            "exact_document_selection": True,
            "bounded_document_scan": True,
            "content_only_revision": True,
            "oauth_used": False,
            "docs_api_native_suggestions_ga": True,
            "docs_api_write_mode": "SUGGEST",
            "desktop_oauth_pkce": True,
            "stored_oauth_refresh": True,
            "workspace_addon_per_file_grant": True,
        },
    )


def execute(
    request: dict[str, Any],
    browser: BrowserClient | None = None,
    api_client: Any | None = None,
) -> dict[str, Any]:
    operation = request.get("operation")
    try:
        if operation == "self-test":
            return self_test()
        if isinstance(operation, str) and operation.startswith("api-"):
            from .api_operations import execute_api

            return execute_api(request, api_client)
        active_browser = browser or _default_browser()
        if operation == "inspect":
            return inspect_document(request, active_browser)
        if operation == "plan":
            return plan_suggestions(request, active_browser)
        if operation == "apply":
            return apply_suggestions(request, active_browser)
        if operation == "recover":
            return recover_suggestions(request, active_browser)
        if operation == "verify":
            return verify_receipt(request, active_browser)
        return _error(str(operation), "unsupported operation")
    except Exception as exc:
        return _error(str(operation), str(exc))
