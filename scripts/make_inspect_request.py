#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from google_docs_adapter.browser_executor import document_id_from_expected_url  # noqa: E402
from google_docs_adapter.storage import write_private_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only Google Docs inspect request")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    document_id_from_expected_url(args.url)
    value = {
        "protocol": "llm-wiki-adapter/v1",
        "adapter_id": "google-docs-editing",
        "operation": "inspect",
        "arguments": {
            "collaboration_resource": "browser-collaboration:active-tab",
            "expected_document_url": args.url,
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
