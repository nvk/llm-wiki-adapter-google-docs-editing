from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import time
from pathlib import Path
from typing import Any, Callable

BRIDGE_PROTOCOL = "llm-wiki-google-docs-extension/v2"
MAX_MESSAGE_BYTES = 1_048_576
SAFE_UNIX_SOCKET_PATH_BYTES = 90


class ExtensionBridgeError(RuntimeError):
    """Raised when the normal-Chrome connector cannot complete an edit."""


def extension_root() -> Path:
    return Path(__file__).resolve().parents[1] / "extension"


def bridge_state_root() -> Path:
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    return (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else Path.home() / ".local" / "state" / "llm-wiki" / "google-docs-editing"
    )


def native_socket_path() -> Path:
    override = os.environ.get("LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    candidate = bridge_state_root() / "native-bridge.sock"
    if len(os.fsencode(str(candidate))) <= SAFE_UNIX_SOCKET_PATH_BYTES:
        return candidate
    digest = hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:12]
    user_id = getattr(os, "getuid", lambda: 0)()
    return Path("/tmp") / f"llm-wiki-gdocs-{user_id}-{digest}" / "bridge.sock"


def ensure_private_socket_parent(socket_path: Path) -> None:
    parent = socket_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ExtensionBridgeError("native connector socket parent is not a private directory")
    user_id = getattr(os, "getuid", lambda: metadata.st_uid)()
    if metadata.st_uid != user_id:
        raise ExtensionBridgeError("native connector socket parent belongs to another user")
    parent.chmod(0o700)


def _send_line(connection: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ExtensionBridgeError("native connector message is too large")
    connection.sendall(payload + b"\n")


def _receive_line(connection: socket.socket, buffer: bytearray) -> dict[str, Any]:
    while b"\n" not in buffer:
        chunk = connection.recv(65536)
        if not chunk:
            raise ExtensionBridgeError("native connector closed before completing the edit")
        buffer.extend(chunk)
        if len(buffer) > MAX_MESSAGE_BYTES:
            raise ExtensionBridgeError("native connector message is too large")
    line, _, remainder = bytes(buffer).partition(b"\n")
    buffer.clear()
    buffer.extend(remainder)
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtensionBridgeError("native connector returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("protocol") != BRIDGE_PROTOCOL:
        raise ExtensionBridgeError("native connector protocol does not match the adapter")
    return value


def _validate_result(value: dict[str, Any], job: dict[str, Any], authorized: bool) -> dict[str, Any]:
    result = value.get("result")
    if value.get("type") != "result" or not isinstance(result, dict):
        raise ExtensionBridgeError("native connector returned an invalid result")
    if result.get("job_id") != job["job_id"]:
        raise ExtensionBridgeError("native connector result belongs to a different job")
    status = result.get("status")
    mode_verified = result.get("mode_verified")
    edit_count = result.get("edit_count")
    mutation_started = result.get("mutation_started")
    if status not in {"ok", "error"}:
        raise ExtensionBridgeError("native connector result status is invalid")
    if not isinstance(mode_verified, bool) or not isinstance(mutation_started, bool):
        raise ExtensionBridgeError("native connector result flags are invalid")
    if not isinstance(edit_count, int) or not 0 <= edit_count <= 100:
        raise ExtensionBridgeError("native connector result edit count is invalid")
    error = result.get("error")
    if error is not None and (not isinstance(error, str) or len(error) > 500):
        raise ExtensionBridgeError("native connector result error is invalid")
    if status == "ok" and (
        not authorized
        or not mode_verified
        or not mutation_started
        or edit_count != len(job["edits"])
    ):
        raise ExtensionBridgeError("native connector did not prove the complete Suggesting edit")
    return result


class ExtensionSuggestionDriver:
    """Send one governed edit through Chrome's allowlisted native host."""

    def __init__(
        self,
        plan_sha256: str,
        *,
        timeout_seconds: int = 300,
        socket_path: Path | None = None,
        ready: Callable[[Path], None] | None = None,
    ) -> None:
        self.plan_sha256 = plan_sha256
        self.timeout_seconds = max(10, timeout_seconds)
        self.socket_path = (socket_path or native_socket_path()).resolve(strict=False)
        self.ready = ready

    def apply(
        self,
        document_id: str,
        edits: list[dict[str, Any]],
        before_mutation: Callable[[], None],
    ) -> dict[str, Any]:
        if not edits:
            raise ExtensionBridgeError("approved plan contains no extension edits")
        if not self.socket_path.exists():
            raise ExtensionBridgeError(
                "Chrome connector is offline; keep normal Chrome running with the "
                "LLM Wiki Google Docs extension enabled"
            )
        job = {
            "protocol": BRIDGE_PROTOCOL,
            "type": "job",
            "job": {
                "protocol": BRIDGE_PROTOCOL,
                "job_id": os.urandom(18).hex(),
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
            },
        }
        authorized = False
        boundary_error: Exception | None = None
        deadline = time.monotonic() + self.timeout_seconds
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(min(5.0, float(self.timeout_seconds)))
        try:
            try:
                connection.connect(str(self.socket_path))
            except OSError as exc:
                raise ExtensionBridgeError(
                    "Chrome connector is unavailable; keep normal Chrome running and verify "
                    "the native connector installation"
                ) from exc
            if self.ready:
                self.ready(self.socket_path)
            _send_line(connection, job)
            buffer = bytearray()
            while time.monotonic() < deadline:
                connection.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    message = _receive_line(connection, buffer)
                except socket.timeout as exc:
                    raise ExtensionBridgeError(
                        "timed out waiting for the normal-Chrome connector"
                    ) from exc
                message_type = message.get("type")
                if message_type == "before-mutation":
                    if message.get("job_id") != job["job"]["job_id"]:
                        raise ExtensionBridgeError(
                            "native connector mutation boundary belongs to a different job"
                        )
                    if authorized:
                        raise ExtensionBridgeError("native connector repeated the mutation boundary")
                    try:
                        before_mutation()
                    except Exception as exc:  # Preserve the governed boundary failure.
                        boundary_error = exc
                        _send_line(connection, {
                            "protocol": BRIDGE_PROTOCOL,
                            "type": "mutation-authorized",
                            "job_id": job["job"]["job_id"],
                            "authorized": False,
                        })
                    else:
                        authorized = True
                        _send_line(connection, {
                            "protocol": BRIDGE_PROTOCOL,
                            "type": "mutation-authorized",
                            "job_id": job["job"]["job_id"],
                            "authorized": True,
                        })
                    continue
                if message_type == "result":
                    result = _validate_result(message, job["job"], authorized)
                    if boundary_error is not None:
                        raise ExtensionBridgeError(
                            "governed mutation boundary rejected the edit"
                        ) from boundary_error
                    if result["status"] != "ok":
                        raise ExtensionBridgeError(
                            result.get("error") or "normal-Chrome connector could not complete the edit"
                        )
                    return {
                        "transport": "chrome-native-messaging-suggesting-ui",
                        "mode_verified": result["mode_verified"],
                        "edit_count": result["edit_count"],
                    }
                if message_type == "error":
                    detail = message.get("error")
                    raise ExtensionBridgeError(
                        detail if isinstance(detail, str) else "native connector rejected the edit"
                    )
                raise ExtensionBridgeError("native connector returned an unexpected message")
            raise ExtensionBridgeError("timed out waiting for the normal-Chrome connector")
        finally:
            connection.close()
