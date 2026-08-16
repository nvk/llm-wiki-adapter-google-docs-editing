from __future__ import annotations

import json
import os
import socket
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
from google_docs_adapter.document import tab_text_indexes
from google_docs_adapter.extension_bridge import (
    BRIDGE_PROTOCOL,
    ExtensionSuggestionDriver,
    extension_root,
    native_socket_path,
)
from google_docs_adapter.native_messaging import (
    NATIVE_HOST_NAME,
    connector_status,
    extension_id_from_manifest,
    install_native_host,
    read_native_message,
    write_native_message,
)
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


class FakeExtension:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls: list[tuple[str, list[dict]]] = []

    def apply(self, document_id: str, edits: list[dict], before_mutation: object) -> dict:
        self.calls.append((document_id, edits))
        before_mutation()
        self.client.applied = True
        return {
            "transport": "chrome-native-messaging-suggesting-ui",
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

    def test_extension_manifest_is_tool_code_with_narrow_hosts(self) -> None:
        manifest = json.loads((extension_root() / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertIn("debugger", manifest["permissions"])
        self.assertIn("nativeMessaging", manifest["permissions"])
        self.assertNotIn("activeTab", manifest["permissions"])
        self.assertEqual(manifest["host_permissions"], ["https://docs.google.com/*"])
        self.assertNotIn("content_scripts", manifest)
        self.assertEqual(extension_id_from_manifest(), "mahpanmnmopoleaphpljfngdlhgjmnob")
        sidepanel = (extension_root() / "sidepanel.html").read_text(encoding="utf-8")
        worker = (extension_root() / "service-worker.js").read_text(encoding="utf-8")
        self.assertNotIn("Apply as suggestions", sidepanel)
        self.assertIn("No interaction is required", sidepanel)
        self.assertIn("chrome.runtime.connectNative", worker)
        self.assertLess(
            worker.index("connectNativeBridge();"),
            worker.index("await chrome.sidePanel.setPanelBehavior"),
        )
        self.assertIn('const NATIVE_HOST = "net.llmwiki.google_docs"', worker)
        self.assertIn("activateDocumentTab", worker)
        self.assertIn("chrome.tabs.create", worker)
        self.assertIn("chrome.windows.update", worker)
        self.assertNotIn("127.0.0.1", worker)
        self.assertNotIn("bridgeFetch", worker)
        self.assertIn("findReplaceContextExpression", worker)
        self.assertIn("findReplaceMenuItemExpression", worker)
        self.assertIn("focusFindReplaceMenuItemExpression", worker)
        self.assertIn("activateFindReplaceMenuItem", worker)
        self.assertIn("focusReplaceFromFindInput", worker)
        self.assertIn("dialogEditable.length === 2", worker)
        self.assertIn("waitForFindReplaceField", worker)
        self.assertIn('new InputEvent("input"', worker)
        self.assertIn("suggestingDiagnosticsExpression", worker)
        self.assertIn(
            'const modifiers = platform.toLowerCase().includes("mac") ? 13 : 11;',
            worker,
        )
        self.assertIn('[role="menuitemradio"]', worker)
        self.assertIn("Accessibility.getFullAXTree", worker)
        self.assertIn("DOM.getBoxModel", worker)
        self.assertIn('command(tabId, "DOM.focus"', worker)
        self.assertIn("Google Docs did not accept the exact ${kind} field.", worker)
        self.assertIn("LAST_NORMAL_WINDOW_KEY", worker)
        self.assertIn("chrome.storage.session", worker)
        self.assertIn("focusedDocumentTab", worker)
        self.assertIn('windowTypes: ["normal"]', worker)
        self.assertNotIn("matches[0]", worker)
        self.assertNotIn('visibleElementExpression(".docs-findandreplacedialog")', worker)
        self.assertNotIn("<all_urls>", json.dumps(manifest))

    def test_native_host_installer_uses_stable_extension_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_dir = root / "NativeMessagingHosts"
            with mock.patch.dict(os.environ, {
                "LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(root / "state"),
            }):
                result = install_native_host(Path(__file__).resolve().parents[1], install_dir)
                status = connector_status(install_dir)
            self.assertEqual(result["extension_id"], extension_id_from_manifest())
            manifest = json.loads(
                (install_dir / f"{NATIVE_HOST_NAME}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["allowed_origins"], [
                f"chrome-extension://{extension_id_from_manifest()}/"
            ])
            self.assertEqual(manifest["type"], "stdio")
            self.assertNotIn("token", json.dumps(manifest).lower())
            self.assertTrue(status["installed"])
            self.assertFalse(status["connected"])
            wrapper = result["wrapper_path"].read_text(encoding="utf-8")
            self.assertIn("LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET=", wrapper)
            self.assertIn(str(result["socket_path"]), wrapper)
            wrong_socket = root / "wrong-environment.sock"
            environment = dict(os.environ)
            environment["LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET"] = str(wrong_socket)
            process = subprocess.Popen(
                [
                    str(result["wrapper_path"]),
                    f"chrome-extension://{extension_id_from_manifest()}/",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(
                    read_native_message(process.stdout),
                    {"protocol": BRIDGE_PROTOCOL, "type": "ready"},
                )
                self.assertTrue(result["socket_path"].is_socket())
                self.assertFalse(wrong_socket.exists())
            finally:
                if process.stdin:
                    process.stdin.close()
                process.wait(timeout=5)
                if process.returncode not in {0, None}:
                    stderr = process.stderr.read().decode("utf-8") if process.stderr else ""
                    self.fail(f"installed native host exited {process.returncode}: {stderr}")
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
            if os.name == "posix":
                self.assertEqual(result["manifest_path"].stat().st_mode & 0o077, 0)
                self.assertEqual(result["wrapper_path"].stat().st_mode & 0o077, 0)

    def test_long_external_state_uses_a_private_short_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / ("long-state-segment-" * 8)
            with mock.patch.dict(os.environ, {
                "LLM_WIKI_GOOGLE_DOCS_STATE_DIR": str(state),
            }, clear=False):
                socket_path = native_socket_path()
                self.assertLessEqual(len(os.fsencode(str(socket_path))), 90)
                self.assertEqual(socket_path.parent.parent, Path("/tmp"))

    def test_native_extension_bridge_authorizes_boundary_and_returns_content_free_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            socket_path = Path(temporary) / "bridge.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            before_mutation = mock.Mock()
            extension_error: list[Exception] = []

            def emulate_host() -> None:
                try:
                    connection, _ = server.accept()
                    stream = connection.makefile("rwb")
                    job_message = json.loads(stream.readline())
                    job = job_message["job"]
                    stream.write(json.dumps({
                        "protocol": BRIDGE_PROTOCOL,
                        "type": "before-mutation",
                        "job_id": job["job_id"],
                    }).encode("utf-8") + b"\n")
                    stream.flush()
                    boundary = json.loads(stream.readline())
                    self.assertTrue(boundary["authorized"])
                    stream.write(json.dumps({
                        "protocol": BRIDGE_PROTOCOL,
                        "type": "result",
                        "result": {
                            "job_id": job["job_id"],
                            "status": "ok",
                            "mode_verified": True,
                            "edit_count": len(job["edits"]),
                            "mutation_started": True,
                        },
                    }).encode("utf-8") + b"\n")
                    stream.flush()
                    stream.close()
                    connection.close()
                except Exception as exc:  # pragma: no cover - surfaced below
                    extension_error.append(exc)

            thread = threading.Thread(target=emulate_host, daemon=True)
            thread.start()

            driver = ExtensionSuggestionDriver(
                "a" * 64,
                timeout_seconds=10,
                socket_path=socket_path,
            )
            result = driver.apply(
                "SyntheticDocument123",
                [{"tab_id": "tab-1", "find": "old", "replace": "new"}],
                before_mutation,
            )
            thread.join(timeout=5)
            server.close()
            self.assertEqual(extension_error, [])
            before_mutation.assert_called_once_with()
            self.assertEqual(result, {
                "transport": "chrome-native-messaging-suggesting-ui",
                "mode_verified": True,
                "edit_count": 1,
            })
            self.assertNotIn("old", json.dumps(result))

    @unittest.skipIf(not hasattr(socket, "AF_UNIX"), "native relay requires Unix sockets")
    def test_native_host_relays_extension_and_agent_messages(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        origin = f"chrome-extension://{extension_id_from_manifest()}/"
        with tempfile.TemporaryDirectory() as temporary:
            socket_path = Path(temporary) / "n.sock"
            environment = dict(os.environ)
            environment["LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET"] = str(socket_path)
            process = subprocess.Popen(
                [sys.executable, str(repository / "adapter.py"), "native-host", origin],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                assert process.stdout is not None
                assert process.stdin is not None
                ready = read_native_message(process.stdout)
                self.assertEqual(ready, {"protocol": BRIDGE_PROTOCOL, "type": "ready"})
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.connect(str(socket_path))
                stream = connection.makefile("rwb")
                job = {
                    "protocol": BRIDGE_PROTOCOL,
                    "type": "job",
                    "job": {"job_id": "synthetic-job"},
                }
                stream.write(json.dumps(job).encode("utf-8") + b"\n")
                stream.flush()
                self.assertEqual(read_native_message(process.stdout), job)
                boundary = {
                    "protocol": BRIDGE_PROTOCOL,
                    "type": "before-mutation",
                    "job_id": "synthetic-job",
                }
                write_native_message(process.stdin, boundary)
                self.assertEqual(json.loads(stream.readline()), boundary)
                authorization = {
                    "protocol": BRIDGE_PROTOCOL,
                    "type": "mutation-authorized",
                    "job_id": "synthetic-job",
                    "authorized": True,
                }
                stream.write(json.dumps(authorization).encode("utf-8") + b"\n")
                stream.flush()
                self.assertEqual(read_native_message(process.stdout), authorization)
                result = {
                    "protocol": BRIDGE_PROTOCOL,
                    "type": "result",
                    "result": {"job_id": "synthetic-job", "status": "ok"},
                }
                write_native_message(process.stdin, result)
                self.assertEqual(json.loads(stream.readline()), result)
                stream.close()
                connection.close()
            finally:
                if process.stdin:
                    process.stdin.close()
                process.wait(timeout=5)
                if process.returncode not in {0, None}:
                    stderr = process.stderr.read().decode("utf-8") if process.stderr else ""
                    self.fail(f"native host exited {process.returncode}: {stderr}")
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    def test_apply_request_helper_runs_from_outside_repository(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        helper = repository / "scripts" / "make_apply_request.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "plan.json"
            request = root / "input" / "apply.json"
            write_private_json(plan, {
                "schema": "google-docs-suggestion-plan/v4",
                "write_transport": "chrome-native-messaging-suggesting-ui",
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
            extension = FakeExtension(client)
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
                apply_response = execute(apply_request, client, extension)
                replay_response = execute(apply_request, client, extension)
            self.assertEqual(apply_response["status"], "ok")
            self.assertEqual(replay_response, apply_response)
            self.assertEqual(client.batch_calls, [])
            self.assertEqual(len(extension.calls), 1)
            receipt = apply_response["remote_receipt"]
            self.assertEqual(receipt["status"], "verified")
            self.assertEqual(receipt["verification"]["suggestion_count"], 2)
            self.assertEqual(
                receipt["verification"]["write_transport"],
                "chrome-native-messaging-suggesting-ui",
            )
            self.assertTrue(receipt["verification"]["extension_mode_verified"])
            receipt_path = output / "receipt.json"
            write_private_json(receipt_path, apply_response)
            verify_response = execute({
                "operation": "verify",
                "arguments": {"document_resource": RESOURCE, "receipt": str(receipt_path)},
                "output_dir": str(output / "verify"),
            }, client)
            self.assertEqual(verify_response["status"], "ok")
            self.assertTrue(verify_response["summary"]["verified"])

    def test_extension_failure_before_mutation_can_retry(self) -> None:
        class UnavailableExtension:
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
                response = execute(request, client, UnavailableExtension())
                retry = execute(request, client, FakeExtension(client))
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

    def test_pending_partial_extension_write_refuses_duplicate_retry(self) -> None:
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

        class PartialExtension:
            def __init__(self, client: PartialClient) -> None:
                self.client = client
                self.calls = 0

            def apply(self, _document_id: str, _edits: list[dict], before_mutation: object) -> dict:
                self.calls += 1
                before_mutation()
                self.client.partial = True
                raise RuntimeError("synthetic extension interrupted after first suggestion")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.json"
            write_private_json(spec, {
                "schema": "google-docs-edit-spec/v1",
                "edits": [{"tab_id": "tab-1", "find": "beta", "replace": "delta"}],
            })
            client = PartialClient()
            extension = PartialExtension(client)
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
                response = execute(request, client, extension)
                replay = execute(request, client, extension)
            self.assertEqual(response["status"], "error")
            self.assertIn("interrupted", response["errors"][0])
            self.assertEqual(replay["status"], "error")
            self.assertIn("pending or partial", replay["errors"][0])
            self.assertEqual(extension.calls, 1)


if __name__ == "__main__":
    unittest.main()
