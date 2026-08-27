from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google_docs_adapter import __version__
from google_docs_adapter.browser_executor import (
    document_projection,
    document_projection_sha256,
)
from google_docs_adapter.browser_operations import COLLABORATION_RESOURCE, execute
from google_docs_adapter.storage import sha256_file, write_private_json

DOCUMENT_ID = "SyntheticBrowserDocument123"
DOCUMENT_URL = f"https://docs.google.com/document/d/{DOCUMENT_ID}/edit?tab=t.0"
COLLABORATION = {
    "collaboration_id": "d" * 64,
    "url": DOCUMENT_URL,
    "origin": "https://docs.google.com",
}


def row(role: str, name: str, value: str | None = None) -> dict:
    return {
        "role": role,
        "name": name,
        "value": value,
        "description": None,
    }


BASELINE = [
    row("document", "Synthetic document"),
    row("button", "Editing mode"),
    row("paragraph", "Synthetic old phrase."),
]
AFTER = [
    row("document", "Synthetic document"),
    row("button", "Suggesting mode"),
    row("paragraph", "Synthetic new phrase."),
]


class FakeBrowser:
    def __init__(
        self,
        *,
        baseline: list[dict] | None = None,
        fail_after_boundary: bool = False,
        presence_found: bool = True,
    ) -> None:
        self.collaboration = dict(COLLABORATION)
        self.baseline = baseline or list(BASELINE)
        self.after = list(AFTER)
        self.fail_after_boundary = fail_after_boundary
        self.presence_found = presence_found
        self.programs: list[dict] = []
        self.mutations = 0
        self.presence_probes = 0

    def collaborations(self) -> list[dict[str, str]]:
        return [dict(self.collaboration)] if self.collaboration else []

    def collaboration_for_url(self, raw_url: str) -> dict[str, str] | None:
        if self.collaboration and self.collaboration["url"] == raw_url:
            return dict(self.collaboration)
        return None

    def run(
        self,
        program: dict,
        *,
        private_values: dict[str, str] | None = None,
        before_mutation: object | None = None,
    ) -> dict:
        self.programs.append(program)
        if program["capability"] == "read":
            return {"status": "ok", "public": {}, "private": {"docs.ax": list(self.baseline)}}
        assert callable(before_mutation)
        assert private_values is not None
        assert "baseline.sha256" not in private_values
        before_mutation()
        if program["program_id"] == "google-docs-suggestion-presence-v1":
            self.presence_probes += 1
            return {
                "status": "ok" if self.presence_found else "error",
                "public": {"mutation_started": True},
                "private": {},
                "error": None if self.presence_found else "synthetic-text-not-found",
            }
        self.mutations += 1
        if self.fail_after_boundary:
            return {"status": "error", "public": {}, "private": {}, "error": "synthetic-failure"}
        return {
            "status": "ok",
            "public": {"mutation_started": True},
            "private": {"docs.after-ax": list(self.after)},
        }


