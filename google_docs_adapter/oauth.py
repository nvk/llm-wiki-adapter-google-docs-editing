from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .storage import load_json, write_private_json

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
DOWNLOADED_AUTHORIZATION_ENDPOINTS = {
    AUTHORIZATION_ENDPOINT,
    "https://accounts.google.com/o/oauth2/auth",
}
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
APPS_SCRIPT_EXTERNAL_REQUEST_SCOPE = (
    "https://www.googleapis.com/auth/script.external_request"
)
OAUTH_SCOPES = (DRIVE_FILE_SCOPE, APPS_SCRIPT_EXTERNAL_REQUEST_SCOPE)
ACCESS_TOKEN_ENV = "LLM_WIKI_GOOGLE_DOCS_ACCESS_TOKEN"
OAUTH_DIR_ENV = "LLM_WIKI_GOOGLE_DOCS_OAUTH_DIR"
CLIENT_SCHEMA = "google-docs-oauth-client/v1"
TOKEN_SCHEMA = "google-docs-oauth-token/v1"
BRIDGE_SCHEMA = "google-docs-apps-script-bridge/v1"
CLIENT_FILENAME = "client.json"
TOKEN_FILENAME = "token.json"
BRIDGE_FILENAME = "bridge.json"
REFRESH_SKEW_SECONDS = 120


class OAuthError(RuntimeError):
    pass


