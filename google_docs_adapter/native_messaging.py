from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

from .extension_bridge import (
    BRIDGE_PROTOCOL,
    MAX_MESSAGE_BYTES,
    bridge_state_root,
    ensure_private_socket_parent,
    extension_root,
    native_socket_path,
)
from .storage import write_private_json

NATIVE_HOST_NAME = "net.llmwiki.google_docs"
NATIVE_HOST_SCHEMA = "llm-wiki-google-docs-native-host/v1"
EXTENSION_ORIGIN_PREFIX = "chrome-extension://"


class NativeMessagingError(RuntimeError):
    """Raised when installation or native framing is invalid."""


def extension_id_from_manifest(manifest_path: Path | None = None) -> str:
    path = manifest_path or (extension_root() / "manifest.json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        public_key = base64.b64decode(manifest["key"], validate=True)
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise NativeMessagingError("extension manifest has no valid stable public key") from exc
    prefix = hashlib.sha256(public_key).digest()[:16].hex()
    return prefix.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def chrome_native_host_dir() -> Path:
    override = os.environ.get("LLM_WIKI_CHROME_NATIVE_HOST_DIR")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "NativeMessagingHosts"
        )
    if os.name == "posix":
        return Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"
    raise NativeMessagingError("automatic native-host installation supports macOS and Linux")


def install_native_host(
    adapter_path: Path | None = None,
    destination: Path | None = None,
) -> dict[str, Any]:
    root = (adapter_path or Path(__file__).resolve().parents[1]).resolve(strict=True)
    python = root / ".venv" / "bin" / "python"
    entrypoint = root / "adapter.py"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise NativeMessagingError("adapter virtual environment is missing or not executable")
    if not entrypoint.is_file():
        raise NativeMessagingError("adapter entrypoint is missing")
    install_dir = (destination or chrome_native_host_dir()).resolve(strict=False)
    install_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    extension_id = extension_id_from_manifest(root / "extension" / "manifest.json")
    state_root = bridge_state_root()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        state_root.chmod(0o700)
    except OSError:
        pass
    ensure_private_socket_parent(native_socket_path())
    wrapper = state_root / "native-host"
    script = (
        "#!/bin/sh\n"
        f"export LLM_WIKI_GOOGLE_DOCS_NATIVE_SOCKET={shlex.quote(str(native_socket_path()))}\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(entrypoint))} native-host \"$@\"\n"
    )
    wrapper.write_text(script, encoding="utf-8")
    wrapper.chmod(0o700)
    manifest_path = install_dir / f"{NATIVE_HOST_NAME}.json"
    write_private_json(manifest_path, {
        "name": NATIVE_HOST_NAME,
        "description": "Local LLM Wiki Google Docs Suggesting connector",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"{EXTENSION_ORIGIN_PREFIX}{extension_id}/"],
    })
    write_private_json(state_root / "native-host-installation.json", {
        "schema": NATIVE_HOST_SCHEMA,
        "extension_id": extension_id,
        "manifest_path": str(manifest_path),
    })
    return {
        "extension_id": extension_id,
        "manifest_path": manifest_path,
        "wrapper_path": wrapper,
        "socket_path": native_socket_path(),
    }


def connector_status(destination: Path | None = None) -> dict[str, Any]:
    install_dir = (destination or chrome_native_host_dir()).resolve(strict=False)
    manifest_path = install_dir / f"{NATIVE_HOST_NAME}.json"
    installed = False
    if manifest_path.is_file():
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = f"{EXTENSION_ORIGIN_PREFIX}{extension_id_from_manifest()}/"
            installed = (
                value.get("name") == NATIVE_HOST_NAME
                and value.get("type") == "stdio"
                and value.get("allowed_origins") == [expected]
                and Path(str(value.get("path", ""))).is_file()
            )
        except (OSError, ValueError, json.JSONDecodeError, NativeMessagingError):
            installed = False
    return {
        "installed": installed,
        "connected": native_socket_path().is_socket(),
        "extension_id": extension_id_from_manifest(),
    }


