#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google_docs_adapter.auth import (
    client_config_from_environment,
    default_client_path,
    default_token_path,
    document_id_from_reference,
    install_client_config,
)
from google_docs_adapter.auth_web import authorize_web
from google_docs_adapter.access import exact_document_authorization_status
from google_docs_adapter.extension_bridge import extension_root
from google_docs_adapter.native_messaging import (
    NativeMessagingError,
    connector_status,
    install_native_host,
    run_native_host,
)
from google_docs_adapter.operations import execute
from google_docs_adapter.storage import load_json, write_private_json


def adapter_root() -> Path:
    value = os.environ.get("LLM_WIKI_ADAPTER_ROOT")
    return Path(value).resolve(strict=False) if value else Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Docs tracked-changes adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("describe")
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--request", required=True)
    execute_parser.add_argument("--response", required=True)
    configure_parser = subparsers.add_parser("configure-oauth")
    configure_parser.add_argument("--client-secrets", required=True)
    configure_parser.add_argument("--destination", default=str(default_client_path()))
    auth_parser = subparsers.add_parser("auth")
    auth_parser.add_argument(
        "--document",
        help="Optional Google Docs URL, google-docs resource, or document ID to pin in Picker",
    )
    auth_parser.add_argument("--token", default=str(default_token_path()))
    auth_parser.add_argument("--timeout", type=int, default=600)
    auth_parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Print the local setup URL without opening the default browser",
    )
    auth_status_parser = subparsers.add_parser("auth-status")
    auth_status_parser.add_argument(
        "--document",
        required=True,
        help="Google Docs URL, google-docs resource, or document ID to probe",
    )
    auth_status_parser.add_argument("--token", default=str(default_token_path()))
    subparsers.add_parser("extension-path")
    browser_install_parser = subparsers.add_parser("browser-install")
    browser_install_parser.add_argument(
        "--native-host-dir",
        help="Override Chrome's user-level NativeMessagingHosts directory",
    )
    browser_status_parser = subparsers.add_parser("browser-status")
    browser_status_parser.add_argument(
        "--native-host-dir",
        help="Override Chrome's user-level NativeMessagingHosts directory",
    )
    native_host_parser = subparsers.add_parser("native-host")
    native_host_parser.add_argument("origin")
    native_host_parser.add_argument("browser_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "describe":
        manifest = load_json(adapter_root() / ".llm-wiki-adapter.json", "adapter manifest")
        print(json.dumps({
            "protocol": manifest["protocol"],
            "id": manifest["id"],
            "version": manifest["version"],
            "capabilities": manifest["capabilities"],
        }, sort_keys=True))
        return 0
    if args.command == "configure-oauth":
        try:
            install_client_config(
                Path(args.client_secrets).expanduser().resolve(strict=True),
                Path(args.destination),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"OAuth client provisioning failed: {exc}", file=sys.stderr)
            return 2
        print("Managed Google OAuth client installed with mode 0600.")
        return 0
    if args.command == "auth":
        try:
            document_id = document_id_from_reference(args.document) if args.document else None
            token_path = Path(args.token).expanduser().resolve(strict=False)
            client_config = client_config_from_environment()
            picked_file_ids = authorize_web(
                client_config,
                token_path,
                args.timeout,
                document_id,
                open_browser=not args.no_open_browser,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Authorization failed: {exc}", file=sys.stderr)
            return 2
        print("OAuth token stored with mode 0600.")
        for document_id in picked_file_ids:
            print(f"Authorized resource: google-docs:{document_id}")
        return 0
    if args.command == "auth-status":
        try:
            document_id = document_id_from_reference(args.document)
            result = exact_document_authorization_status(
                document_id,
                Path(args.token),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Authorization status failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "extension-path":
        print(extension_root())
        return 0
    if args.command == "browser-install":
        try:
            destination = (
                Path(args.native_host_dir).expanduser().resolve(strict=False)
                if args.native_host_dir
                else None
            )
            result = install_native_host(adapter_root(), destination)
        except (OSError, RuntimeError, ValueError, NativeMessagingError) as exc:
            print(f"Browser connector installation failed: {exc}", file=sys.stderr)
            return 2
        print("LLM Wiki browser connector installed for normal Chrome.")
        print(f"Extension ID: {result['extension_id']}")
        print(f"Load extension: {extension_root()}")
        return 0
    if args.command == "browser-status":
        try:
            destination = (
                Path(args.native_host_dir).expanduser().resolve(strict=False)
                if args.native_host_dir
                else None
            )
            print(json.dumps(connector_status(destination), sort_keys=True))
        except (OSError, RuntimeError, ValueError, NativeMessagingError) as exc:
            print(f"Browser connector status failed: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.command == "native-host":
        try:
            run_native_host(args.origin)
        except (OSError, RuntimeError, ValueError, NativeMessagingError) as exc:
            print(f"Native browser connector stopped: {exc}", file=sys.stderr)
            return 2
        return 0

    request = load_json(Path(args.request).resolve(strict=True), "adapter request")
    response = execute(request)
    write_private_json(Path(args.response).resolve(strict=False), response)
    return 0 if response.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
