from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google_docs_adapter.browser_executor import snapshot_sha256
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
    def __init__(self, *, baseline: list[dict] | None = None, fail_after_boundary: bool = False) -> None:
        self.collaboration = dict(COLLABORATION)
        self.baseline = baseline or list(BASELINE)
        self.after = list(AFTER)
        self.fail_after_boundary = fail_after_boundary
        self.programs: list[dict] = []
        self.mutations = 0

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
        assert private_values["baseline.sha256"] == snapshot_sha256(self.baseline)
        before_mutation()
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
        self.assertEqual(manifest["network"], "none")
        self.assertFalse(manifest["writes_wiki"])
        self.assertNotIn("oauth", json.dumps(manifest).lower())
        for name in ("inspect", "plan", "apply", "verify"):
            self.assertEqual(
                manifest["operations"][name]["remote_resource_arguments"],
                ["collaboration_resource"],
            )
        self.assertEqual(
            manifest["operations"]["verify"]["read_arguments"],
            ["receipt", "plan"],
        )

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
            self.assertEqual(inspection["revision_id"], snapshot_sha256(BASELINE))
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
            self.assertEqual(plan["revision_id"], snapshot_sha256(BASELINE))

            remote_write = {
                "plan_sha256": sha256_file(plan_path),
                "idempotency_key": "synthetic-idempotency-1",
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


if __name__ == "__main__":
    unittest.main()
