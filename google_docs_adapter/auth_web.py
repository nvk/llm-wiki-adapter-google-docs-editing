from __future__ import annotations

import base64
import html
import http.server
import json
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import (
    AUTHORIZATION_ENDPOINT,
    _authorization_parameters,
    complete_authorization,
)

MAX_REQUEST_BYTES = 4096


@dataclass
class _Flow:
    verifier: str
    state: str
    redirect_uri: str


@dataclass
class LocalAuthState:
    token_path: Path
    client_config: dict[str, Any]
    expected_document_id: str | None
    csrf_token: str
    page_nonce: str
    base_url: str = ""
    flow: _Flow | None = None
    picked_file_ids: list[str] | None = None
    error: str | None = None
    event: threading.Event | None = None


def _page(state: LocalAuthState) -> bytes:
    pinned = (
        "The Google Picker will be restricted to the requested document."
        if state.expected_document_id
        else "Google Picker will ask you to select exactly one native Google Doc."
    )
    csrf_json = json.dumps(state.csrf_token)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Google Docs</title>
  <style nonce="{state.page_nonce}">
    :root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; background: #0b1020; color: #edf2ff; }}
    main {{ max-width: 680px; margin: 8vh auto; padding: 0 24px; }}
    .card {{ background: #141b31; border: 1px solid #2a3558; border-radius: 18px; padding: 28px; box-shadow: 0 24px 80px #0007; }}
    .eyebrow {{ color: #91a7ff; font-size: 13px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(28px, 5vw, 42px); line-height: 1.05; }}
    p, li {{ color: #cbd5ef; line-height: 1.55; }}
    .privacy {{ background: #0d1428; border-radius: 12px; padding: 14px 16px; margin: 20px 0; }}
    button {{ margin-top: 16px; width: 100%; padding: 14px 18px; border: 0; border-radius: 12px; background: #6d7cff; color: white; font: inherit; font-weight: 800; cursor: pointer; }}
    button:disabled {{ cursor: wait; opacity: .65; }}
    #status {{ min-height: 24px; color: #ffcf7d; }}
    a {{ color: #aeb9ff; }}
    code {{ color: #bde0ff; }}
  </style>
</head>
<body>
<main>
  <section class="card">
    <div class="eyebrow">Private llm-wiki adapter</div>
    <h1>Connect Google Docs</h1>
    <p>{html.escape(pinned)}</p>
    <div class="privacy">
      This page is served only on <code>127.0.0.1</code>. The adapter owner has
      already provisioned the Google app identity. Your Google token and
      selected document grant remain private on this machine.
    </div>
    <p>Google will ask you to sign in, approve access to the selected file, and
    confirm the document in Picker. The adapter requests only
    <code>drive.file</code> access.</p>
    <form id="connect">
      <button id="submit" type="submit">Connect with Google</button>
      <p id="status" role="status" aria-live="polite"></p>
    </form>
  </section>
</main>
<script nonce="{state.page_nonce}">
  const csrfToken = {csrf_json};
  const form = document.getElementById('connect');
  const button = document.getElementById('submit');
  const status = document.getElementById('status');
  form.addEventListener('submit', async (event) => {{
    event.preventDefault();
    button.disabled = true;
    status.textContent = 'Opening Google authorization…';
    try {{
      const response = await fetch('/start', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{csrf_token: csrfToken}}),
        cache: 'no-store',
      }});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not start Google authorization.');
      status.textContent = 'Opening Google authorization…';
      window.location.assign(result.authorization_url);
    }} catch (error) {{
      status.textContent = error.message;
      button.disabled = false;
    }}
  }});
</script>
</body>
</html>
""".encode("utf-8")


def _result_page(success: bool, message: str) -> bytes:
    title = "Google Docs connected" if success else "Authorization failed"
    detail = (
        "The private token is stored. You may close this page and return to the agent."
        if success
        else message
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p></main></body></html>
""".encode("utf-8")


def _handler_for(state: LocalAuthState) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _headers(self, status: int, media_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; "
                f"style-src 'nonce-{state.page_nonce}'; script-src 'nonce-{state.page_nonce}'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()

        def _send_bytes(self, status: int, media_type: str, body: bytes) -> None:
            self._headers(status, media_type, len(body))
            self.wfile.write(body)

        def _send_json(self, status: int, value: dict[str, Any]) -> None:
            self._send_bytes(
                status,
                "application/json; charset=utf-8",
                (json.dumps(value, sort_keys=True) + "\n").encode("utf-8"),
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send_bytes(200, "text/html; charset=utf-8", _page(state))
                return
            if parsed.path != "/callback":
                self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n")
                return
            if state.flow is None:
                self._send_bytes(400, "text/html; charset=utf-8", _result_page(False, "No authorization is in progress."))
                return
            flow = state.flow
            query = urllib.parse.parse_qs(parsed.query)
            returned_state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            error = query.get("error", [""])[0]
            picked_file_ids = [
                value for value in query.get("picked_file_ids", [""])[0].split(",") if value
            ]
            if returned_state != flow.state:
                error = "invalid OAuth state"
            if not error and not code:
                error = "missing authorization code"
            try:
                if error:
                    raise RuntimeError(f"Google authorization failed: {error}")
                state.picked_file_ids = complete_authorization(
                    state.client_config,
                    state.token_path,
                    flow.verifier,
                    flow.redirect_uri,
                    code,
                    picked_file_ids,
                    state.expected_document_id,
                )
                body = _result_page(True, "")
                status = 200
            except Exception as exc:
                state.error = str(exc)
                body = _result_page(False, state.error)
                status = 400
            finally:
                state.flow = None
                if state.event is not None:
                    state.event.set()
            self._send_bytes(status, "text/html; charset=utf-8", body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/start":
                self._send_json(404, {"error": "not found"})
                return
            if self.headers.get("Origin") != state.base_url:
                self._send_json(403, {"error": "invalid local origin"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send_json(415, {"error": "expected application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send_json(413, {"error": "local authorization request is too large or empty"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("csrf_token") != state.csrf_token:
                    raise ValueError("invalid local session token")
                if state.flow is not None:
                    raise ValueError("authorization is already in progress")
                verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
                oauth_state = secrets.token_urlsafe(32)
                redirect_uri = state.base_url + "/callback"
                state.flow = _Flow(verifier, oauth_state, redirect_uri)
                parameters = _authorization_parameters(
                    str(state.client_config["client_id"]),
                    redirect_uri,
                    verifier,
                    oauth_state,
                    state.expected_document_id,
                )
                authorization_url = AUTHORIZATION_ENDPOINT + "?" + urllib.parse.urlencode(parameters)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"authorization_url": authorization_url})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def create_local_auth_server(
    token_path: Path,
    client_config: dict[str, Any],
    expected_document_id: str | None = None,
) -> tuple[http.server.HTTPServer, LocalAuthState]:
    state = LocalAuthState(
        token_path=token_path.resolve(strict=False),
        client_config=client_config,
        expected_document_id=expected_document_id,
        csrf_token=secrets.token_urlsafe(32),
        page_nonce=secrets.token_urlsafe(24),
        event=threading.Event(),
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), _handler_for(state))
    state.base_url = f"http://127.0.0.1:{server.server_port}"
    return server, state


def _open_system_browser(url: str) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["open", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError:
            pass
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()


def authorize_web(
    client_config: dict[str, Any],
    token_path: Path,
    timeout: int = 600,
    expected_document_id: str | None = None,
    open_browser: bool = True,
) -> list[str]:
    server, state = create_local_auth_server(token_path, client_config, expected_document_id)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Local Google Docs authorization page:", flush=True)
    print(state.base_url, flush=True)
    if open_browser:
        _open_system_browser(state.base_url)
    try:
        if state.event is None or not state.event.wait(timeout):
            raise RuntimeError("local Google authorization timed out")
        if state.error:
            raise RuntimeError(state.error)
        if not state.picked_file_ids:
            raise RuntimeError("Google authorization completed without a selected document")
        return state.picked_file_ids
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