def read_native_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise NativeMessagingError("native message header is truncated")
    length = struct.unpack("=I", header)[0]
    if length <= 0 or length > MAX_MESSAGE_BYTES:
        raise NativeMessagingError("native message has an invalid size")
    payload = stream.read(length)
    if len(payload) != length:
        raise NativeMessagingError("native message is truncated")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeMessagingError("native message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise NativeMessagingError("native message must be a JSON object")
    return value


def write_native_message(stream: BinaryIO, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise NativeMessagingError("native message is too large")
    stream.write(struct.pack("=I", len(payload)))
    stream.write(payload)
    stream.flush()


def _valid_extension_origin(value: str) -> bool:
    if not value.startswith(EXTENSION_ORIGIN_PREFIX) or not value.endswith("/"):
        return False
    extension_id = value[len(EXTENSION_ORIGIN_PREFIX):-1]
    return len(extension_id) == 32 and all(character in "abcdefghijklmnop" for character in extension_id)


class NativeRelay:
    """Relay one local agent connection at a time to the connected extension."""

    def __init__(self, input_stream: BinaryIO, output_stream: BinaryIO, socket_path: Path) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.socket_path = socket_path
        self.output_lock = threading.Lock()
        self.agent_lock = threading.Lock()
        self.agent: socket.socket | None = None
        self.server: socket.socket | None = None

    def _write_extension(self, value: dict[str, Any]) -> None:
        with self.output_lock:
            write_native_message(self.output_stream, value)

    def _write_agent(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            raise NativeMessagingError("relay message is too large")
        with self.agent_lock:
            if self.agent is None:
                return
            self.agent.sendall(payload + b"\n")

    def _handle_agent(self, connection: socket.socket) -> None:
        with self.agent_lock:
            if self.agent is not None:
                connection.sendall(json.dumps({
                    "protocol": BRIDGE_PROTOCOL,
                    "type": "error",
                    "error": "another Google Docs edit is already active",
                }, separators=(",", ":")).encode("utf-8") + b"\n")
                connection.close()
                return
            self.agent = connection
        buffer = bytearray()
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_MESSAGE_BYTES:
                    raise NativeMessagingError("agent relay message is too large")
                while b"\n" in buffer:
                    line, _, remainder = bytes(buffer).partition(b"\n")
                    buffer.clear()
                    buffer.extend(remainder)
                    value = json.loads(line.decode("utf-8"))
                    if not isinstance(value, dict) or value.get("protocol") != BRIDGE_PROTOCOL:
                        raise NativeMessagingError("agent relay protocol is invalid")
                    self._write_extension(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, NativeMessagingError):
            pass
        finally:
            with self.agent_lock:
                if self.agent is connection:
                    self.agent = None
            connection.close()

    def _accept_agents(self) -> None:
        assert self.server is not None
        while True:
            try:
                connection, _address = self.server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle_agent,
                args=(connection,),
                daemon=True,
            ).start()

    def run(self) -> None:
        ensure_private_socket_parent(self.socket_path)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            if self.socket_path.is_socket() or self.socket_path.is_symlink():
                self.socket_path.unlink()
            else:
                raise NativeMessagingError("native connector socket path is not a socket")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            self.server.listen(2)
            threading.Thread(target=self._accept_agents, daemon=True).start()
            self._write_extension({"protocol": BRIDGE_PROTOCOL, "type": "ready"})
            while True:
                value = read_native_message(self.input_stream)
                if value is None:
                    return
                if value.get("protocol") != BRIDGE_PROTOCOL:
                    self._write_extension({
                        "protocol": BRIDGE_PROTOCOL,
                        "type": "error",
                        "error": "extension protocol does not match native host",
                    })
                    continue
                self._write_agent(value)
        finally:
            if self.server is not None:
                self.server.close()
            with self.agent_lock:
                if self.agent is not None:
                    self.agent.close()
                    self.agent = None
            try:
                if self.socket_path.is_socket() or self.socket_path.is_symlink():
                    self.socket_path.unlink()
            except OSError:
                pass


def run_native_host(origin: str | None = None) -> None:
    caller = origin or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not _valid_extension_origin(caller):
        raise NativeMessagingError("native host caller is not a Chrome extension")
    expected = f"{EXTENSION_ORIGIN_PREFIX}{extension_id_from_manifest()}/"
    if caller != expected:
        raise NativeMessagingError("native host caller is not the installed LLM Wiki extension")
    NativeRelay(sys.stdin.buffer, sys.stdout.buffer, native_socket_path()).run()
