from __future__ import annotations

import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

from google_docs_adapter.storage import sha256_file, write_private_json

DOCUMENT_ID = "SyntheticControlPlaneDocument123"
RESOURCE = f"google-docs:{DOCUMENT_ID}"


def api_document(text: str, revision: str, suggestions: list[str] | None = None) -> dict:
    units = len(text.encode("utf-16-le")) // 2
    element = {"startIndex": 1, "endIndex": 1 + units, "textRun": {"content": text}}
    if suggestions:
        element["suggestedInsertionIds"] = suggestions
    return {
        "documentId": DOCUMENT_ID,
        "revisionId": revision,
        "title": "Synthetic tracked document",
        "tabs": [{
            "tabProperties": {"tabId": "tab-synthetic", "title": "Synthetic"},
            "documentTab": {"body": {"content": [{
                "startIndex": 1,
                "endIndex": 1 + units,
                "paragraph": {"elements": [element]},
            }]}},
        }],
    }


class ApiHandler(http.server.BaseHTTPRequestHandler):
    applied = False
    post_count = 0
    last_body: dict = {}
    preview_supported = True

    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        mode = query.get("suggestionsViewMode", [""])[0]
        if not type(self).applied:
            value = api_document("Synthetic old phrase.\n", "revision-1")
        elif mode == "PREVIEW_WITHOUT_SUGGESTIONS":
            value = api_document("Synthetic old phrase.\n", "revision-2")
        elif mode == "PREVIEW_SUGGESTIONS_ACCEPTED":
            value = api_document("Synthetic new phrase.\n", "revision-2")
        else:
            value = api_document(
                "Synthetic new phrase.\n",
                "revision-2",
                ["suggestion-delete", "suggestion-insert"],
            )
        if (
            type(self).preview_supported
            and query.get("commentsViewMode", [""])[0] == "COMMENTS_VIEW_MODE_OMITTED"
        ):
            value["commentsViewMode"] = "COMMENTS_VIEW_MODE_OMITTED"
        self._json(200, value)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).post_count += 1
        type(self).last_body = body
        type(self).applied = True
        self._json(200, {
            "documentId": DOCUMENT_ID,
            "writeControl": {"writeMode": "SUGGEST", "requiredRevisionId": "revision-2"},
            "suggestionResponses": [
                {"createdSuggestionIds": ["suggestion-delete"]},
                {"createdSuggestionIds": ["suggestion-insert"]},
            ],
            "commentUpdateState": "ALL_SAVED",
        })

    def _json(self, status: int, value: dict) -> None:
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ControlPlaneIntegrationTests(unittest.TestCase):
    def test_real_cli_plan_apply_and_idempotent_replay(self) -> None:
        cli_raw = os.environ.get("LLM_WIKI_CLI")
        if not cli_raw:
            self.skipTest("LLM_WIKI_CLI is not set")
        cli = Path(cli_raw).resolve(strict=True)
        adapter = Path(__file__).resolve().parents[1]
        ApiHandler.applied = False
        ApiHandler.post_count = 0
        ApiHandler.preview_supported = True
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                inputs = root / "inputs"
                outputs = root / "outputs"
                state = root / "state"
                inputs.mkdir()
                outputs.mkdir()
                token = root / "token.json"
                write_private_json(token, {
                    "access_token": "synthetic-access-token",
                    "expires_at": int(time.time()) + 3600,
                })
                spec = inputs / "edit-spec.json"
                write_private_json(spec, {
                    "schema": "google-docs-edit-spec/v1",
                    "edits": [{
                        "tab_id": "tab-synthetic",
                        "find": "Synthetic old phrase.",
                        "replace": "Synthetic new phrase.",
                    }],
                })
                environment = dict(os.environ)
                environment.update({
                    "LLM_WIKI_CONFIG_DIR": str(root / "config"),
                    "GOOGLE_OAUTH_TOKEN_FILE": str(token),
                    "LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state),
                    "LLM_WIKI_GOOGLE_DOCS_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
                })

                def run_raw(*arguments: str) -> subprocess.CompletedProcess[str]:
                    result = subprocess.run(
                        [str(cli), *arguments],
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    return result

                def run(*arguments: str) -> subprocess.CompletedProcess[str]:
                    result = run_raw(*arguments)
                    if result.returncode:
                        self.fail(
                            f"llm-wiki command failed ({result.returncode}): "
                            f"{' '.join(arguments)}\nstdout={result.stdout}\nstderr={result.stderr}"
                    )
                    return result

                run(
                    "adapter", "add", str(adapter),
                    "--read-root", str(inputs),
                    "--read-root", str(outputs),
                    "--write-root", str(outputs),
                    "--remote-resource", RESOURCE,
                    "--env", "GOOGLE_OAUTH_TOKEN_FILE",
                    "--env", "LLM_WIKI_GOOGLE_DOCS_STATE_DIR",
                    "--env", "LLM_WIKI_GOOGLE_DOCS_API_BASE",
                    "--json",
                )
                doctor = json.loads(run("adapter", "doctor", "google-docs-editing", "--json").stdout)
                self.assertEqual(doctor["status"], "healthy")
                plan_request = inputs / "plan-request.json"
                write_private_json(plan_request, {
                    "protocol": "llm-wiki-adapter/v1",
                    "adapter_id": "google-docs-editing",
                    "operation": "plan",
                    "arguments": {"document_resource": RESOURCE, "edit_spec": str(spec)},
                    "output_dir": str(outputs / "plan"),
                    "options": {},
                })
                plan_run = json.loads(run(
                    "adapter", "run", "google-docs-editing",
                    "--request", str(plan_request), "--json",
                ).stdout)
                self.assertTrue(plan_run["summary"]["tracked_changes"])
                plan = outputs / "plan" / "plan.json"
                plan_value = json.loads(plan.read_text())
                plan_sha = sha256_file(plan)
                apply_request = inputs / "apply-request.json"
                write_private_json(apply_request, {
                    "protocol": "llm-wiki-adapter/v1",
                    "adapter_id": "google-docs-editing",
                    "operation": "apply",
                    "arguments": {"document_resource": RESOURCE, "plan": str(plan)},
                    "output_dir": str(outputs / "apply"),
                    "remote_write": {
                        "plan_sha256": plan_sha,
                        "idempotency_key": "control-plane-write-0001",
                        "expected_revision": plan_value["revision_id"],
                    },
                    "options": {},
                })
                receipt = outputs / "apply-receipt.json"
                ApiHandler.preview_supported = False
                failed = run_raw(
                    "adapter", "run", "google-docs-editing",
                    "--request", str(apply_request),
                    "--response", str(outputs / "unsupported-preview-receipt.json"),
                    "--approve-remote-write", plan_sha,
                    "--json",
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertEqual(ApiHandler.post_count, 0)
                self.assertFalse(ApiHandler.applied)

                ApiHandler.preview_supported = True
                result = json.loads(run(
                    "adapter", "run", "google-docs-editing",
                    "--request", str(apply_request),
                    "--response", str(receipt),
                    "--approve-remote-write", plan_sha,
                    "--json",
                ).stdout)
                self.assertEqual(result["remote_write"], {
                    "resource_count": 1,
                    "status": "verified",
                    "verified": True,
                })
                self.assertNotIn(DOCUMENT_ID, json.dumps(result))
                full_receipt = json.loads(receipt.read_text())
                self.assertEqual(full_receipt["remote_receipt"]["status"], "verified")
                self.assertEqual(ApiHandler.last_body["writeControl"], {
                    "writeMode": "SUGGEST",
                    "requiredRevisionId": "revision-1",
                })
                replay_receipt = outputs / "apply-replay-receipt.json"
                run(
                    "adapter", "run", "google-docs-editing",
                    "--request", str(apply_request),
                    "--response", str(replay_receipt),
                    "--approve-remote-write", plan_sha,
                    "--json",
                )
                self.assertEqual(ApiHandler.post_count, 1)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
