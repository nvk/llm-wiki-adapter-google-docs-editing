from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google_docs_adapter.auth import DRIVE_FILE_SCOPE, _authorization_parameters
from google_docs_adapter.document import tab_text_indexes
from google_docs_adapter.operations import execute
from google_docs_adapter.storage import sha256_file, write_private_json

RESOURCE = "google-docs:SyntheticDocument123"


def document(text: str, revision: str, suggestion_ids: list[str] | None = None) -> dict:
    units = len(text.encode("utf-16-le")) // 2
    element = {
        "startIndex": 1,
        "endIndex": 1 + units,
        "textRun": {"content": text},
    }
    if suggestion_ids:
        element["suggestedInsertionIds"] = suggestion_ids
    return {
        "documentId": "SyntheticDocument123",
        "revisionId": revision,
        "title": "Synthetic document",
        "tabs": [{
            "tabProperties": {"tabId": "tab-1", "title": "Synthetic tab"},
            "documentTab": {"body": {"content": [{
                "startIndex": 1,
                "endIndex": 1 + units,
                "paragraph": {"elements": [element]},
            }]}},
        }],
    }


class FakeClient:
    def __init__(self) -> None:
        self.applied = False
        self.batch_calls: list[dict] = []

    def get_document(self, _document_id: str, mode: str) -> dict:
        if not self.applied:
            return document("Alpha beta gamma.\n", "revision-1")
        if mode == "PREVIEW_WITHOUT_SUGGESTIONS":
            return document("Alpha beta gamma.\n", "revision-2")
        if mode == "PREVIEW_SUGGESTIONS_ACCEPTED":
            return document("Alpha delta gamma.\n", "revision-2")
        return document(
            "Alpha delta gamma.\n",
            "revision-2",
            ["suggestion-delete", "suggestion-insert"],
        )

    def batch_update(self, _document_id: str, body: dict) -> dict:
        self.batch_calls.append(body)
        if body["writeControl"] != {
            "writeMode": "SUGGEST",
            "requiredRevisionId": "revision-1",
        }:
            raise AssertionError("apply did not use revision-locked suggest mode")
        self.applied = True
        return {
            "documentId": "SyntheticDocument123",
            "writeControl": {"requiredRevisionId": "revision-2", "writeMode": "SUGGEST"},
            "suggestionResponses": [
                {"createdSuggestionIds": ["suggestion-delete"]},
                {"createdSuggestionIds": ["suggestion-insert"]},
            ],
            "commentUpdateState": "ALL_SAVED",
        }


class AdapterTests(unittest.TestCase):
    def test_oauth_uses_desktop_picker_and_drive_file_only(self) -> None:
        parameters = _authorization_parameters(
            "synthetic-client", "http://127.0.0.1:10000/callback", "verifier", "state"
        )
        self.assertEqual(parameters["scope"], DRIVE_FILE_SCOPE)
        self.assertEqual(parameters["trigger_onepick"], "true")
        self.assertEqual(parameters["allow_multiple"], "false")
        self.assertEqual(parameters["mimetypes"], "application/vnd.google-apps.document")

    def test_self_test_and_utf16_indexing(self) -> None:
        response = execute({"operation": "self-test"})
        self.assertEqual(response["status"], "ok")
        value = document("A 🌎 B\n", "revision")
        index = tab_text_indexes(value)["tab-1"]
        self.assertEqual(index.locate("🌎")[2:], (3, 5))

    def test_plan_apply_verify_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            state = root / "state"
            edit_spec = root / "edit-spec.json"
            write_private_json(edit_spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"tab_id": "tab-1", "find": "beta", "replace": "delta"}],
            })
            client = FakeClient()
            plan_response = execute({
                "operation": "plan",
                "arguments": {"document_resource": RESOURCE, "edit_spec": str(edit_spec)},
                "output_dir": str(output / "plan"),
            }, client)
            self.assertEqual(plan_response["status"], "ok")
            plan = output / "plan" / "plan.json"
            value = json.loads(plan.read_text())
            self.assertEqual(value["revision_id"], "revision-1")
            self.assertEqual(value["requests"][0]["deleteContentRange"]["range"], {
                "startIndex": 7,
                "endIndex": 11,
                "tabId": "tab-1",
            })
            plan_sha = sha256_file(plan)
            apply_request = {
                "operation": "apply",
                "arguments": {"document_resource": RESOURCE, "plan": str(plan)},
                "output_dir": str(output / "apply"),
                "remote_write": {
                    "plan_sha256": plan_sha,
                    "idempotency_key": "synthetic-write-0001",
                    "expected_revision": "revision-1",
                },
            }
            with mock.patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
                apply_response = execute(apply_request, client)
                replay_response = execute(apply_request, client)
            self.assertEqual(apply_response["status"], "ok")
            self.assertEqual(replay_response, apply_response)
            self.assertEqual(len(client.batch_calls), 1)
            receipt = apply_response["remote_receipt"]
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["verification"]["comment_update_state"], "ALL_SAVED")
            self.assertEqual(receipt["verification"]["suggestion_count"], 2)
            receipt_path = output / "receipt.json"
            write_private_json(receipt_path, apply_response)
            verify_response = execute({
                "operation": "verify",
                "arguments": {"document_resource": RESOURCE, "receipt": str(receipt_path)},
                "output_dir": str(output / "verify"),
            }, client)
            self.assertEqual(verify_response["status"], "ok")
            self.assertTrue(verify_response["summary"]["verified"])

    def test_plan_rejects_target_with_existing_suggestion(self) -> None:
        class SuggestedClient(FakeClient):
            def get_document(self, _document_id: str, _mode: str) -> dict:
                return document("Alpha beta gamma.\n", "revision-1", ["existing-suggestion"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"tab_id": "tab-1", "find": "beta", "replace": "delta"}],
            })
            response = execute({
                "operation": "plan",
                "arguments": {"document_resource": RESOURCE, "edit_spec": str(spec)},
                "output_dir": str(root / "output"),
            }, SuggestedClient())
            self.assertEqual(response["status"], "error")
            self.assertIn("overlaps an unresolved", response["errors"][0])

    def test_failed_comment_state_never_verifies(self) -> None:
        class FailedThreadsClient(FakeClient):
            def batch_update(self, document_id: str, body: dict) -> dict:
                value = super().batch_update(document_id, body)
                value["commentUpdateState"] = "ALL_FAILED_UNKNOWN_REASON"
                return value

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"tab_id": "tab-1", "find": "beta", "replace": "delta"}],
            })
            client = FailedThreadsClient()
            plan_response = execute({
                "operation": "plan",
                "arguments": {"document_resource": RESOURCE, "edit_spec": str(spec)},
                "output_dir": str(root / "plan"),
            }, client)
            plan = root / "plan" / "plan.json"
            request = {
                "operation": "apply",
                "arguments": {"document_resource": RESOURCE, "plan": str(plan)},
                "output_dir": str(root / "apply"),
                "remote_write": {
                    "plan_sha256": plan_response["summary"]["plan_sha256"],
                    "idempotency_key": "synthetic-write-failed",
                    "expected_revision": "revision-1",
                },
            }
            with mock.patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")}):
                response = execute(request, client)
            self.assertEqual(response["status"], "error")
            self.assertIn("did not save all", response["errors"][0])


if __name__ == "__main__":
    unittest.main()
