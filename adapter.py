#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google_docs_adapter.browser_operations import execute
from google_docs_adapter.storage import load_json, write_private_json


def adapter_root() -> Path:
    value = os.environ.get("LLM_WIKI_ADAPTER_ROOT")
    return Path(value).resolve(strict=False) if value else Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Docs tracked-suggestions adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("describe")
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--request", required=True)
    execute_parser.add_argument("--response", required=True)
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

    request = load_json(Path(args.request).resolve(strict=True), "adapter request")
    response = execute(request)
    write_private_json(Path(args.response).resolve(strict=False), response)
    return 0 if response.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
