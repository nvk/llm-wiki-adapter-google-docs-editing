from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import load_json, write_private_json

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def default_token_path() -> Path:
    return Path.home() / ".config" / "llm-wiki" / "google-docs-editing" / "token.json"


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"OAuth token request failed: {exc}") from exc
    if not isinstance(value, dict) or "access_token" not in value:
        raise RuntimeError("OAuth token response did not contain an access token")
    return value


def _client_config_value(root: dict[str, Any]) -> dict[str, Any]:
    value = root.get("installed")
    if not isinstance(value, dict):
        raise ValueError("OAuth client configuration must contain an installed application")
    if not isinstance(value.get("client_id"), str) or not value["client_id"].strip():
        raise ValueError("OAuth client configuration is missing a client ID")
    if not isinstance(value.get("client_secret"), str) or not value["client_secret"].strip():
        raise ValueError("OAuth client configuration is missing client credentials")
    return value


def _client_config(path: Path) -> dict[str, Any]:
    return _client_config_value(load_json(path, "OAuth client configuration"))


def document_id_from_reference(value: str) -> str:
    raw = value.strip()
    if raw.startswith("google-docs:"):
        raw = raw.removeprefix("google-docs:")
    elif raw.startswith("https://docs.google.com/document/d/"):
        raw = raw.removeprefix("https://docs.google.com/document/d/").split("/", 1)[0]
    if not raw or len(raw) > 256 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for character in raw):
        raise ValueError("document must be a Google Docs URL, google-docs resource, or document ID")
    return raw


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}
    expected_state = ""
    event = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        error = query.get("error", [""])[0]
        picked_file_ids = query.get("picked_file_ids", [""])[0]
        if state != self.expected_state:
            error = "invalid OAuth state"
        type(self).result = {
            "code": code,
            "error": error,
            "picked_file_ids": picked_file_ids,
        }
        body = b"Authorization received. You may close this tab."
        self.send_response(200 if code and not error else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).event.set()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _authorization_parameters(
    client_id: str,
    redirect_uri: str,
    verifier: str,
    state: str,
    document_id: str | None = None,
) -> dict[str, str]:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode("ascii")
    parameters = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_FILE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "trigger_onepick": "true",
        "allow_multiple": "false",
        "mimetypes": "application/vnd.google-apps.document",
    }
    if document_id:
        parameters["file_ids"] = document_id
    return parameters


def complete_authorization(
    config: dict[str, Any],
    token_path: Path,
    verifier: str,
    redirect_uri: str,
    code: str,
    picked_file_ids: list[str],
    expected_document_id: str | None = None,
) -> list[str]:
    if len(picked_file_ids) != 1:
        raise RuntimeError("Google Picker did not return exactly one Google Docs file")
    if expected_document_id and picked_file_ids != [expected_document_id]:
        raise RuntimeError("Google Picker returned a different document than the requested document")
    token_endpoint = str(config.get("token_uri") or DEFAULT_TOKEN_ENDPOINT)
    token = _post_form(
        token_endpoint,
        {
            "client_id": str(config["client_id"]),
            "client_secret": str(config["client_secret"]),
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    expires_in = int(token.get("expires_in", 3600))
    prior_file_ids: set[str] = set()
    prior_refresh_token: str | None = None
    if token_path.is_file():
        prior = load_json(token_path, "existing OAuth token")
        if prior.get("client_id") == config["client_id"]:
            if isinstance(prior.get("refresh_token"), str):
                prior_refresh_token = prior["refresh_token"]
            values = prior.get("granted_file_ids", [])
            if isinstance(values, list):
                prior_file_ids.update(str(value) for value in values if isinstance(value, str))
    stored = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or prior_refresh_token,
        "expires_at": int(time.time()) + expires_in,
        "scope": token.get("scope", DRIVE_FILE_SCOPE),
        "token_type": token.get("token_type", "Bearer"),
        "token_uri": token_endpoint,
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "granted_file_ids": sorted(prior_file_ids | set(picked_file_ids)),
    }
    if not stored["refresh_token"]:
        raise RuntimeError("OAuth response did not include a refresh token")
    write_private_json(token_path, stored)
    return picked_file_ids


def authorize(
    client_secrets: Path,
    token_path: Path,
    timeout: int = 300,
    expected_document_id: str | None = None,
) -> list[str]:
    config = _client_config(client_secrets)
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    _CallbackHandler.result = {}
    _CallbackHandler.expected_state = state
    _CallbackHandler.event = threading.Event()
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    parameters = _authorization_parameters(
        str(config["client_id"]), redirect_uri, verifier, state, expected_document_id
    )
    url = AUTHORIZATION_ENDPOINT + "?" + urllib.parse.urlencode(parameters)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Open this URL if the browser does not open automatically:")
    print(url)
    webbrowser.open(url)
    if not _CallbackHandler.event.wait(timeout):
        server.shutdown()
        raise RuntimeError("OAuth authorization timed out")
    server.shutdown()
    result = _CallbackHandler.result
    if result.get("error") or not result.get("code"):
        raise RuntimeError(f"OAuth authorization failed: {result.get('error', 'missing code')}")
    picked_file_ids = [
        value for value in result.get("picked_file_ids", "").split(",") if value
    ]
    return complete_authorization(
        config,
        token_path,
        verifier,
        redirect_uri,
        result["code"],
        picked_file_ids,
        expected_document_id,
    )


@dataclass
class TokenProvider:
    path: Path

    @classmethod
    def from_environment(cls) -> "TokenProvider":
        raw = os.environ.get("GOOGLE_OAUTH_TOKEN_FILE")
        path = (
            Path(raw).expanduser().resolve(strict=False)
            if raw
            else default_token_path().resolve(strict=False)
        )
        if not path.is_file():
            raise RuntimeError(
                "Google OAuth token does not exist; run adapter.py auth first"
            )
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise RuntimeError("GOOGLE_OAUTH_TOKEN_FILE must not be group/world accessible")
        return cls(path)

    def access_token(self) -> str:
        token = load_json(self.path, "OAuth token")
        access_token = token.get("access_token")
        if access_token and int(token.get("expires_at", 0)) > int(time.time()) + 60:
            return str(access_token)
        required = ("refresh_token", "client_id", "client_secret")
        if any(not token.get(field) for field in required):
            raise RuntimeError("OAuth token cannot be refreshed")
        refreshed = _post_form(
            str(token.get("token_uri") or DEFAULT_TOKEN_ENDPOINT),
            {
                "client_id": str(token["client_id"]),
                "client_secret": str(token["client_secret"]),
                "refresh_token": str(token["refresh_token"]),
                "grant_type": "refresh_token",
            },
        )
        token["access_token"] = refreshed["access_token"]
        token["expires_at"] = int(time.time()) + int(refreshed.get("expires_in", 3600))
        token["scope"] = refreshed.get("scope", token.get("scope", DRIVE_FILE_SCOPE))
        token["token_type"] = refreshed.get("token_type", token.get("token_type", "Bearer"))
        write_private_json(self.path, token)
        return str(token["access_token"])
