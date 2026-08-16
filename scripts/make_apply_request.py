#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from google_docs_adapter.storage import load_json, sha256_file, write_private_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a governed apply request from a private plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan).expanduser().resolve(strict=True)
    plan = load_json(plan_path, "suggestion plan")
    if plan.get("schema") != "google-docs-suggestion-plan/v1":
        raise SystemExit("not a google-docs-suggestion-plan/v1 plan")
    value = {
        "protocol": "llm-wiki-adapter/v1",
        "adapter_id": "google-docs-editing",
        "operation": "apply",
        "arguments": {
            "document_resource": plan["document_resource"],
            "plan": str(plan_path),
        },
        "output_dir": str(Path(args.output_dir).expanduser().resolve(strict=False)),
        "remote_write": {
            "plan_sha256": sha256_file(plan_path),
            "idempotency_key": args.idempotency_key,
            "expected_revision": plan["revision_id"],
        },
        "options": {},
    }
    destination = Path(args.request).expanduser().resolve(strict=False)
    write_private_json(destination, value)
    print(value["remote_write"]["plan_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