def oauth_directory(value: Path | str | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve(strict=False)
    configured = os.environ.get(OAUTH_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (
        Path.home() / ".config" / "llm-wiki" / "google-docs-editing" / "oauth"
    ).resolve(strict=False)


def client_path(root: Path | str | None = None) -> Path:
    return oauth_directory(root) / CLIENT_FILENAME


def token_path(root: Path | str | None = None) -> Path:
    return oauth_directory(root) / TOKEN_FILENAME


def bridge_path(root: Path | str | None = None) -> Path:
    return oauth_directory(root) / BRIDGE_FILENAME


def _require_private_file(path: Path, label: str) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise OAuthError(f"could not inspect {label}") from exc
    if mode & 0o077:
        raise OAuthError(f"{label} permissions must not allow group or other access")


def _load_private_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise OAuthError(f"{label} is not configured")
    _require_private_file(path, label)
    try:
        return load_json(path, label)
    except ValueError as exc:
        raise OAuthError(f"could not read {label}") from exc


def configure_client(
    source: Path | str, root: Path | str | None = None
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve(strict=True)
    try:
        downloaded = load_json(source_path, "Google OAuth client download")
    except ValueError as exc:
        raise OAuthError("the OAuth client download is not valid JSON") from exc
    installed = downloaded.get("installed")
    if not isinstance(installed, dict):
        raise OAuthError("OAuth credentials must use the Desktop app client type")
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret", "")
    auth_uri = installed.get("auth_uri")
    token_uri = installed.get("token_uri")
    if not isinstance(client_id, str) or not client_id.endswith(
        ".apps.googleusercontent.com"
    ):
        raise OAuthError("OAuth client download has an invalid client_id")
    if not isinstance(client_secret, str):
        raise OAuthError("OAuth client download has an invalid client_secret")
    if (
        auth_uri not in DOWNLOADED_AUTHORIZATION_ENDPOINTS
        or token_uri != TOKEN_ENDPOINT
    ):
        raise OAuthError("OAuth client download uses unexpected Google endpoints")
    value = {
        "schema": CLIENT_SCHEMA,
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": AUTHORIZATION_ENDPOINT,
        "token_uri": TOKEN_ENDPOINT,
        "scopes": list(OAUTH_SCOPES),
    }
    destination = client_path(root)
    write_private_json(destination, value)
    return {"configured": True, "scopes": list(OAUTH_SCOPES)}


def load_client(root: Path | str | None = None) -> dict[str, Any]:
    value = _load_private_json(client_path(root), "Google OAuth client")
    if value.get("schema") != CLIENT_SCHEMA:
        raise OAuthError("Google OAuth client has an unsupported schema")
    client_id = value.get("client_id")
    client_secret = value.get("client_secret", "")
    scopes = value.get("scopes")
    if (
        not isinstance(client_id, str)
        or not client_id.endswith(".apps.googleusercontent.com")
        or not isinstance(client_secret, str)
        or not isinstance(scopes, list)
        or set(scopes) not in ({DRIVE_FILE_SCOPE}, set(OAUTH_SCOPES))
        or value.get("auth_uri") != AUTHORIZATION_ENDPOINT
        or value.get("token_uri") != TOKEN_ENDPOINT
    ):
        raise OAuthError("Google OAuth client is invalid")
    return value


def configure_bridge(
    deployment_id: str, root: Path | str | None = None
) -> dict[str, Any]:
    value = deployment_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", value):
        raise OAuthError("Apps Script API executable deployment ID is invalid")
    write_private_json(
        bridge_path(root),
        {"schema": BRIDGE_SCHEMA, "deployment_id": value},
    )
    return {"bridge_configured": True}


def load_bridge(root: Path | str | None = None) -> dict[str, str]:
    value = _load_private_json(bridge_path(root), "Apps Script bridge configuration")
    deployment_id = value.get("deployment_id")
    if (
        value.get("schema") != BRIDGE_SCHEMA
        or not isinstance(deployment_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", deployment_id)
    ):
        raise OAuthError("Apps Script bridge configuration is invalid")
    return {"schema": BRIDGE_SCHEMA, "deployment_id": deployment_id}


def _scope_set(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {value for value in raw.split() if value}
    if isinstance(raw, list):
        return {value for value in raw if isinstance(value, str) and value}
    return set()


def _validated_token(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != TOKEN_SCHEMA:
        raise OAuthError("Google OAuth token has an unsupported schema")
    access_token = value.get("access_token")
    refresh_token = value.get("refresh_token")
    expires_at = value.get("expires_at")
    scopes = _scope_set(value.get("scope"))
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("Google OAuth token has no access token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthError("Google OAuth token has no refresh token; sign in again")
    if not isinstance(expires_at, (int, float)):
        raise OAuthError("Google OAuth token has no expiry")
    missing = set(OAUTH_SCOPES) - scopes
    if missing:
        raise OAuthError(
            "Google OAuth token is missing the Apps Script bridge scopes; sign in again"
        )
    return {
        "schema": TOKEN_SCHEMA,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(expires_at),
        "scope": sorted(scopes),
        "token_type": "Bearer",
    }


def load_token(root: Path | str | None = None) -> dict[str, Any]:
    return _validated_token(_load_private_json(token_path(root), "Google OAuth token"))


def _oauth_error_code(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown_error"
    code = value.get("error")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
        return "unknown_error"
    return code


def _post_form(
    url: str, fields: dict[str, str], timeout: float = 30.0
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            error = _oauth_error_code(json.loads(exc.read().decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            error = "unknown_error"
        raise OAuthError(
            f"Google OAuth token request failed with HTTP {exc.code} ({error})"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthError("Google OAuth token request failed") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError("Google OAuth token response was invalid") from exc
    if not isinstance(value, dict):
        raise OAuthError("Google OAuth token response was invalid")
    return value


def _token_from_response(
    response: dict[str, Any], *, previous_refresh_token: str | None = None
) -> dict[str, Any]:
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token", previous_refresh_token)
    expires_in = response.get("expires_in")
    scopes = _scope_set(response.get("scope"))
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError("Google OAuth response has no access token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthError("Google OAuth response has no refresh token; sign in again")
    if type(expires_in) is not int or expires_in <= 0:
        raise OAuthError("Google OAuth response has an invalid expiry")
    missing = set(OAUTH_SCOPES) - scopes
    if missing:
        raise OAuthError(
            "Google OAuth consent did not grant every Apps Script bridge scope"
        )
    return {
        "schema": TOKEN_SCHEMA,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + expires_in,
        "scope": sorted(scopes),
        "token_type": "Bearer",
    }


def store_authorization_response(
    response: dict[str, Any], root: Path | str | None = None
) -> dict[str, Any]:
    value = _token_from_response(response)
    write_private_json(token_path(root), value)
    return value


def create_authorization_request(
    client: dict[str, Any], redirect_uri: str
) -> tuple[str, str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    query = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}", state, verifier


def exchange_authorization_code(
    client: dict[str, Any], code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    fields = {
        "client_id": client["client_id"],
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    return _post_form(TOKEN_ENDPOINT, fields)


@contextmanager
def _refresh_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    lock_path = root / ".refresh.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "a+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def refresh_access_token(root: Path | str | None = None) -> str:
    directory = oauth_directory(root)
    with _refresh_lock(directory):
        token = load_token(directory)
        if token["expires_at"] > int(time.time()) + REFRESH_SKEW_SECONDS:
            return token["access_token"]
        client = load_client(directory)
        fields = {
            "client_id": client["client_id"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        }
        if client.get("client_secret"):
            fields["client_secret"] = client["client_secret"]
        response = _post_form(TOKEN_ENDPOINT, fields)
        if "scope" not in response:
            response["scope"] = " ".join(token["scope"])
        refreshed = _token_from_response(
            response, previous_refresh_token=token["refresh_token"]
        )
        write_private_json(token_path(directory), refreshed)
        return refreshed["access_token"]


def get_access_token(root: Path | str | None = None) -> str:
    environment_token = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
    if environment_token:
        return environment_token
    if not token_path(root).is_file():
        raise OAuthError(
            "Google Docs OAuth is not connected; run "
            "scripts/google_docs_auth.py login or set "
            f"{ACCESS_TOKEN_ENV} for an ephemeral test"
        )
    token = load_token(root)
    if token["expires_at"] > int(time.time()) + REFRESH_SKEW_SECONDS:
        return token["access_token"]
    return refresh_access_token(root)


def oauth_status(root: Path | str | None = None) -> dict[str, Any]:
    directory = oauth_directory(root)
    configured = client_path(directory).is_file()
    connected = token_path(directory).is_file()
    result: dict[str, Any] = {
        "configured": configured,
        "connected": False,
        "bridge_configured": bridge_path(directory).is_file(),
        "scopes": list(OAUTH_SCOPES),
        "token_source": "stored" if connected else "none",
    }
    if not connected:
        return result
    raw_token = _load_private_json(token_path(directory), "Google OAuth token")
    if set(OAUTH_SCOPES) - _scope_set(raw_token.get("scope")):
        result["reauthorization_required"] = True
        return result
    token = _validated_token(raw_token)
    result.update(
        {
            "connected": True,
            "expires_at": token["expires_at"],
            "needs_refresh": token["expires_at"]
            <= int(time.time()) + REFRESH_SKEW_SECONDS,
        }
    )
    return result


def disconnect(root: Path | str | None = None) -> bool:
    path = token_path(root)
    if not path.exists():
        return False
    path.unlink()
    return True
