#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from google_docs_adapter.storage import load_json, sha256_file, write_private_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Google Docs receipt-verification request")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan).expanduser().resolve(strict=True)
    plan = load_json(plan_path, "suggestion plan")
    if plan.get("schema") != "google-docs-browser-suggestion-plan/v1":
        raise SystemExit("not a google-docs-browser-suggestion-plan/v1 plan")
    receipt_path = Path(args.receipt).expanduser().resolve(strict=True)
    receipt = load_json(receipt_path, "remote receipt")
    remote_receipt = receipt.get("remote_receipt")
    if not isinstance(remote_receipt, dict):
        raise SystemExit("receipt has no remote_receipt")
    if remote_receipt.get("plan_sha256") != sha256_file(plan_path):
        raise SystemExit("receipt does not match the suggestion plan")
    value = {
        "protocol": "llm-wiki-adapter/v1",
        "adapter_id": "google-docs-editing",
        "operation": "verify",
        "arguments": {
            "collaboration_resource": plan["collaboration_resource"],
            "receipt": str(receipt_path),
            "plan": str(plan_path),
        },
        "output_dir": str(Path(args.output_dir).expanduser().resolve(strict=False)),
        "options": {},
    }
    destination = Path(args.request).expanduser().resolve(strict=False)
    write_private_json(destination, value)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
