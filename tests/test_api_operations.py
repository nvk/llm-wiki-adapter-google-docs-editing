from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from google_docs_adapter.apps_script_bridge import GoogleDocsAppsScriptClient
from google_docs_adapter.api_operations import (
    API_RESOURCE,
    PLAN_SCHEMA,
    WRITE_TRANSPORT,
    GoogleDocsRestClient,
    execute_api,
)
from google_docs_adapter.storage import load_json, sha256_file, write_private_json

DOCUMENT_ID = "SYNTHETIC_DOCUMENT_12345"
DOCUMENT_URL = f"https://docs.google.com/document/d/{DOCUMENT_ID}/edit"


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def text_element(
    text: str,
    start: int,
    *,
    inserted: list[str] | None = None,
    deleted: list[str] | None = None,
) -> dict:
    run: dict = {"content": text}
    if inserted:
        run["suggestedInsertionIds"] = inserted
    if deleted:
        run["suggestedDeletionIds"] = deleted
    return {
        "startIndex": start,
        "endIndex": start + utf16_length(text),
        "textRun": run,
    }


def document(
    revision: str,
    *,
    elements: list[dict] | None = None,
    text: str = "Hello old world\n",
    tab_id: str = "tab-1",
    suggestions: list[dict] | None = None,
    extra_tabs: list[dict] | None = None,
) -> dict:
    if elements is None:
        elements = [text_element(text, 1)]
    tabs = [
        {
            "tabProperties": {"tabId": tab_id, "title": "Synthetic tab"},
            "documentTab": {
                "body": {"content": [{"paragraph": {"elements": elements}}]}
            },
        }
    ]
    if extra_tabs:
        tabs.extend(extra_tabs)
    value = {
        "documentId": DOCUMENT_ID,
        "title": "Synthetic document",
        "revisionId": revision,
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "tabs": tabs,
    }
    if suggestions is not None:
        value["suggestions"] = suggestions
    return value


def replacement_readback(revision: str = "rev-2") -> dict:
    return document(
        revision,
        elements=[
            text_element("Hello ", 1),
            text_element("old", 7, deleted=["suggestion-1"]),
            text_element("new", 10, inserted=["suggestion-1"]),
            text_element(" world\n", 13),
        ],
        suggestions=[{"suggestionId": "suggestion-1", "status": "OPEN"}],
    )


class FakeApi:
    def __init__(self, documents: list[dict], batch: dict | None = None) -> None:
        self.documents = list(documents)
        self.batch = batch
        self.get_calls: list[str] = []
        self.batch_calls: list[tuple[str, dict]] = []

    def get_document(self, document_id: str) -> dict:
        self.get_calls.append(document_id)
        if not self.documents:
            raise AssertionError("unexpected get_document call")
        return self.documents.pop(0)

    def batch_update(self, document_id: str, body: dict) -> dict:
        self.batch_calls.append((document_id, body))
        if self.batch is None:
            raise AssertionError("unexpected batch_update call")
        return self.batch


class ApiOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.edit_spec = self.root / "edit-spec.json"
        write_private_json(
            self.edit_spec,
            {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "old", "replace": "new"}],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        operation: str,
        output_name: str,
        arguments: dict,
        remote_write: dict | None = None,
    ) -> dict:
        value = {
            "protocol": "llm-wiki-adapter/v1",
            "adapter_id": "google-docs-editing",
            "operation": operation,
            "arguments": {"api_resource": API_RESOURCE, **arguments},
            "output_dir": str(self.root / output_name),
            "options": {},
        }
        if remote_write is not None:
            value["remote_write"] = remote_write
        return value

    def plan(self, source: dict | None = None) -> tuple[dict, Path]:
        request = self.request(
            "api-plan",
            "plan",
            {"expected_document_url": DOCUMENT_URL, "edit_spec": str(self.edit_spec)},
        )
        result = execute_api(request, FakeApi([source or document("rev-1")]))
        self.assertEqual(result["status"], "ok", result)
        plan_path = self.root / "plan" / "api-plan.json"
        return load_json(plan_path, "plan"), plan_path

    def apply_request(self, plan: dict, plan_path: Path) -> dict:
        return self.request(
            "api-apply",
            "apply",
            {"plan": str(plan_path)},
            {
                "plan_sha256": sha256_file(plan_path),
                "idempotency_key": "synthetic-api-write-0001",
                "expected_revision": plan["revision_id"],
            },
        )

    def successful_batch(self) -> dict:
        return {
            "documentId": DOCUMENT_ID,
            "writeControl": {"requiredRevisionId": "rev-2"},
            "suggestionResponses": [
                {"createdSuggestionIds": ["suggestion-1"]},
                {"updatedSummarySuggestionIds": ["suggestion-1"]},
            ],
            "commentUpdateState": "ALL_SAVED",
        }

    def test_inspect_writes_curated_private_artifact(self) -> None:
        request = self.request(
            "api-inspect", "inspect", {"expected_document_url": DOCUMENT_URL}
        )
        result = execute_api(request, FakeApi([document("rev-1")]))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["summary"]["tab_count"], 1)
        artifact = load_json(
            self.root / "inspect" / "api-inspection.json", "inspection"
        )
        self.assertEqual(artifact["tabs"][0]["text"], "Hello old world\n")
        self.assertEqual(artifact["document_id"], DOCUMENT_ID)
        self.assertNotIn("comments", artifact)

    def test_plan_uses_utf16_indexes_and_explicit_tab(self) -> None:
        source = document("rev-emoji", text="A😀 old world\n")
        plan, _plan_path = self.plan(source)
        self.assertEqual(plan["schema"], PLAN_SCHEMA)
        self.assertEqual(plan["write_transport"], WRITE_TRANSPORT)
        self.assertEqual(
            plan["requests"],
            [
                {
                    "deleteContentRange": {
                        "range": {"startIndex": 5, "endIndex": 8, "tabId": "tab-1"}
                    }
                },
                {
                    "insertText": {
                        "text": "new",
                        "location": {"index": 5, "tabId": "tab-1"},
                    }
                },
            ],
        )

    def test_plan_matches_across_adjacent_styled_runs(self) -> None:
        source = document(
            "rev-split",
            elements=[text_element("Hello o", 1), text_element("ld world\n", 8)],
        )
        plan, _plan_path = self.plan(source)
        target = plan["targets"][0]
        self.assertEqual((target["start_index"], target["end_index"]), (7, 10))

    def test_plan_does_not_join_text_across_paragraph_boundaries(self) -> None:
        write_private_json(
            self.edit_spec,
            {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"find": "old", "replace": "new"}],
            },
        )
        source = document("rev-paragraphs")
        source["tabs"][0]["documentTab"]["body"]["content"] = [
            {"paragraph": {"elements": [text_element("o", 1)]}},
            {"paragraph": {"elements": [text_element("ld\n", 2)]}},
        ]
        request = self.request(
            "api-plan",
            "plan-paragraphs",
            {"expected_document_url": DOCUMENT_URL, "edit_spec": str(self.edit_spec)},
        )
        result = execute_api(request, FakeApi([source]))
        self.assertEqual(result["status"], "error")
        self.assertIn("observed 0", result["errors"][0])

    def test_plan_rejects_existing_suggestion_overlap(self) -> None:
        source = document(
            "rev-existing",
            elements=[text_element("Hello old world\n", 1, inserted=["existing-1"])],
            suggestions=[{"suggestionId": "existing-1", "status": "OPEN"}],
        )
        request = self.request(
            "api-plan",
            "plan-existing",
            {"expected_document_url": DOCUMENT_URL, "edit_spec": str(self.edit_spec)},
        )
        result = execute_api(request, FakeApi([source]))
        self.assertEqual(result["status"], "error")
        self.assertIn("overlaps an existing suggestion", result["errors"][0])

    def test_plan_rejects_existing_suggested_style_overlap(self) -> None:
        source = document("rev-existing-style")
        run = source["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"][
            "elements"
        ][0]["textRun"]
        run["suggestedTextStyleChanges"] = {"style-suggestion-1": {}}
        source["suggestions"] = [
            {"suggestionId": "style-suggestion-1", "status": "OPEN"}
        ]
        request = self.request(
            "api-plan",
            "plan-existing-style",
            {"expected_document_url": DOCUMENT_URL, "edit_spec": str(self.edit_spec)},
        )
        result = execute_api(request, FakeApi([source]))
        self.assertEqual(result["status"], "error")
        self.assertIn("overlaps an existing suggestion", result["errors"][0])

    def test_append_rejects_ambiguous_multi_tab_document(self) -> None:
        write_private_json(
            self.edit_spec,
            {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": "Synthetic append."}],
            },
        )
        second_tab = {
            "tabProperties": {"tabId": "tab-2", "title": "Second"},
            "documentTab": {
                "body": {
                    "content": [{"paragraph": {"elements": [text_element("Two\n", 1)]}}]
                }
            },
        }
        request = self.request(
            "api-plan",
            "plan-multitab",
            {"expected_document_url": DOCUMENT_URL, "edit_spec": str(self.edit_spec)},
        )
        result = execute_api(
            request, FakeApi([document("rev-tabs", extra_tabs=[second_tab])])
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("single-tab", result["errors"][0])

    def test_apply_enforces_suggest_revision_and_verifies_ids(self) -> None:
        plan, plan_path = self.plan()
        api = FakeApi(
            [document("rev-1"), replacement_readback()], self.successful_batch()
        )
        state = self.root / "state"
        with patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
            result = execute_api(self.apply_request(plan, plan_path), api)
            repeated = execute_api(self.apply_request(plan, plan_path), api)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(repeated, result)
        self.assertEqual(len(api.batch_calls), 1)
        body = api.batch_calls[0][1]
        self.assertEqual(body["requests"], plan["requests"])
        self.assertEqual(
            body["writeControl"],
            {
                "writeMode": "SUGGEST",
                "requiredRevisionId": "rev-1",
            },
        )
        receipt = result["remote_receipt"]
        self.assertEqual(receipt["before_revision"], "rev-1")
        self.assertEqual(receipt["after_revision"], "rev-2")
        self.assertEqual(
            receipt["verification"]["created_suggestion_ids"], ["suggestion-1"]
        )

    def test_single_tab_append_is_planned_and_verified_as_a_suggestion(self) -> None:
        append_text = " Synthetic append."
        write_private_json(
            self.edit_spec,
            {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"append": append_text}],
            },
        )
        plan, plan_path = self.plan()
        self.assertEqual(
            plan["requests"],
            [
                {
                    "insertText": {
                        "text": append_text,
                        "endOfSegmentLocation": {"tabId": "tab-1"},
                    }
                }
            ],
        )
        after = document(
            "rev-2",
            elements=[
                text_element("Hello old world", 1),
                text_element(append_text, 16, inserted=["suggestion-1"]),
                text_element("\n", 16 + utf16_length(append_text)),
            ],
            suggestions=[{"suggestionId": "suggestion-1", "status": "OPEN"}],
        )
        batch = {
            "documentId": DOCUMENT_ID,
            "writeControl": {"requiredRevisionId": "rev-2"},
            "suggestionResponses": [{"createdSuggestionIds": ["suggestion-1"]}],
            "commentUpdateState": "ALL_SAVED",
        }
        state = self.root / "append-state"
        with patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
            result = execute_api(
                self.apply_request(plan, plan_path),
                FakeApi([document("rev-1"), after], batch),
            )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["summary"]["suggestion_count"], 1)

    def test_partial_comment_failure_is_not_retried_or_auto_receipted(self) -> None:
        plan, plan_path = self.plan()
        ambiguous_batch = {
            "documentId": DOCUMENT_ID,
            "writeControl": {"requiredRevisionId": "rev-2"},
            "suggestionResponses": [
                {"createdSuggestionIds": ["suggestion-1"]},
                {"updatedSummarySuggestionIds": ["suggestion-1"]},
            ],
            "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON",
        }
        apply_api = FakeApi([document("rev-1")], ambiguous_batch)
        request = self.apply_request(plan, plan_path)
        state = self.root / "recover-state"
        with patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
            failed = execute_api(request, apply_api)
            request["operation"] = "api-recover"
            recovered = execute_api(request, FakeApi([replacement_readback()]))
        self.assertEqual(failed["status"], "error")
        self.assertIn("ambiguous", failed["errors"][0])
        self.assertEqual(recovered["status"], "error", recovered)
        self.assertIn("no returned suggestion IDs", recovered["errors"][0])
        self.assertEqual(len(apply_api.batch_calls), 1)

    def test_readback_failure_can_recover_with_returned_suggestion_ids(self) -> None:
        plan, plan_path = self.plan()
        request = self.apply_request(plan, plan_path)
        state = self.root / "readback-recovery-state"
        with patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
            failed = execute_api(
                request,
                FakeApi(
                    [document("rev-1"), document("rev-2")],
                    self.successful_batch(),
                ),
            )
            request["operation"] = "api-recover"
            recovered = execute_api(request, FakeApi([replacement_readback()]))
        self.assertEqual(failed["status"], "error")
        self.assertEqual(recovered["status"], "ok", recovered)
        self.assertEqual(recovered["operation"], "api-recover")
        ids = recovered["remote_receipt"]["verification"]["created_suggestion_ids"]
        self.assertEqual(ids, ["suggestion-1"])

    def test_verify_reads_back_receipted_suggestion_ids(self) -> None:
        plan, plan_path = self.plan()
        api = FakeApi(
            [document("rev-1"), replacement_readback()], self.successful_batch()
        )
        state = self.root / "verify-state"
        with patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)}):
            applied = execute_api(self.apply_request(plan, plan_path), api)
        receipt_path = self.root / "receipt.json"
        write_private_json(receipt_path, applied)
        request = self.request(
            "api-verify",
            "verify",
            {"plan": str(plan_path), "receipt": str(receipt_path)},
        )
        result = execute_api(request, FakeApi([replacement_readback("rev-3")]))
        self.assertEqual(result["status"], "ok", result)
        report = load_json(self.root / "verify" / "api-verification.json", "verify")
        self.assertEqual(report["status"], "verified")

    def test_rest_client_requires_a_configured_token_source(self) -> None:
        isolated_oauth = self.root / "missing-oauth"
        with patch.dict(
            os.environ,
            {"LLM_WIKI_GOOGLE_DOCS_OAUTH_DIR": str(isolated_oauth)},
            clear=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN"
            ):
                GoogleDocsRestClient.from_environment()

    def test_api_errors_do_not_echo_access_token_or_google_message(self) -> None:
        token = "synthetic-secret-token"
        client = GoogleDocsRestClient(token)
        error_body = json.dumps(
            {
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "message": f"sensitive project detail {token}",
                }
            }
        ).encode()

        http_error = __import__("urllib.error").error.HTTPError(
            DOCUMENT_URL, 403, "Forbidden", {}, None
        )
        http_error.read = lambda: error_body  # type: ignore[method-assign]
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(RuntimeError) as raised:
                client.get_document(DOCUMENT_ID)
        self.assertIn("PERMISSION_DENIED", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("sensitive project", str(raised.exception))

    def test_apps_script_bridge_calls_only_fixed_functions(self) -> None:
        returned = document("rev-bridge")
        response = io.BytesIO(
            json.dumps(
                {"done": True, "response": {"result": returned}}
            ).encode("utf-8")
        )
        client = GoogleDocsAppsScriptClient(
            "synthetic-token", "synthetic_deployment_id_123456789"
        )
        with patch("urllib.request.urlopen", return_value=response) as opened:
            result = client.get_document(DOCUMENT_ID)
        self.assertEqual(result, returned)
        request = opened.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent["function"], "llmWikiBridgeGetDocument")
        self.assertEqual(sent["parameters"], [DOCUMENT_ID])
        self.assertFalse(sent["devMode"])
        self.assertNotIn("synthetic-token", request.full_url)

    def test_apps_script_bridge_errors_do_not_echo_tokens_or_script_details(self) -> None:
        token = "synthetic-secret-token"
        error_body = json.dumps(
            {
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "message": f"sensitive script detail {token}",
                }
            }
        ).encode()
        http_error = __import__("urllib.error").error.HTTPError(
            "https://script.googleapis.com/", 403, "Forbidden", {}, None
        )
        http_error.read = lambda: error_body  # type: ignore[method-assign]
        client = GoogleDocsAppsScriptClient(token, "synthetic_deployment_id_123456789")
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(RuntimeError) as raised:
                client.get_document(DOCUMENT_ID)
        self.assertIn("PERMISSION_DENIED", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("sensitive script", str(raised.exception))

    def test_api_request_builders_and_serialized_runner(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan_output = self.root / "builder-plan"
        plan_request = self.root / "builder-plan-request.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "make_api_plan_request.py"),
                "--url",
                DOCUMENT_URL,
                "--edit-spec",
                str(self.edit_spec),
                "--output-dir",
                str(plan_output),
                "--request",
                str(plan_request),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        built = load_json(plan_request, "built API request")
        self.assertEqual(built["operation"], "api-plan")
        self.assertEqual(built["arguments"]["api_resource"], API_RESOURCE)

        call_log = self.root / "api-runner-calls.txt"
        fake_llm_wiki = self.root / "fake-llm-wiki"
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
            "if operation=='api-plan':\n"
            "  spec=json.loads(Path(value['arguments']['edit_spec']).read_text())\n"
            "  plan={'schema':'google-docs-api-suggestion-plan/v2',"
            "'write_transport':'google-docs-api-suggest-apps-script-bridge-v1',"
            "'api_resource':'google-docs-api:authorized-files',"
            "'revision_id':'revision-1','edits':spec['edits']}\n"
            "  output=Path(value['output_dir']); output.mkdir(parents=True,exist_ok=True)\n"
            "  (output/'api-plan.json').write_text(json.dumps(plan))\n"
            "  result={'status':'ok','adapter_version':'0.13.0'}\n"
            "elif operation=='api-apply':\n"
            "  plan_path=Path(value['arguments']['plan'])\n"
            "  digest=hashlib.sha256(plan_path.read_bytes()).hexdigest()\n"
            "  assert args[args.index('--approve-remote-write')+1]==digest\n"
            "  result={'status':'ok','summary':{'suggestion_count':1,"
            "'tracked_changes':True},'remote_receipt':{'plan_sha256':digest,"
            "'verification':{'status':'verified'}}}\n"
            "elif operation=='api-verify':\n"
            "  result={'status':'ok','summary':{'verified':True}}\n"
            "else: raise SystemExit(3)\n"
            "response.parent.mkdir(parents=True,exist_ok=True)\n"
            "response.write_text(json.dumps(result))\n",
            encoding="utf-8",
        )
        fake_llm_wiki.chmod(0o700)
        run_dir = self.root / "api-workflow"
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "run_api_suggestion_workflow.py"),
                "--llm-wiki",
                str(fake_llm_wiki),
                "--url",
                DOCUMENT_URL,
                "--edit-spec",
                str(self.edit_spec),
                "--run-dir",
                str(run_dir),
                "--idempotency-key",
                "synthetic-api-runner",
                "--approve-remote-write",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            call_log.read_text(encoding="utf-8").splitlines(),
            ["api-plan", "api-apply", "api-verify"],
        )
        self.assertTrue(json.loads(completed.stdout)["verified"])


if __name__ == "__main__":
    unittest.main()
