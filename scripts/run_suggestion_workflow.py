#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from google_docs_adapter.browser_executor import document_id_from_expected_url  # noqa: E402
from google_docs_adapter.storage import load_json, sha256_file, write_private_json  # noqa: E402

ADAPTER_ID = "google-docs-editing"
COLLABORATION_RESOURCE = "browser-collaboration:active-tab"


class WorkflowFailure(RuntimeError):
    pass


def run_adapter(
    llm_wiki: Path,
    request: Path,
    response: Path,
    *,
    timeout: int,
    approved_plan_sha256: str | None = None,
    require_ok: bool = True,
) -> dict[str, Any]:
    command = [
        str(llm_wiki),
        "adapter",
        "run",
        ADAPTER_ID,
        "--request",
        str(request),
        "--response",
        str(response),
        "--timeout",
        str(timeout),
        "--json",
    ]
    if approved_plan_sha256 is not None:
        command.extend(["--approve-remote-write", approved_plan_sha256])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowFailure("adapter process failed") from exc
    if not response.is_file():
        raise WorkflowFailure("adapter did not write a response")
    value = load_json(response, "adapter response")
    if require_ok and (completed.returncode != 0 or value.get("status") != "ok"):
        raise WorkflowFailure("adapter operation failed")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one serialized Google Docs plan/apply/verify workflow"
    )
    parser.add_argument("--llm-wiki", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--edit-spec", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--approve-remote-write", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    stage = "validate"
    try:
        if not args.approve_remote_write:
            raise WorkflowFailure("explicit remote-write approval is required")
        if not 30 <= args.timeout <= 600:
            raise WorkflowFailure("timeout must be between 30 and 600 seconds")
        if not args.idempotency_key or len(args.idempotency_key) > 256:
            raise WorkflowFailure("invalid idempotency key")
        document_id_from_expected_url(args.url)
        llm_wiki = Path(args.llm_wiki).expanduser().resolve(strict=True)
        if not llm_wiki.is_file() or not os.access(llm_wiki, os.X_OK):
            raise WorkflowFailure("llm-wiki is not executable")
        edit_spec = Path(args.edit_spec).expanduser().resolve(strict=True)
        if load_json(edit_spec, "edit specification").get("schema") != "google-docs-edit-spec/v1":
            raise WorkflowFailure("invalid edit specification")
        run_dir = Path(args.run_dir).expanduser().resolve(strict=False)
        if run_dir.exists():
            if not run_dir.is_dir() or next(run_dir.iterdir(), None) is not None:
                raise WorkflowFailure("run directory must be absent or empty")
        else:
            run_dir.mkdir(parents=True, mode=0o700)
        try:
            run_dir.chmod(0o700)
        except OSError:
            pass

        stage = "plan"
        plan_dir = run_dir / "plan"
        plan_dir.mkdir(mode=0o700)
        plan_request = run_dir / "plan-request.json"
        plan_response = run_dir / "plan-response.json"
        write_private_json(plan_request, {
            "protocol": "llm-wiki-adapter/v1",
            "adapter_id": ADAPTER_ID,
            "operation": "plan",
            "arguments": {
                "collaboration_resource": COLLABORATION_RESOURCE,
                "expected_document_url": args.url,
                "edit_spec": str(edit_spec),
            },
            "output_dir": str(plan_dir),
            "options": {},
        })
        plan_result = run_adapter(
            llm_wiki, plan_request, plan_response, timeout=args.timeout
        )
        plan_path = plan_dir / "plan.json"
        plan = load_json(plan_path, "suggestion plan")
        if plan.get("schema") != "google-docs-browser-suggestion-plan/v1":
            raise WorkflowFailure("adapter returned an invalid plan")
        plan_sha256 = sha256_file(plan_path)

        stage = "apply"
        apply_dir = run_dir / "apply"
        apply_dir.mkdir(mode=0o700)
        apply_request = run_dir / "apply-request.json"
        apply_response = run_dir / "apply-response.json"
        write_private_json(apply_request, {
            "protocol": "llm-wiki-adapter/v1",
            "adapter_id": ADAPTER_ID,
            "operation": "apply",
            "arguments": {
                "collaboration_resource": plan["collaboration_resource"],
                "plan": str(plan_path),
            },
            "output_dir": str(apply_dir),
            "remote_write": {
                "plan_sha256": plan_sha256,
                "idempotency_key": args.idempotency_key,
                "expected_revision": plan["revision_id"],
            },
            "options": {},
        })
        apply_result = run_adapter(
            llm_wiki,
            apply_request,
            apply_response,
            timeout=args.timeout,
            approved_plan_sha256=plan_sha256,
            require_ok=False,
        )
        receipt_path = apply_response
        if apply_result.get("status") != "ok":
            stage = "recover"
            recover_dir = run_dir / "recover"
            recover_dir.mkdir(mode=0o700)
            recover_request = run_dir / "recover-request.json"
            recover_response = run_dir / "recover-response.json"
            write_private_json(recover_request, {
                "protocol": "llm-wiki-adapter/v1",
                "adapter_id": ADAPTER_ID,
                "operation": "recover",
                "arguments": {
                    "collaboration_resource": plan["collaboration_resource"],
                    "plan": str(plan_path),
                },
                "output_dir": str(recover_dir),
                "remote_write": {
                    "plan_sha256": plan_sha256,
                    "idempotency_key": args.idempotency_key,
                    "expected_revision": plan["revision_id"],
                },
                "options": {},
            })
            apply_result = run_adapter(
                llm_wiki,
                recover_request,
                recover_response,
                timeout=args.timeout,
                approved_plan_sha256=plan_sha256,
            )
            receipt_path = recover_response
        receipt = apply_result.get("remote_receipt")
        verification = receipt.get("verification") if isinstance(receipt, dict) else None
        if not isinstance(verification, dict) or verification.get("status") != "verified":
            raise WorkflowFailure("apply did not return a verified receipt")

        stage = "verify"
        verify_dir = run_dir / "verify"
        verify_dir.mkdir(mode=0o700)
        verify_request = run_dir / "verify-request.json"
        verify_response = run_dir / "verify-response.json"
        write_private_json(verify_request, {
            "protocol": "llm-wiki-adapter/v1",
            "adapter_id": ADAPTER_ID,
            "operation": "verify",
            "arguments": {
                "collaboration_resource": plan["collaboration_resource"],
                "receipt": str(receipt_path),
                "plan": str(plan_path),
            },
            "output_dir": str(verify_dir),
            "options": {},
        })
        verify_result = run_adapter(
            llm_wiki, verify_request, verify_response, timeout=args.timeout
        )
        if verify_result.get("summary", {}).get("verified") is not True:
            raise WorkflowFailure("receipt verification failed")

        print(json.dumps({
            "status": "ok",
            "stage": "complete",
            "adapter_version": plan_result.get("adapter_version"),
            "suggestion_count": apply_result.get("summary", {}).get("suggestion_count"),
            "tracked_changes": apply_result.get("summary", {}).get("tracked_changes") is True,
            "verified": True,
        }, sort_keys=True))
        return 0
    except (KeyError, TypeError, ValueError, WorkflowFailure):
        print(json.dumps({
            "status": "error",
            "stage": stage,
            "verified": False,
        }, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