class BrowserOperationsTests(unittest.TestCase):
    def request(
        self,
        operation: str,
        output_dir: Path,
        arguments: dict,
        remote_write: dict | None = None,
    ) -> dict:
        value = {
            "protocol": "llm-wiki-adapter/v1",
            "adapter_id": "google-docs-editing",
            "operation": operation,
            "arguments": arguments,
            "output_dir": str(output_dir),
            "options": {},
        }
        if remote_write is not None:
            value["remote_write"] = remote_write
        return value

    def test_manifest_uses_one_static_collaboration_capability_and_no_oauth(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".llm-wiki-adapter.json").read_text(encoding="utf-8"))
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{__version__}"', project)
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(manifest["network"], "none")
        self.assertFalse(manifest["writes_wiki"])
        self.assertNotIn("oauth", json.dumps(manifest).lower())
        self.assertTrue(
            {"inspect", "read", "review"}.issubset(
                set(manifest["routes"][0]["intents"])
            )
        )
        for name in ("inspect", "plan", "apply", "recover", "verify"):
            self.assertEqual(
                manifest["operations"][name]["remote_resource_arguments"],
                ["collaboration_resource"],
            )
        self.assertEqual(
            manifest["operations"]["verify"]["read_arguments"],
            ["receipt", "plan"],
        )

    def test_request_builders_create_all_private_requests(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            spec = private_root / "input" / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic appended suggestion."}],
            })
            cases = [
                (
                    "make_inspect_request.py",
                    [],
                    "inspect",
                    private_root / "inspect-output",
                ),
                (
                    "make_plan_request.py",
                    ["--edit-spec", str(spec)],
                    "plan",
                    private_root / "plan-output",
                ),
            ]
            for script, extra_args, operation, output_dir in cases:
                with self.subTest(script=script):
                    request_path = private_root / f"{operation}-request.json"
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(root / "scripts" / script),
                            "--url",
                            DOCUMENT_URL,
                            *extra_args,
                            "--output-dir",
                            str(output_dir),
                            "--request",
                            str(request_path),
                        ],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        Path(completed.stdout.strip()), request_path.resolve()
                    )
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    self.assertEqual(request["operation"], operation)
                    self.assertEqual(request["output_dir"], str(output_dir.resolve()))
                    self.assertEqual(
                        request["arguments"]["expected_document_url"],
                        DOCUMENT_URL,
                    )
                    if operation == "plan":
                        self.assertEqual(
                            request["arguments"]["edit_spec"], str(spec.resolve())
                        )
                    self.assertEqual(request_path.stat().st_mode & 0o077, 0)

            plan_path = private_root / "plan.json"
            write_private_json(plan_path, {
                "schema": "google-docs-browser-suggestion-plan/v1",
                "write_transport": "shared-browser-executor-suggesting-ui",
                "collaboration_resource": COLLABORATION_RESOURCE,
                "revision_id": "a" * 64,
            })
            apply_output = private_root / "apply-output"
            apply_request_path = private_root / "apply-request.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "make_apply_request.py"),
                    "--plan",
                    str(plan_path),
                    "--idempotency-key",
                    "synthetic-builder-key",
                    "--output-dir",
                    str(apply_output),
                    "--request",
                    str(apply_request_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), sha256_file(plan_path))
            apply_request = json.loads(
                apply_request_path.read_text(encoding="utf-8")
            )
            self.assertEqual(apply_request["operation"], "apply")
            self.assertEqual(
                apply_request["arguments"]["plan"], str(plan_path.resolve())
            )
            self.assertEqual(apply_request_path.stat().st_mode & 0o077, 0)
            receipt_path = private_root / "receipt.json"
            write_private_json(receipt_path, {
                "remote_receipt": {"plan_sha256": sha256_file(plan_path)},
            })
            output_dir = private_root / "verify-output"
            request_path = private_root / "verify-request.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "make_verify_request.py"),
                    "--plan",
                    str(plan_path),
                    "--receipt",
                    str(receipt_path),
                    "--output-dir",
                    str(output_dir),
                    "--request",
                    str(request_path),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["operation"], "verify")
            self.assertEqual(request["arguments"]["plan"], str(plan_path.resolve()))
            self.assertEqual(
                request["arguments"]["receipt"], str(receipt_path.resolve())
            )
            self.assertEqual(request_path.stat().st_mode & 0o077, 0)

    def test_serialized_workflow_runner_completes_plan_apply_and_verify(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            private_root = Path(temporary)
            spec = private_root / "input" / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic serialized suggestion."}],
            })
            call_log = private_root / "calls.txt"
            fake_llm_wiki = private_root / "fake-llm-wiki"
            fake_llm_wiki.write_text(
                "#!/usr/bin/env python3\n"
                "import hashlib,json,sys\n"
                "from pathlib import Path\n"
                f"log=Path({str(call_log)!r})\n"
                "args=sys.argv[1:]\n"
                "request=Path(args[args.index('--request')+1])\n"
                "response=Path(args[args.index('--response')+1])\n"
                "value=json.loads(request.read_text())\n"
                "operation=value['operation']\n"
                "with log.open('a') as handle: handle.write(operation+'\\n')\n"
                "if operation=='plan':\n"
                "  spec=json.loads(Path(value['arguments']['edit_spec']).read_text())\n"
                "  plan={'schema':'google-docs-browser-suggestion-plan/v1',"
                "'write_transport':'shared-browser-executor-suggesting-ui',"
                "'collaboration_resource':'browser-collaboration:active-tab',"
                "'revision_id':'a'*64,'edits':spec['edits']}\n"
                "  output=Path(value['output_dir']); output.mkdir(parents=True,exist_ok=True)\n"
                "  (output/'plan.json').write_text(json.dumps(plan))\n"
                "  result={'status':'ok','adapter_version':'0.9.0'}\n"
                "elif operation=='apply':\n"
                "  plan_path=Path(value['arguments']['plan'])\n"
                "  digest=hashlib.sha256(plan_path.read_bytes()).hexdigest()\n"
                "  assert args[args.index('--approve-remote-write')+1]==digest\n"
                "  if value['remote_write']['idempotency_key'].endswith('recovery'):\n"
                "    result={'status':'error'}\n"
                "  else: result={'status':'ok','summary':{'suggestion_count':1,"
                "'tracked_changes':True},'remote_receipt':{'plan_sha256':digest,"
                "'verification':{'status':'verified'}}}\n"
                "elif operation=='recover':\n"
                "  plan_path=Path(value['arguments']['plan'])\n"
                "  digest=hashlib.sha256(plan_path.read_bytes()).hexdigest()\n"
                "  assert args[args.index('--approve-remote-write')+1]==digest\n"
                "  result={'status':'ok','summary':{'suggestion_count':1,"
                "'tracked_changes':True},'remote_receipt':{'plan_sha256':digest,"
                "'verification':{'status':'verified'}}}\n"
                "elif operation=='verify':\n"
                "  result={'status':'ok','summary':{'verified':True}}\n"
                "else: raise SystemExit(3)\n"
                "response.parent.mkdir(parents=True,exist_ok=True)\n"
                "response.write_text(json.dumps(result))\n",
                encoding="utf-8",
            )
            fake_llm_wiki.chmod(0o700)
            run_dir = private_root / "output" / "workflow"
            run_dir.mkdir(parents=True)
            command = [
                sys.executable,
                str(root / "scripts" / "run_suggestion_workflow.py"),
                "--llm-wiki",
                str(fake_llm_wiki),
                "--url",
                DOCUMENT_URL,
                "--edit-spec",
                str(spec),
                "--run-dir",
                str(run_dir),
                "--idempotency-key",
                "synthetic-serialized-workflow",
                "--approve-remote-write",
            ]
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(call_log.read_text().splitlines(), [
                "plan", "apply", "verify",
            ])

            recovery_run_dir = private_root / "output" / "recovery-workflow"
            recovery_command = list(command)
            recovery_command[recovery_command.index(str(run_dir))] = str(
                recovery_run_dir
            )
            recovery_command[
                recovery_command.index("synthetic-serialized-workflow")
            ] = "synthetic-serialized-recovery"
            recovered = subprocess.run(
                recovery_command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(call_log.read_text().splitlines(), [
                "plan", "apply", "verify", "plan", "apply", "recover", "verify",
            ])
            self.assertEqual(json.loads(recovered.stdout)["status"], "ok")
            self.assertTrue((recovery_run_dir / "recover-response.json").is_file())
            final = json.loads(completed.stdout)
            self.assertEqual(final["status"], "ok")
            self.assertTrue(final["tracked_changes"])
            self.assertTrue(final["verified"])
            self.assertTrue((run_dir / "apply-response.json").is_file())
            self.assertTrue((run_dir / "verify-response.json").is_file())

            repeated = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(json.loads(repeated.stdout)["stage"], "validate")
            self.assertEqual(call_log.read_text().splitlines(), [
                "plan", "apply", "verify", "plan", "apply", "recover", "verify",
            ])

    def test_docs_live_region_fallback_excludes_accumulated_cursor_announcements(self) -> None:
        content = row("StaticText", "Synthetic document content.")
        first = [
            row("RootWebArea", "Synthetic - Google Docs"),
            row("StaticText", "Banner hidden\u00a0"),
            row("StaticText", "Screen reader support enabled."),
            content,
            row("InlineTextBox", "Banner hidden\u00a0"),
        ]
        second = [
            row("RootWebArea", "Synthetic changed title - Google Docs"),
            row("StaticText", "Banner hidden\u00a0"),
            row("StaticText", "Suggested insert end"),
            row("StaticText", "new line"),
            content,
            row("StaticText", "Suggested insert exited"),
            row("InlineTextBox", "Banner hidden\u00a0"),
        ]
        self.assertEqual(document_projection(first), [content])
        self.assertEqual(
            document_projection_sha256(first),
            document_projection_sha256(second),
        )

    def test_isolated_adapter_bootstraps_client_from_companion_command(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            client_root = temporary_root / "client"
            package = client_root / "browser_executor"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "client.py").write_text(
                "class BrowserExecutorClient:\n    pass\n",
                encoding="utf-8",
            )
            (client_root / ".llm-wiki-adapter.json").write_text(json.dumps({
                "id": "browser-execution",
                "version": "0.1.1",
            }), encoding="utf-8")
            command = temporary_root / "llm-wiki-chrome"
            command.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = client-path ]; then\n"
                f"  printf '%s\\n' '{client_root}'\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            command.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = f"{temporary_root}:{environment.get('PATH', '')}"
            environment["PYTHONPATH"] = str(root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from google_docs_adapter.browser_operations import _default_browser; "
                    "print(type(_default_browser()).__module__)",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "browser_executor.client")

    def test_browser_only_inspect_plan_apply_and_verify(self) -> None:
        browser = FakeBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "input" / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{
                    "find": "Synthetic old phrase.",
                    "replace": "Synthetic new phrase.",
                }],
            })
            inspect = execute(self.request("inspect", root / "inspect", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
            }), browser)
            self.assertEqual(inspect["status"], "ok")
            self.assertFalse(inspect["summary"]["oauth_used"])
            inspection = json.loads((root / "inspect" / "inspection.json").read_text())
            self.assertEqual(inspection["revision_id"], document_projection_sha256(BASELINE))
            self.assertIn("Synthetic old phrase.", inspection["text_fragments"])

            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            self.assertEqual(planned["status"], "ok")
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            self.assertEqual(plan["schema"], "google-docs-browser-suggestion-plan/v1")
            self.assertEqual(plan["revision_id"], document_projection_sha256(BASELINE))

            remote_write = {
                "plan_sha256": sha256_file(plan_path),
                "idempotency_key": "synthetic-idempotency-1",  # gitleaks:allow -- synthetic fixture
                "expected_revision": plan["revision_id"],
            }
            state = root / "state"
            with mock.patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, remote_write), browser)
            self.assertEqual(applied["status"], "ok")
            self.assertTrue(applied["summary"]["tracked_changes"])
            self.assertEqual(applied["remote_receipt"]["resources"], [COLLABORATION_RESOURCE])
            self.assertEqual(browser.mutations, 1)
            mutation_program = next(value for value in browser.programs if value["capability"] == "mutation")
            encoded_program = json.dumps(mutation_program)
            self.assertNotIn("Synthetic old phrase.", encoded_program)
            self.assertNotIn("Synthetic new phrase.", encoded_program)

            receipt = root / "receipt.json"
            write_private_json(receipt, applied)
            browser.baseline = list(AFTER)
            verified = execute(self.request("verify", root / "verify", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "receipt": str(receipt),
                "plan": str(plan_path),
            }), browser)
            self.assertEqual(verified["status"], "ok")
            self.assertTrue(verified["summary"]["verified"])
            verification = json.loads(
                (root / "verify" / "verification.json").read_text()
            )
            self.assertTrue(verification["receipt_projection_matches"])

    def test_requested_document_is_selected_from_multiple_explicit_tabs(self) -> None:
        other = {
            "collaboration_id": "e" * 64,
            "url": "https://docs.google.com/document/d/AnotherSyntheticDocument123/edit",
            "origin": "https://docs.google.com",
        }

        class MultiTabBrowser(FakeBrowser):
            def collaborations(self) -> list[dict[str, str]]:
                return [dict(other), dict(self.collaboration)]

            def collaboration_for_url(self, raw_url: str) -> dict[str, str] | None:
                return next((
                    value for value in self.collaborations() if value["url"] == raw_url
                ), None)

        browser = MultiTabBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inspected = execute(self.request("inspect", root, {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
            }), browser)
            self.assertEqual(inspected["status"], "ok")
            self.assertEqual(browser.programs[-1]["target"]["url"], DOCUMENT_URL)

    def test_invalid_expected_document_url_fails_before_workspace_matching(self) -> None:
        browser = FakeBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            inspected = execute(self.request("inspect", Path(temporary), {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": "https://example.invalid/document/d/synthetic/edit",
            }), browser)
        self.assertEqual(inspected["status"], "error")
        self.assertIn("expected_document_url", inspected["errors"][0])
        self.assertEqual(browser.programs, [])

    def test_inspection_error_reports_the_bounded_action(self) -> None:
        class InspectionFailureBrowser(FakeBrowser):
            def run(self, program: dict, **kwargs: object) -> dict:
                self.programs.append(program)
                return {
                    "status": "error",
                    "public": {"action_count": 12},
                    "private": {},
                    "error": "synthetic-cdp-failure",
                }

        browser = InspectionFailureBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            inspected = execute(self.request("inspect", Path(temporary), {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
            }), browser)
        self.assertEqual(inspected["status"], "error")
        self.assertIn("synthetic-cdp-failure at bounded action 12", inspected["errors"][0])

    def test_inspection_retries_transient_cdp_timeouts_inside_the_adapter(self) -> None:
        class TransientInspectionBrowser(FakeBrowser):
            def __init__(self) -> None:
                super().__init__()
                self.failures_remaining = 2

            def run(self, program: dict, **kwargs: object) -> dict:
                if self.failures_remaining:
                    self.failures_remaining -= 1
                    self.programs.append(program)
                    return {
                        "status": "error",
                        "public": {"action_count": 14},
                        "private": {},
                        "error": "cdp-command-timeout",
                    }
                return super().run(program, **kwargs)

        browser = TransientInspectionBrowser()
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "google_docs_adapter.browser_operations.time.sleep"
        ) as sleep:
            inspected = execute(self.request("inspect", Path(temporary), {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
            }), browser)
        self.assertEqual(inspected["status"], "ok")
        self.assertEqual(len(browser.programs), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_wrong_exposed_document_and_revision_drift_fail_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = FakeBrowser()
            wrong = execute(self.request("inspect", root / "wrong", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": "https://docs.google.com/document/d/AnotherSyntheticDocument123/edit",
            }), browser)
            self.assertEqual(wrong["status"], "error")
            self.assertIn("none of the explicitly shared tabs", wrong["errors"][0])

            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            browser.baseline = [*BASELINE, row("paragraph", "Synthetic externally changed content.")]
            drifted = execute(self.request("apply", root / "apply", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "plan": str(plan_path),
            }, {
                "plan_sha256": planned["summary"]["plan_sha256"],
                "idempotency_key": "synthetic-drift-key",
                "expected_revision": plan["revision_id"],
            }), browser)
            self.assertEqual(drifted["status"], "error")
            self.assertIn("changed after planning", drifted["errors"][0])
            self.assertEqual(browser.mutations, 0)

    def test_volatile_editor_chrome_does_not_invalidate_document_revision(self) -> None:
        browser = FakeBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            browser.baseline = [*BASELINE, row("button", "Synthetic volatile toolbar state")]
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")},
            ):
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, {
                    "plan_sha256": planned["summary"]["plan_sha256"],
                    "idempotency_key": "synthetic-volatile-ui-key",
                    "expected_revision": plan["revision_id"],
                }), browser)
            self.assertEqual(applied["status"], "ok")
            self.assertEqual(browser.mutations, 1)

    def test_default_journal_stays_with_the_private_plan(self) -> None:
        browser = FakeBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            with mock.patch.dict(os.environ, {}, clear=True):
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, {
                    "plan_sha256": planned["summary"]["plan_sha256"],
                    "idempotency_key": "synthetic-default-journal",
                    "expected_revision": plan["revision_id"],
                }), browser)
            self.assertEqual(applied["status"], "ok")
            journals = list((plan_path.parent / ".google-docs-state" / "browser-journal").glob("*.json"))
            self.assertEqual(len(journals), 1)

    def test_preflight_error_is_reported_before_authorization(self) -> None:
        class PreflightFailureBrowser(FakeBrowser):
            def run(self, program: dict, **kwargs: object) -> dict:
                if program["capability"] == "mutation":
                    self.programs.append(program)
                    return {
                        "status": "error",
                        "public": {"mutation_started": False, "action_count": 17},
                        "private": {},
                        "error": "synthetic-private-value-mismatch",
                    }
                return super().run(program, **kwargs)

        browser = PreflightFailureBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            failed = execute(self.request("apply", root / "apply", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "plan": str(plan_path),
            }, {
                "plan_sha256": planned["summary"]["plan_sha256"],
                "idempotency_key": "synthetic-preflight-failure",
                "expected_revision": plan["revision_id"],
            }), browser)
            self.assertEqual(failed["status"], "error")
            self.assertIn("preflight failed before authorization", failed["errors"][0])
            self.assertIn("synthetic-private-value-mismatch", failed["errors"][0])
            self.assertIn("bounded action 17", failed["errors"][0])
            self.assertEqual(browser.mutations, 0)
            self.assertFalse(list((plan_path.parent / ".google-docs-state").rglob("*.json")))

    def test_transient_suggestion_preflight_retries_only_before_boundary(self) -> None:
        class TransientPreflightBrowser(FakeBrowser):
            def __init__(self) -> None:
                super().__init__()
                self.preflight_failures_remaining = 2

            def run(self, program: dict, **kwargs: object) -> dict:
                if (
                    program["capability"] == "mutation"
                    and self.preflight_failures_remaining
                ):
                    self.preflight_failures_remaining -= 1
                    self.programs.append(program)
                    return {
                        "status": "error",
                        "public": {"mutation_started": False, "action_count": 20},
                        "private": {},
                        "error": "cdp-command-timeout",
                    }
                return super().run(program, **kwargs)

        browser = TransientPreflightBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic appended suggestion."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            browser.after = [*AFTER, row("paragraph", "Synthetic appended suggestion.")]
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")},
            ), mock.patch("google_docs_adapter.browser_operations.time.sleep") as sleep:
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, {
                    "plan_sha256": planned["summary"]["plan_sha256"],
                    "idempotency_key": "synthetic-transient-preflight",
                    "expected_revision": plan["revision_id"],
                }), browser)
            self.assertEqual(applied["status"], "ok")
            self.assertEqual(browser.mutations, 1)
            mutation_programs = [
                value for value in browser.programs
                if value["capability"] == "mutation"
            ]
            self.assertEqual(len(mutation_programs), 3)
            self.assertEqual(sleep.call_count, 2)

    def test_pending_journal_blocks_duplicate_after_boundary_failure(self) -> None:
        browser = FakeBrowser(fail_after_boundary=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            remote_write = {
                "plan_sha256": planned["summary"]["plan_sha256"],
                "idempotency_key": "synthetic-pending-key",
                "expected_revision": plan["revision_id"],
            }
            request = self.request("apply", root / "apply", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "plan": str(plan_path),
            }, remote_write)
            with mock.patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")}):
                first = execute(request, browser)
                second = execute(request, browser)
            self.assertEqual(first["status"], "error")
            self.assertEqual(second["status"], "error")
            self.assertIn("refusing a duplicate", second["errors"][0])
            self.assertEqual(browser.mutations, 1)

    def test_append_readback_uses_exact_find_probe_when_ax_text_is_truncated(self) -> None:
        browser = FakeBrowser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic appended suggestion."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            # The changed projection omits the full suggestion, as Docs does
            # after collapsing a long suggestion card.
            browser.after = list(AFTER)
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")},
            ):
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, {
                    "plan_sha256": planned["summary"]["plan_sha256"],
                    "idempotency_key": "synthetic-truncated-append",
                    "expected_revision": plan["revision_id"],
                }), browser)
            self.assertEqual(applied["status"], "ok")
            self.assertEqual(browser.mutations, 1)
            self.assertEqual(browser.presence_probes, 1)
            self.assertEqual(
                applied["remote_receipt"]["verification"]["verification_method"],
                "exact-docs-find-probe",
            )

    def test_pending_append_can_be_recovered_without_duplicate_mutation(self) -> None:
        browser = FakeBrowser(fail_after_boundary=True, presence_found=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic recovered suggestion."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            remote_write = {
                "plan_sha256": planned["summary"]["plan_sha256"],
                "idempotency_key": "synthetic-recovered-append",
                "expected_revision": plan["revision_id"],
            }
            apply_request = self.request("apply", root / "apply", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "plan": str(plan_path),
            }, remote_write)
            recover_request = self.request("recover", root / "recover", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "plan": str(plan_path),
            }, remote_write)
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")},
            ):
                failed = execute(apply_request, browser)
                browser.fail_after_boundary = False
                browser.presence_found = True
                recovered = execute(recover_request, browser)
                repeated = execute(apply_request, browser)
            self.assertEqual(failed["status"], "error")
            self.assertEqual(recovered["status"], "ok")
            self.assertEqual(recovered["operation"], "recover")
            self.assertEqual(repeated["status"], "ok")
            self.assertEqual(repeated["operation"], "apply")
            self.assertEqual(browser.mutations, 1)
            self.assertEqual(browser.presence_probes, 2)

    def test_append_plan_needs_no_existing_source_text(self) -> None:
        browser = FakeBrowser()
        browser.after = [*AFTER, row("paragraph", "Synthetic appended suggestion.")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic appended suggestion."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            self.assertEqual(planned["status"], "ok")
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            remote_write = {
                "plan_sha256": sha256_file(plan_path),
                "idempotency_key": "synthetic-append-key",
                "expected_revision": plan["revision_id"],
            }
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")},
            ):
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, remote_write), browser)
            self.assertEqual(applied["status"], "ok")
            self.assertTrue(applied["remote_receipt"]["verification"]["planned_text_observed_after_mutation"])
            self.assertFalse(
                applied["remote_receipt"]["verification"][
                    "unique_find_preconditions_asserted_before_mutation"
                ]
            )

            receipt = root / "receipt.json"
            write_private_json(receipt, applied)
            browser.baseline = [
                row("document", "Synthetic document with volatile UI state"),
                row("button", "Suggesting mode"),
                row("paragraph", "Synthetic appended suggestion."),
            ]
            verified = execute(self.request("verify", root / "verify", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "receipt": str(receipt),
                "plan": str(plan_path),
            }), browser)
            self.assertEqual(verified["status"], "ok")
            report = json.loads((root / "verify" / "verification.json").read_text())
            self.assertTrue(report["planned_text_matches"])
            self.assertFalse(report["receipt_projection_matches"])

    def test_readback_matches_text_split_across_accessibility_fragments(self) -> None:
        browser = FakeBrowser()
        browser.after = [
            row("document", "Synthetic document"),
            row("button", "Suggesting mode"),
            row("InlineTextBox", "Synthetic new "),
            row("InlineTextBox", "phrase."),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "Synthetic old phrase.", "replace": "Synthetic new phrase."}],
            })
            planned = execute(self.request("plan", root / "plan", {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": DOCUMENT_URL,
                "edit_spec": str(spec),
            }), browser)
            plan_path = root / "plan" / "plan.json"
            plan = json.loads(plan_path.read_text())
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")},
            ):
                applied = execute(self.request("apply", root / "apply", {
                    "collaboration_resource": COLLABORATION_RESOURCE,
                    "plan": str(plan_path),
                }, {
                    "plan_sha256": planned["summary"]["plan_sha256"],
                    "idempotency_key": "synthetic-split-readback-key",
                    "expected_revision": plan["revision_id"],
                }), browser)
            self.assertEqual(applied["status"], "ok")


if __name__ == "__main__":
    unittest.main()
