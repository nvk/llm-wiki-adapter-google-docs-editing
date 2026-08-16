from __future__ import annotations

import hmac
import http.server
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .storage import load_json, write_private_json

BRIDGE_PROTOCOL = "llm-wiki-google-docs-extension/v1"
PAIRING_SCHEMA = "llm-wiki-google-docs-extension-pairing/v1"
DEFAULT_PORT = 17843
MAX_REQUEST_BYTES = 1_048_576


class ExtensionBridgeError(RuntimeError):
    """Raised when the paired Chrome extension cannot complete a governed edit."""


def extension_root() -> Path:
    return Path(__file__).resolve().parents[1] / "extension"


def bridge_state_path() -> Path:
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    root = (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else Path.home() / ".local" / "state" / "llm-wiki" / "google-docs-editing"
    )
    return root / "extension-pairing.json"


def bridge_port() -> int:
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_EXTENSION_PORT", str(DEFAULT_PORT))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ExtensionBridgeError("extension bridge port must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ExtensionBridgeError("extension bridge port must be between 1 and 65535")
    return value


def _extension_origin(value: str | None) -> str:
    origin = value or ""
    prefix = "chrome-extension://"
    extension_id = origin[len(prefix):] if origin.startswith(prefix) else ""
    if len(extension_id) != 32 or any(
        character not in "abcdefghijklmnop" for character in extension_id
    ):
        raise ExtensionBridgeError("request did not come from a Chrome extension origin")
    return origin


def _load_pairing(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ExtensionBridgeError(
            "Chrome extension is not paired; run adapter.py extension-pair first"
        )
    value = load_json(path, "extension pairing")
    if value.get("schema") != PAIRING_SCHEMA:
        raise ExtensionBridgeError("extension pairing state has an unsupported schema")
    token = value.get("token")
    origin = value.get("extension_origin")
    if not isinstance(token, str) or len(token) < 32:
        raise ExtensionBridgeError("extension pairing token is invalid")
    if not isinstance(origin, str):
        raise ExtensionBridgeError("extension pairing origin is invalid")
    _extension_origin(origin)
    return {"token": token, "extension_origin": origin}


@dataclass
class PairingSession:
    state_path: Path
    pairing_code: str
    event: threading.Event = field(default_factory=threading.Event)
    paired_origin: str = ""
    attempts: int = 0


@dataclass
class JobSession:
    token: str
    extension_origin: str
    job: dict[str, Any]
    before_mutation: Callable[[], None]
    result_event: threading.Event = field(default_factory=threading.Event)
    mutation_authorized: bool = False
    result: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _BridgeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        session: PairingSession | JobSession,
    ) -> None:
        self.session = session
        super().__init__(address, _BridgeHandler)


