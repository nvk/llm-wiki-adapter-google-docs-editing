from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from google_docs_adapter.auth import (
    DRIVE_FILE_SCOPE,
    TokenProvider,
    _authorization_parameters,
    document_id_from_reference,
    install_client_config,
)
from google_docs_adapter.auth_web import create_local_auth_server
from google_docs_adapter.browser import BrowserSuggestionDriver, browser_profile_path, document_url
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

    def get_document(
        self,
        _document_id: str,
        mode: str,
        comments_view_mode: str | None = None,
    ) -> dict:
        if not self.applied:
            value = document("Alpha beta gamma.\n", "revision-1")
        elif mode == "PREVIEW_WITHOUT_SUGGESTIONS":
            value = document("Alpha beta gamma.\n", "revision-2")
        elif mode == "PREVIEW_SUGGESTIONS_ACCEPTED":
            value = document("Alpha delta gamma.\n", "revision-2")
        else:
            value = document(
                "Alpha delta gamma.\n",
                "revision-2",
                ["suggestion-delete", "suggestion-insert"],
            )
        if comments_view_mode is not None:
            value["commentsViewMode"] = comments_view_mode
        return value

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


class FakeBrowser:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls: list[tuple[str, list[dict]]] = []

    def apply(self, document_id: str, edits: list[dict], before_mutation: object) -> dict:
        self.calls.append((document_id, edits))
        before_mutation()
        self.client.applied = True
        return {
            "transport": "browser-suggesting-ui",
            "mode_verified": True,
            "edit_count": len(edits),
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

    def test_document_reference_parser_and_picker_pin(self) -> None:
        document_id = "SyntheticDocument123"
        self.assertEqual(document_id_from_reference(document_id), document_id)
        self.assertEqual(document_id_from_reference(f"google-docs:{document_id}"), document_id)
        self.assertEqual(
            document_id_from_reference(
                f"https://docs.google.com/document/d/{document_id}/edit?tab=t.0"
            ),
            document_id,
        )
        with self.assertRaises(ValueError):
            document_id_from_reference("https://example.com/not-a-google-doc")
        parameters = _authorization_parameters(
            "synthetic-client",
            "http://127.0.0.1:10000/callback",
            "verifier",
            "state",
            document_id,
        )
        self.assertEqual(parameters["file_ids"], document_id)

    def test_local_oauth_page_is_one_click_and_stores_private_token(self) -> None:
        document_id = "SyntheticDocument123"
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "token.json"
            client_config = {
                "client_id": "synthetic-client",
                "client_secret": "synthetic-secret",
                "token_uri": "https://oauth2.example.test/token",
            }
            server, state = create_local_auth_server(
                token_path, client_config, document_id
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(state.base_url + "/", timeout=5) as response:
                    page = response.read().decode("utf-8")
                    self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertIn("Connect Google Docs", page)
                self.assertNotIn("synthetic-secret", page)
                self.assertNotIn('type="file"', page)
                self.assertIn("Connect with Google", page)

                payload = json.dumps({
                    "csrf_token": state.csrf_token,
                }).encode("utf-8")
                bad_request = urllib.request.Request(
                    state.base_url + "/start",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://invalid.example",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(bad_request, timeout=5)
                self.assertEqual(rejected.exception.code, 403)
                request = urllib.request.Request(
                    state.base_url + "/start",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": state.base_url,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    start = json.loads(response.read().decode("utf-8"))
                self.assertNotIn("synthetic-secret", start["authorization_url"])
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(start["authorization_url"]).query
                )
                self.assertEqual(query["file_ids"], [document_id])
                self.assertEqual(query["scope"], [DRIVE_FILE_SCOPE])
                self.assertEqual(query["code_challenge_method"], ["S256"])
                self.assertIsNotNone(state.flow)
                callback_state = state.flow.state
                callback = state.base_url + "/callback?" + urllib.parse.urlencode({
                    "state": callback_state,
                    "code": "synthetic-code",
                    "picked_file_ids": document_id,
                })
                with mock.patch("google_docs_adapter.auth._post_form", return_value={
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "expires_in": 3600,
                    "scope": DRIVE_FILE_SCOPE,
                    "token_type": "Bearer",
                }) as post_form:
                    with urllib.request.urlopen(callback, timeout=5) as response:
                        result_page = response.read().decode("utf-8")
                self.assertIn("Google Docs connected", result_page)
                self.assertTrue(state.event.wait(2))
                self.assertEqual(state.picked_file_ids, [document_id])
                self.assertIsNone(state.flow)
                post_form.assert_called_once()
                token = json.loads(token_path.read_text(encoding="utf-8"))
                self.assertEqual(token["granted_file_ids"], [document_id])
                self.assertNotIn("client_secret", token)
                if os.name == "posix":
                    self.assertEqual(token_path.stat().st_mode & 0o077, 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_managed_client_install_and_token_refresh_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "downloaded-client.json"
            profile = root / "private" / "oauth-client.json"
            write_private_json(source, {
                "installed": {
                    "client_id": "synthetic-client",
                    "client_secret": "synthetic-secret",
                    "auth_uri": "https://accounts.example.test/auth",
                    "token_uri": "https://oauth2.example.test/token",
                    "project_id": "synthetic-project",
                }
            })
            install_client_config(source, profile)
            installed = json.loads(profile.read_text(encoding="utf-8"))
            self.assertEqual(set(installed["installed"]), {
                "client_id", "client_secret", "token_uri"
            })
            token_path = root / "token.json"
            write_private_json(token_path, {
                "access_token": "expired-access",
                "refresh_token": "synthetic-refresh",
                "expires_at": 0,
                "scope": DRIVE_FILE_SCOPE,
                "token_type": "Bearer",
                "token_uri": "https://oauth2.example.test/token",
                "client_id": "synthetic-client",
                "granted_file_ids": ["SyntheticDocument123"],
            })
            with mock.patch.dict(os.environ, {
                "GOOGLE_OAUTH_CLIENT_FILE": str(profile)
            }), mock.patch("google_docs_adapter.auth._post_form", return_value={
                "access_token": "refreshed-access",
                "expires_in": 3600,
                "scope": DRIVE_FILE_SCOPE,
                "token_type": "Bearer",
            }) as post_form:
                self.assertEqual(TokenProvider(token_path).access_token(), "refreshed-access")
            fields = post_form.call_args.args[1]
            self.assertEqual(fields["client_secret"], "synthetic-secret")
            refreshed = json.loads(token_path.read_text(encoding="utf-8"))
            self.assertNotIn("client_secret", refreshed)

    def test_self_test_and_utf16_indexing(self) -> None:
        response = execute({"operation": "self-test"})
        self.assertEqual(response["status"], "ok")
        value = document("A 🌎 B\n", "revision")
        index = tab_text_indexes(value)["tab-1"]
        self.assertEqual(index.locate("🌎")[2:], (3, 5))

    def test_browser_profile_is_external_and_document_url_preserves_tab(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with mock.patch.dict(
                os.environ,
                {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state)},
                clear=False,
            ):
                self.assertEqual(
                    browser_profile_path(),
                    state.resolve(strict=False) / "browser-profile",
                )
        self.assertEqual(
            document_url("SyntheticDocument123", "t.0"),
            "https://docs.google.com/document/d/SyntheticDocument123/edit?tab=t.0",
        )

    def test_browser_replace_confirms_exact_match_before_mutation(self) -> None:
        driver = BrowserSuggestionDriver(Path("/synthetic/browser-profile"))
        page = mock.MagicMock()
        dialog = mock.MagicMock()
        find_input = mock.MagicMock()
        replace_input = mock.MagicMock()
        match_case = mock.MagicMock()
        option = mock.MagicMock()
        replace_button = mock.MagicMock()
        find_input.count.return_value = 1
        replace_input.count.return_value = 1
        match_case.first.is_checked.return_value = False
        option.first.is_checked.return_value = False
        dialog.inner_text.return_value = "Find and replace\n1 of 1\nReplace"

        def locate(selector: str) -> object:
            return {
                "input.docs-findandreplacedialog-find-input": find_input,
                "input.docs-findandreplacedialog-replace-input": replace_input,
                ".docs-findandreplacedialog-replace-button": replace_button,
            }[selector]

        def by_role(role: str, name: object = None) -> object:
            if role == "checkbox" and "Match case" in str(name):
                return match_case
            if role == "checkbox":
                return option
            if role == "button":
                return replace_button
            raise AssertionError(role)

        dialog.locator.side_effect = locate
        dialog.get_by_role.side_effect = by_role
        before_click = mock.Mock()
        with mock.patch.object(driver, "_open_find_replace", return_value=dialog), mock.patch.object(
            driver,
            "_visible",
            side_effect=lambda locator: locator in {match_case, replace_button},
        ):
            driver._replace_unique(
                page,
                {"find": "Synthetic old", "replace": "Synthetic new"},
                before_click,
            )
        match_case.first.check.assert_called_once_with()
        find_input.fill.assert_called_once_with("Synthetic old")
        replace_input.fill.assert_called_once_with("Synthetic new")
        before_click.assert_called_once_with()
        replace_button.first.click.assert_called_once_with()

    def test_apply_request_helper_runs_from_outside_repository(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        helper = repository / "scripts" / "make_apply_request.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            request = root / "input" / "apply.json"
            write_private_json(plan, {
                "schema": "google-docs-suggestion-plan/v2",
                "write_transport": "browser-suggesting-ui",
                "document_resource": RESOURCE,
                "revision_id": "synthetic-revision",
            })
            result = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "--plan", str(plan),
                    "--output-dir", str(root / "output"),
                    "--idempotency-key", "synthetic-helper-key",
                    "--request", str(request),
                ],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), sha256_file(plan))
            value = json.loads(request.read_text(encoding="utf-8"))
            self.assertEqual(value["remote_write"]["plan_sha256"], sha256_file(plan))

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
            browser = FakeBrowser(client)
            plan_response = execute({
                "operation": "plan",
                "arguments": {"document_resource": RESOURCE, "edit_spec": str(edit_spec)},
                "output_dir": str(output / "plan"),
            }, client)
            self.assertEqual(plan_response["status"], "ok")
            plan = output / "plan" / "plan.json"
            value = json.loads(plan.read_text())
            self.assertEqual(value["revision_id"], "revision-1")
            self.assertEqual(value["edits"][0]["start_index"], 7)
            self.assertEqual(value["edits"][0]["end_index"], 11)
            self.assertNotIn("requests", value)
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
                apply_response = execute(apply_request, client, browser)
                replay_response = execute(apply_request, client, browser)
            self.assertEqual(apply_response["status"], "ok")
            self.assertEqual(replay_response, apply_response)
            self.assertEqual(client.batch_calls, [])
            self.assertEqual(len(browser.calls), 1)
            receipt = apply_response["remote_receipt"]
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["verification"]["suggestion_count"], 2)
            self.assertEqual(
                receipt["verification"]["write_transport"],
                "browser-suggesting-ui",
            )
            self.assertTrue(receipt["verification"]["browser_mode_verified"])
            receipt_path = output / "receipt.json"
            write_private_json(receipt_path, apply_response)
            verify_response = execute({
                "operation": "verify",
                "arguments": {"document_resource": RESOURCE, "receipt": str(receipt_path)},
                "output_dir": str(output / "verify"),
            }, client)
            self.assertEqual(verify_response["status"], "ok")
            self.assertTrue(verify_response["summary"]["verified"])

    def test_browser_failure_before_mutation_can_retry(self) -> None:
        class UnauthenticatedBrowser:
            def apply(self, _document_id: str, _edits: list[dict], _before_mutation: object) -> dict:
                raise RuntimeError("editor did not become ready; no edit was sent")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"tab_id": "tab-1", "find": "beta", "replace": "delta"}],
            })
            client = FakeClient()
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
                    "idempotency_key": "synthetic-preview-missing",
                    "expected_revision": "revision-1",
                },
            }
            with mock.patch.dict(os.environ, {"LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state")}):
                response = execute(request, client, UnauthenticatedBrowser())
                retry = execute(request, client, FakeBrowser(client))
            self.assertEqual(response["status"], "error")
            self.assertIn("did not become ready", response["errors"][0])
            self.assertIn("no edit was sent", response["errors"][0])
            self.assertEqual(client.batch_calls, [])
            self.assertEqual(retry["status"], "ok")

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
            self.assertIn("no unresolved existing suggestions", response["errors"][0])

    def test_pending_partial_browser_write_refuses_duplicate_retry(self) -> None:
        class PartialClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.partial = False

            def get_document(
                self,
                _document_id: str,
                mode: str,
                comments_view_mode: str | None = None,
            ) -> dict:
                if not self.partial:
                    return super().get_document(_document_id, mode, comments_view_mode)
                if mode == "PREVIEW_WITHOUT_SUGGESTIONS":
                    return document("Alpha beta gamma.\n", "revision-2")
                if mode == "PREVIEW_SUGGESTIONS_ACCEPTED":
                    return document("Alpha epsilon gamma.\n", "revision-2")
                return document(
                    "Alpha epsilon gamma.\n",
                    "revision-2",
                    ["partial-suggestion"],
                )

        class PartialBrowser:
            def __init__(self, client: PartialClient) -> None:
                self.client = client
                self.calls = 0

            def apply(self, _document_id: str, _edits: list[dict], before_mutation: object) -> dict:
                self.calls += 1
                before_mutation()
                self.client.partial = True
                raise RuntimeError("synthetic browser interrupted after first suggestion")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"tab_id": "tab-1", "find": "beta", "replace": "delta"}],
            })
            client = PartialClient()
            browser = PartialBrowser(client)
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
                response = execute(request, client, browser)
                replay = execute(request, client, browser)
            self.assertEqual(response["status"], "error")
            self.assertIn("interrupted", response["errors"][0])
            self.assertEqual(replay["status"], "error")
            self.assertIn("pending or partial", replay["errors"][0])
            self.assertEqual(browser.calls, 1)


if __name__ == "__main__":
    unittest.main()
