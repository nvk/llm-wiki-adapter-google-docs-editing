#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from google_docs_adapter.oauth import (  # noqa: E402
    OAuthError,
    configure_client,
    create_authorization_request,
    disconnect,
    exchange_authorization_code,
    load_client,
    oauth_directory,
    oauth_status,
    store_authorization_response,
    validate_picker_selection,
)
from google_docs_adapter.browser_executor import (  # noqa: E402
    document_id_from_expected_url,
)


class OAuthCallbackServer(HTTPServer):
    expected_state: str
    result: dict[str, str] | None


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: OAuthCallbackServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        parsed = urllib.parse.urlparse(self.path)
        values = urllib.parse.parse_qs(parsed.query)
        state = values.get("state", [""])[0]
        code = values.get("code", [""])[0]
        error = values.get("error", [""])[0]
        picked_file_ids = values.get("picked_file_ids", [""])[0]
        valid = bool(state and state == self.server.expected_state and (code or error))
        if valid:
            self.server.result = {
                "state": state,
                "code": code,
                "error": error,
                "picked_file_ids": picked_file_ids,
            }
            status = 200
            heading = "Authorization received" if code else "Connection cancelled"
            message = "You can close this window and return to LLM Wiki."
        else:
            status = 400
            heading = "Invalid authorization response"
            message = "Close this window and restart the connection flow."
        body = (
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>LLM Wiki</title><style>"
            "body{font:16px system-ui,-apple-system,sans-serif;max-width:34rem;"
            "margin:12vh auto;padding:0 1.5rem;color:#202124}h1{font-size:1.5rem}"
            "p{line-height:1.5;color:#5f6368}</style>"
            f"<h1>{html.escape(heading)}</h1><p>{html.escape(message)}</p></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _print(value: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True))
        return
    if value.get("status") == "ok":
        print(value.get("message", "Google Docs authorization is ready."))
    else:
        print(
            value.get("message", "Google Docs authorization failed."), file=sys.stderr
        )


def _authorize(
    root: Path,
    *,
    no_browser: bool,
    timeout: int,
    document_id: str,
) -> dict[str, Any]:
    client = load_client(root)
    server = OAuthCallbackServer(("127.0.0.1", 0), OAuthCallbackHandler)
    server.result = None
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    authorization_url, state, verifier = create_authorization_request(
        client, redirect_uri, document_id=document_id
    )
    server.expected_state = state
    if no_browser:
        print(authorization_url)
    elif not webbrowser.open(authorization_url, new=1, autoraise=True):
        raise OAuthError("could not open a browser; rerun authorize with --no-browser")
    deadline = time.monotonic() + timeout
    server.timeout = 0.5
    try:
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if result is None:
        raise OAuthError("Google Picker authorization timed out")
    if result.get("state") != state:
        raise OAuthError("Google OAuth state did not match")
    if result.get("error"):
        raise OAuthError("Google OAuth consent was not granted")
    code = result.get("code", "")
    if not code:
        raise OAuthError("Google OAuth returned no authorization code")
    picked_file_ids = validate_picker_selection(
        result.get("picked_file_ids", ""), expected_document_id=document_id
    )
    response = exchange_authorization_code(client, code, verifier, redirect_uri)
    store_authorization_response(response, root)
    return {
        "status": "ok",
        "message": "Google Doc selected with per-file access.",
        "connected": True,
        "selected_file_count": len(picked_file_ids),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure persistent Google Docs OAuth for the llm-wiki adapter"
    )
    parser.add_argument(
        "--oauth-dir",
        help="Private OAuth directory (defaults to ~/.config/llm-wiki/google-docs-editing/oauth)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser(
        "configure", help="Import a downloaded Desktop app OAuth client JSON"
    )
    configure.add_argument("client_json")
    configure.add_argument("--json", action="store_true")

    authorize = subparsers.add_parser(
        "authorize", help="Authorize one exact Google Doc through Google Picker"
    )
    authorize.add_argument("document_url")
    authorize.add_argument("--no-browser", action="store_true")
    authorize.add_argument("--timeout", type=int, default=300)
    authorize.add_argument("--json", action="store_true")

    status = subparsers.add_parser(
        "status", help="Show content-free authorization status"
    )
    status.add_argument("--json", action="store_true")
    logout = subparsers.add_parser(
        "logout", help="Delete the locally stored OAuth token"
    )
    logout.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = oauth_directory(args.oauth_dir)
    try:
        if args.command == "configure":
            configure_client(args.client_json, root)
            result = {
                "status": "ok",
                "message": "Desktop OAuth client configured.",
                "configured": True,
            }
        elif args.command == "authorize":
            if not 30 <= args.timeout <= 900:
                raise OAuthError(
                    "authorization timeout must be between 30 and 900 seconds"
                )
            document_id = document_id_from_expected_url(args.document_url)
            result = _authorize(
                root,
                no_browser=args.no_browser,
                timeout=args.timeout,
                document_id=document_id,
            )
        elif args.command == "status":
            result = {"status": "ok", **oauth_status(root)}
            if args.json:
                print(json.dumps(result, sort_keys=True))
                return 0
            state = "connected" if result["connected"] else "not connected"
            configured = "configured" if result["configured"] else "not configured"
            print(f"Google Docs OAuth: {state}; desktop client: {configured}.")
            return 0
        else:
            removed = disconnect(root)
            result = {
                "status": "ok",
                "message": "Local Google Docs authorization removed."
                if removed
                else "No local Google Docs authorization was stored.",
                "connected": False,
            }
        _print(result, args.json)
        return 0
    except (OAuthError, OSError, ValueError) as exc:
        _print({"status": "error", "message": str(exc)}, args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