class _BridgeHandler(http.server.BaseHTTPRequestHandler):
    server: _BridgeServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self.headers.get("Origin")
        try:
            safe_origin = _extension_origin(origin)
        except ExtensionBridgeError:
            safe_origin = ""
        if safe_origin:
            self.send_header("Access-Control-Allow-Origin", safe_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(payload)

    def _origin(self) -> str:
        return _extension_origin(self.headers.get("Origin"))

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ExtensionBridgeError("invalid request length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ExtensionBridgeError("invalid request body size")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExtensionBridgeError("request body must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ExtensionBridgeError("request body must be a JSON object")
        return value

    def _authorized_job(self, *, allow_missing_origin: bool = False) -> JobSession:
        session = self.server.session
        if not isinstance(session, JobSession):
            raise ExtensionBridgeError("no edit job is active")
        raw_origin = self.headers.get("Origin")
        if raw_origin is None:
            # Chrome omits Origin on extension GET requests. Those requests are
            # read-only and still require the unguessable paired bearer token.
            # State-changing POSTs continue to require the exact paired origin.
            if not allow_missing_origin:
                raise ExtensionBridgeError("request did not include the paired extension origin")
        elif not hmac.compare_digest(_extension_origin(raw_origin), session.extension_origin):
            raise ExtensionBridgeError("extension origin does not match the paired extension")
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {session.token}"
        if not hmac.compare_digest(authorization, expected):
            raise ExtensionBridgeError("extension bridge authorization failed")
        return session

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            self._origin()
        except ExtensionBridgeError as exc:
            self._send(403, {"protocol": BRIDGE_PROTOCOL, "error": str(exc)})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            session = self._authorized_job(allow_missing_origin=True)
            if self.path == "/v1/status":
                self._send(200, {
                    "protocol": BRIDGE_PROTOCOL,
                    "paired": True,
                    "job_available": session.result is None,
                })
                return
            if self.path == "/v1/job":
                if session.result is not None:
                    raise ExtensionBridgeError("edit job is already complete")
                self._send(200, session.job)
                return
            self._send(404, {"protocol": BRIDGE_PROTOCOL, "error": "not found"})
        except ExtensionBridgeError as exc:
            self._send(403, {"protocol": BRIDGE_PROTOCOL, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/v1/pair":
                self._pair()
            elif self.path == "/v1/before-mutation":
                self._before_mutation()
            elif self.path == "/v1/result":
                self._result()
            else:
                self._send(404, {"protocol": BRIDGE_PROTOCOL, "error": "not found"})
        except ExtensionBridgeError as exc:
            self._send(403, {"protocol": BRIDGE_PROTOCOL, "error": str(exc)})
        except Exception:
            self._send(409, {
                "protocol": BRIDGE_PROTOCOL,
                "error": "governed mutation boundary rejected the edit",
            })

    def _pair(self) -> None:
        session = self.server.session
        if not isinstance(session, PairingSession):
            raise ExtensionBridgeError("pairing is not active")
        origin = self._origin()
        value = self._body()
        code = value.get("pairing_code")
        session.attempts += 1
        if session.attempts > 5:
            raise ExtensionBridgeError("pairing attempt limit exceeded")
        if not isinstance(code, str) or not hmac.compare_digest(code, session.pairing_code):
            raise ExtensionBridgeError("pairing code is invalid")
        token = secrets.token_urlsafe(32)
        write_private_json(session.state_path, {
            "schema": PAIRING_SCHEMA,
            "extension_origin": origin,
            "token": token,
            "paired_at_unix": int(time.time()),
        })
        session.paired_origin = origin
        session.event.set()
        self._send(200, {"protocol": BRIDGE_PROTOCOL, "paired": True, "token": token})

    def _before_mutation(self) -> None:
        session = self._authorized_job()
        value = self._body()
        if value.get("job_id") != session.job["job_id"]:
            raise ExtensionBridgeError("edit job identifier does not match")
        with session.lock:
            if not session.mutation_authorized:
                session.before_mutation()
                session.mutation_authorized = True
        self._send(200, {"protocol": BRIDGE_PROTOCOL, "mutation_authorized": True})

    def _result(self) -> None:
        session = self._authorized_job()
        value = self._body()
        if value.get("job_id") != session.job["job_id"]:
            raise ExtensionBridgeError("edit job identifier does not match")
        status = value.get("status")
        mode_verified = value.get("mode_verified")
        edit_count = value.get("edit_count")
        mutation_started = value.get("mutation_started")
        if status not in {"ok", "error"}:
            raise ExtensionBridgeError("extension result status is invalid")
        if not isinstance(mode_verified, bool) or not isinstance(mutation_started, bool):
            raise ExtensionBridgeError("extension result flags are invalid")
        if not isinstance(edit_count, int) or not 0 <= edit_count <= 100:
            raise ExtensionBridgeError("extension result edit count is invalid")
        if status == "ok" and (
            not mode_verified
            or not session.mutation_authorized
            or edit_count != len(session.job["edits"])
        ):
            raise ExtensionBridgeError("extension did not prove the complete Suggesting edit")
        error = value.get("error")
        if error is not None and (not isinstance(error, str) or len(error) > 500):
            raise ExtensionBridgeError("extension result error is invalid")
        session.result = {
            "status": status,
            "mode_verified": mode_verified,
            "edit_count": edit_count,
            "mutation_started": mutation_started,
            "error": error or "",
        }
        session.result_event.set()
        self._send(200, {"protocol": BRIDGE_PROTOCOL, "received": True})


def create_pairing_server(
    state_path: Path | None = None,
    *,
    port: int | None = None,
    pairing_code: str | None = None,
) -> tuple[_BridgeServer, PairingSession]:
    destination = (state_path or bridge_state_path()).resolve(strict=False)
    code = pairing_code or f"{secrets.randbelow(100_000_000):08d}"
    session = PairingSession(destination, code)
    try:
        server = _BridgeServer(("127.0.0.1", bridge_port() if port is None else port), session)
    except OSError as exc:
        raise ExtensionBridgeError(f"could not start extension bridge: {exc}") from exc
    return server, session


def wait_for_pairing(
    server: _BridgeServer,
    session: PairingSession,
    timeout_seconds: int,
) -> str:
    deadline = time.monotonic() + max(1, timeout_seconds)
    server.timeout = 0.25
    try:
        while not session.event.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not session.event.is_set():
        raise ExtensionBridgeError("extension pairing timed out")
    return session.paired_origin


class ExtensionSuggestionDriver:
    """Wait for a paired normal-Chrome extension to run one approved edit job."""

    def __init__(
        self,
        plan_sha256: str,
        *,
        timeout_seconds: int = 300,
        port: int | None = None,
        state_path: Path | None = None,
        ready: Callable[[str], None] | None = None,
    ) -> None:
        self.plan_sha256 = plan_sha256
        self.timeout_seconds = max(10, timeout_seconds)
        self.port = bridge_port() if port is None else port
        self.state_path = (state_path or bridge_state_path()).resolve(strict=False)
        self.ready = ready

    def apply(
        self,
        document_id: str,
        edits: list[dict[str, Any]],
        before_mutation: Callable[[], None],
    ) -> dict[str, Any]:
        if not edits:
            raise ExtensionBridgeError("approved plan contains no extension edits")
        pairing = _load_pairing(self.state_path)
        job_id = secrets.token_urlsafe(18)
        job = {
            "protocol": BRIDGE_PROTOCOL,
            "job_id": job_id,
            "plan_sha256": self.plan_sha256,
            "document_id": document_id,
            "edits": [
                {
                    "tab_id": str(edit.get("tab_id", "")),
                    "find": str(edit["find"]),
                    "replace": str(edit["replace"]),
                }
                for edit in edits
            ],
        }
        session = JobSession(
            pairing["token"],
            pairing["extension_origin"],
            job,
            before_mutation,
        )
        try:
            server = _BridgeServer(("127.0.0.1", self.port), session)
        except OSError as exc:
            raise ExtensionBridgeError(f"could not start extension bridge: {exc}") from exc
        server.timeout = 0.25
        base_url = f"http://127.0.0.1:{server.server_port}"
        if self.ready:
            self.ready(base_url)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while not session.result_event.is_set() and time.monotonic() < deadline:
                server.handle_request()
        finally:
            server.server_close()
        if not session.result_event.is_set() or session.result is None:
            raise ExtensionBridgeError(
                "timed out waiting for the paired Chrome extension; open the extension side panel "
                "on the authorized Google Doc and choose Check, then Apply"
            )
        if session.result["status"] != "ok":
            detail = session.result["error"] or "extension could not complete the edit"
            raise ExtensionBridgeError(detail)
        return {
            "transport": "chrome-extension-suggesting-ui",
            "mode_verified": session.result["mode_verified"],
            "edit_count": session.result["edit_count"],
        }
