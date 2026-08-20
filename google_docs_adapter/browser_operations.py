from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .browser_executor import (
    MAX_BROWSER_EDITS,
    assert_expected_document_url,
    compile_inspection_program,
    compile_suggestion_program,
    document_id_from_collaboration,
    document_id_from_expected_url,
    snapshot_sha256,
    snapshot_text_fragments,
)
from .storage import (
    canonical_json_bytes,
    load_json,
    private_artifact,
    sha256_bytes,
    sha256_file,
    write_private_json,
)

COLLABORATION_RESOURCE = "browser-collaboration:active-tab"
INSPECTION_SCHEMA = "google-docs-browser-inspection/v1"
PLAN_SCHEMA = "google-docs-browser-suggestion-plan/v1"
EDIT_SPEC_SCHEMA = "google-docs-edit-spec/v1"


class BrowserClient(Protocol):
    def collaborations(self) -> list[dict[str, str]]: ...

    def collaboration_for_url(self, raw_url: str) -> dict[str, str] | None: ...

    def run(
        self,
        program: dict[str, Any],
        *,
        private_values: dict[str, str] | None = None,
        before_mutation: Any | None = None,
    ) -> dict[str, Any]: ...


def _default_browser() -> BrowserClient:
    try:
        from browser_executor.client import BrowserExecutorClient
    except ImportError as exc:
        raise RuntimeError(
            "the shared browser executor package is not installed in this adapter environment"
        ) from exc
    return BrowserExecutorClient()


def _response(operation: str, status: str, run_id: str, **values: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "protocol": "llm-wiki-adapter/v1",
        "adapter_id": "google-docs-editing",
        "adapter_version": __version__,
        "operation": operation,
        "status": status,
        "run_id": run_id,
        "summary": {},
        "artifacts": [],
    }
    response.update(values)
    return response


def _error(operation: str, message: str, run_id: str = "failed") -> dict[str, Any]:
    value = _response(operation, "error", run_id)
    value["errors"] = [message]
    return value


def _collaboration_resource(request: dict[str, Any]) -> str:
    value = request.get("arguments", {}).get("collaboration_resource")
    if value != COLLABORATION_RESOURCE:
        raise ValueError(
            f"collaboration_resource must be the registered {COLLABORATION_RESOURCE} capability"
        )
    return value


def _live_collaboration(
    request: dict[str, Any],
    browser: BrowserClient,
) -> tuple[dict[str, str], str]:
    _collaboration_resource(request)
    collaborations = browser.collaborations()
    if not collaborations:
        raise RuntimeError(
            "no page is exposed; open the requested Google Doc and click the LLM Wiki Browser Executor"
        )
    expected = request.get("arguments", {}).get("expected_document_url")
    expected_document_id = document_id_from_expected_url(expected)
    matches: list[dict[str, str]] = []
    for candidate in collaborations:
        try:
            if document_id_from_collaboration(candidate) == expected_document_id:
                matches.append(candidate)
        except ValueError:
            continue
    if not matches:
        raise RuntimeError("none of the explicitly shared tabs is the requested Google Doc")
    if len(matches) > 1:
        raise RuntimeError("more than one explicitly shared tab matches the requested Google Doc")
    collaboration = matches[0]
    document_id = assert_expected_document_url(expected, collaboration)
    return collaboration, document_id


def _run_inspection(
    browser: BrowserClient,
    collaboration: dict[str, str],
    document_id: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    result = browser.run(compile_inspection_program(document_id, collaboration))
    if not isinstance(result, dict) or result.get("status") != "ok":
        error = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError(f"browser inspection failed: {error or 'invalid executor result'}")
    private = result.get("private")
    snapshot = private.get("docs.ax") if isinstance(private, dict) else None
    if not isinstance(snapshot, list):
        raise RuntimeError("browser inspection did not return the private accessibility projection")
    revision = snapshot_sha256(snapshot)
    return snapshot, revision, snapshot_text_fragments(snapshot)


def inspect_document(
    request: dict[str, Any],
    browser: BrowserClient,
) -> dict[str, Any]:
    collaboration, document_id = _live_collaboration(request, browser)
    snapshot, revision, fragments = _run_inspection(browser, collaboration, document_id)
    inspection = {
        "schema": INSPECTION_SCHEMA,
        "collaboration_resource": COLLABORATION_RESOURCE,
        "target_url": collaboration["url"],
        "document_id": document_id,
        "revision_id": revision,
        "text_fragments": fragments,
        "accessibility_projection": snapshot,
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "inspection.json"
    write_private_json(output_path, inspection)
    run_id = sha256_bytes(canonical_json_bytes({
        "resource": COLLABORATION_RESOURCE,
        "target_url": collaboration["url"],
        "revision": revision,
    }))[:24]
    return _response(
        "inspect",
        "ok",
        run_id,
        summary={
            "revision_sha256": revision,
            "private_text_fragment_count": len(fragments),
            "private_ax_node_count": len(snapshot),
            "oauth_used": False,
        },
        artifacts=[private_artifact(output_path, "google-docs-browser-inspection")],
    )


def _validate_edit_spec(value: dict[str, Any]) -> list[dict[str, str]]:
    if value.get("schema") != EDIT_SPEC_SCHEMA:
        raise ValueError(f"edit spec schema must be {EDIT_SPEC_SCHEMA}")
    edits = value.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BROWSER_EDITS:
        raise ValueError(f"edit spec must contain 1-{MAX_BROWSER_EDITS} edits")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("browser edit entries must be objects")
        if set(edit) == {"append"}:
            append = edit.get("append")
            if not isinstance(append, str) or not append:
                raise ValueError("append edits require non-empty text")
            normalized.append({"append": append})
            continue
        if set(edit) != {"find", "replace"}:
            raise ValueError("browser edit entries accept an exact replacement or append")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find:
            raise ValueError("every edit requires non-empty find text")
        if not isinstance(replace, str) or replace == find:
            raise ValueError("every edit requires different string replace text")
        if find in seen:
            raise ValueError("duplicate find text is not allowed in one suggestion plan")
        if any(find in prior or prior in find for prior in seen):
            raise ValueError("overlapping find text is not allowed in one suggestion plan")
        seen.add(find)
        normalized.append({"find": find, "replace": replace})
    append_count = sum("append" in edit for edit in normalized)
    if append_count and (append_count != 1 or len(normalized) != 1):
        raise ValueError("append plans must contain exactly one append edit")
    return normalized


def _fragment_contains(fragments: list[str], text: str) -> bool:
    return any(text in fragment for fragment in fragments)


def _planned_text(edit: dict[str, Any]) -> str:
    value = edit.get("replace", edit.get("append"))
    if not isinstance(value, str) or not value:
        raise ValueError("suggestion plan contains an invalid edit")
    return value


def plan_suggestions(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    collaboration, document_id = _live_collaboration(request, browser)
    edit_spec_path = Path(request["arguments"]["edit_spec"]).resolve(strict=True)
    edit_spec = load_json(edit_spec_path, "edit specification")
    edits = _validate_edit_spec(edit_spec)
    _snapshot, revision, fragments = _run_inspection(browser, collaboration, document_id)
    missing = [
        index + 1
        for index, edit in enumerate(edits)
        if "find" in edit and not _fragment_contains(fragments, edit["find"])
    ]
    if missing:
        raise ValueError(
            "browser inspection could not find the requested source text for edit(s): "
            + ", ".join(map(str, missing))
        )
    plan = {
        "schema": PLAN_SCHEMA,
        "write_transport": "shared-browser-executor-suggesting-ui",
        "collaboration_resource": COLLABORATION_RESOURCE,
        "target": {
            "url": collaboration["url"],
            "collaboration_id": collaboration["collaboration_id"],
            "document_id": document_id,
        },
        "revision_id": revision,
        "edit_spec_sha256": sha256_file(edit_spec_path),
        "created_at_unix": int(time.time()),
        "edits": edits,
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "plan.json"
    write_private_json(output_path, plan)
    plan_sha256 = sha256_file(output_path)
    return _response(
        "plan",
        "ok",
        plan_sha256[:24],
        summary={
            "edit_count": len(edits),
            "plan_sha256": plan_sha256,
            "expected_revision_sha256": revision,
            "tracked_changes": True,
            "oauth_used": False,
        },
        artifacts=[private_artifact(output_path, "approved-remote-write-plan")],
    )


def _journal_path(idempotency_key: str, plan_path: Path) -> Path:
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("remote_write idempotency_key must contain 1-256 characters")
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    root = (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else plan_path.parent / ".google-docs-state"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root / "browser-journal" / f"{sha256_bytes(idempotency_key.encode('utf-8'))}.json"


def _validate_plan_request(request: dict[str, Any]) -> tuple[dict[str, Any], Path, str, str, str]:
    resource = _collaboration_resource(request)
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    plan = load_json(plan_path, "suggestion plan")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA}")
    if plan.get("write_transport") != "shared-browser-executor-suggesting-ui":
        raise ValueError("plan does not authorize the shared browser Suggesting transport")
    if plan.get("collaboration_resource") != resource:
        raise ValueError("plan collaboration_resource does not match the request")
    remote_write = request.get("remote_write")
    if not isinstance(remote_write, dict):
        raise ValueError("apply requires remote_write governance")
    plan_sha256 = sha256_file(plan_path)
    if remote_write.get("plan_sha256") != plan_sha256:
        raise ValueError("remote_write plan_sha256 does not match plan")
    expected_revision = str(remote_write.get("expected_revision", ""))
    if expected_revision != str(plan.get("revision_id", "")):
        raise ValueError("remote_write expected_revision does not match plan revision")
    idempotency_key = str(remote_write.get("idempotency_key", ""))
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("remote_write idempotency_key must contain 1-256 characters")
    return plan, plan_path, plan_sha256, expected_revision, idempotency_key


def _same_plan_collaboration(plan: dict[str, Any], collaboration: dict[str, str]) -> str:
    target = plan.get("target")
    if not isinstance(target, dict):
        raise ValueError("plan has no exact collaboration target")
    document_id = document_id_from_collaboration(collaboration)
    if (
        target.get("document_id") != document_id
        or target.get("url") != collaboration.get("url")
        or target.get("collaboration_id") != collaboration.get("collaboration_id")
    ):
        raise RuntimeError(
            "the active collaboration changed after planning; click the extension and create a new plan"
        )
    return document_id


def _verify_after_snapshot(
    plan: dict[str, Any],
    snapshot: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    after_revision = snapshot_sha256(snapshot)
    if after_revision == plan["revision_id"]:
        raise RuntimeError("browser read-back did not observe a changed document projection")
    fragments = snapshot_text_fragments(snapshot)
    missing = [
        index + 1
        for index, edit in enumerate(plan["edits"])
        if not _fragment_contains(fragments, _planned_text(edit))
    ]
    if missing:
        raise RuntimeError(
            "browser read-back did not observe replacement text for edit(s): "
            + ", ".join(map(str, missing))
        )
    return after_revision, {
        "status": "verified",
        "write_transport": "shared-browser-executor-suggesting-ui",
        "suggesting_mode_asserted_before_and_after": True,
        "unique_find_preconditions_asserted_before_mutation": all(
            "find" in edit for edit in plan["edits"]
        ),
        "append_position_precondition_asserted_before_mutation": all(
            "append" in edit for edit in plan["edits"]
        ),
        "planned_text_observed_after_mutation": True,
        "replacement_text_observed_after_mutation": all(
            "replace" in edit for edit in plan["edits"]
        ),
        "suggestion_count": len(plan["edits"]),
        "before_projection_sha256": plan["revision_id"],
        "after_projection_sha256": after_revision,
        "target_url_sha256": sha256_bytes(plan["target"]["url"].encode("utf-8")),
    }


def _successful_apply_response(
    resource: str,
    plan_sha256: str,
    idempotency_key: str,
    before_revision: str,
    after_revision: str,
    verification: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    return _response(
        "apply",
        "ok",
        run_id,
        summary={
            "tracked_changes": True,
            "suggestion_count": verification["suggestion_count"],
            "verified": True,
            "oauth_used": False,
        },
        remote_receipt={
            "status": "verified",
            "resources": [resource],
            "plan_sha256": plan_sha256,
            "idempotency_key": idempotency_key,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "verification": verification,
        },
    )


def apply_suggestions(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    plan, plan_path, plan_sha256, expected_revision, idempotency_key = _validate_plan_request(request)
    resource = COLLABORATION_RESOURCE
    journal_path = _journal_path(idempotency_key, plan_path)
    run_id = sha256_bytes(idempotency_key.encode("utf-8"))[:24]
    if journal_path.is_file():
        stored = load_json(journal_path, "idempotency journal")
        if stored.get("plan_sha256") != plan_sha256 or stored.get("resource") != resource:
            raise RuntimeError("idempotency key was already used for a different remote write")
        response = stored.get("response")
        if isinstance(response, dict):
            return response
        raise RuntimeError(
            "a prior browser write crossed the mutation boundary without a verified receipt; refusing a duplicate"
        )

    target = plan.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("url"), str):
        raise ValueError("plan has no exact collaboration target")
    collaboration = browser.collaboration_for_url(target["url"])
    if collaboration is None:
        raise RuntimeError("the planned Google Doc is no longer exposed")
    document_id = _same_plan_collaboration(plan, collaboration)
    _before_snapshot, live_revision, _fragments = _run_inspection(
        browser, collaboration, document_id,
    )
    if live_revision != expected_revision:
        raise RuntimeError("the browser-visible document changed after planning; no edit was sent")

    pending_written = False

    def mark_pending() -> None:
        nonlocal pending_written
        write_private_json(journal_path, {
            "status": "pending",
            "plan_sha256": plan_sha256,
            "resource": resource,
            "expected_revision": expected_revision,
            "idempotency_key_sha256": sha256_bytes(idempotency_key.encode("utf-8")),
        })
        pending_written = True

    program, private_values = compile_suggestion_program(
        document_id,
        plan_sha256,
        list(plan["edits"]),
        collaboration,
    )
    result = browser.run(
        program,
        private_values=private_values,
        before_mutation=mark_pending,
    )
    if not pending_written:
        error = result.get("error") if isinstance(result, dict) else None
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise RuntimeError(
                "browser suggestion preflight failed before authorization; no edit was sent: "
                + (error or "invalid executor result")
            )
        raise RuntimeError("browser executor returned without crossing the governed mutation boundary")
    if not isinstance(result, dict) or result.get("status") != "ok":
        error = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError(f"browser suggestion write failed after authorization: {error or 'invalid result'}")
    private = result.get("private")
    after_snapshot = private.get("docs.after-ax") if isinstance(private, dict) else None
    if not isinstance(after_snapshot, list):
        raise RuntimeError("browser executor did not return the private read-back projection")
    after_revision, verification = _verify_after_snapshot(plan, after_snapshot)
    response = _successful_apply_response(
        resource,
        plan_sha256,
        idempotency_key,
        expected_revision,
        after_revision,
        verification,
        run_id,
    )
    write_private_json(journal_path, {
        "plan_sha256": plan_sha256,
        "resource": resource,
        "response": response,
    })
    return response


def verify_receipt(request: dict[str, Any], browser: BrowserClient) -> dict[str, Any]:
    resource = _collaboration_resource(request)
    receipt_path = Path(request["arguments"]["receipt"]).resolve(strict=True)
    receipt_document = load_json(receipt_path, "remote receipt")
    remote_receipt = receipt_document.get("remote_receipt")
    if not isinstance(remote_receipt, dict) or remote_receipt.get("resources") != [resource]:
        raise ValueError("receipt does not belong to the active-tab collaboration capability")
    previous = remote_receipt.get("verification")
    if not isinstance(previous, dict):
        raise ValueError("receipt has no browser verification record")
    matches = [
        value for value in browser.collaborations()
        if sha256_bytes(value["url"].encode("utf-8")) == previous.get("target_url_sha256")
    ]
    if not matches:
        raise RuntimeError("the receipted Google Doc is not explicitly shared for verification")
    if len(matches) > 1:
        raise RuntimeError("the receipted Google Doc has an ambiguous collaboration grant")
    collaboration = matches[0]
    document_id = document_id_from_collaboration(collaboration)
    snapshot, revision, fragments = _run_inspection(browser, collaboration, document_id)
    target_matches = (
        sha256_bytes(collaboration["url"].encode("utf-8"))
        == previous.get("target_url_sha256")
    )
    receipt_projection_matches = revision == remote_receipt.get("after_revision")
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    if sha256_file(plan_path) != remote_receipt.get("plan_sha256"):
        raise ValueError("verification plan does not match the remote receipt")
    plan = load_json(plan_path, "suggestion plan")
    target = plan.get("target")
    if not isinstance(target, dict) or target.get("document_id") != document_id:
        raise ValueError("verification plan does not belong to the exposed Google Doc")
    planned_text_matches = all(
        _fragment_contains(fragments, _planned_text(edit))
        for edit in plan.get("edits", [])
    )
    verified = target_matches and planned_text_matches
    report = {
        "schema": "google-docs-browser-suggestion-verification/v1",
        "status": "verified" if verified else "drifted",
        "collaboration_resource": resource,
        "receipt_sha256": sha256_file(receipt_path),
        "revision_id": revision,
        "target_matches": target_matches,
        "receipt_projection_matches": receipt_projection_matches,
        "planned_text_matches": planned_text_matches,
        "private_ax_node_count": len(snapshot),
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "verification.json"
    write_private_json(output_path, report)
    response = _response(
        "verify",
        "ok" if verified else "error",
        report["receipt_sha256"][:24],
        summary={"verified": verified, "oauth_used": False},
        artifacts=[private_artifact(output_path, "google-docs-browser-suggestion-verification")],
    )
    if not verified:
        response["errors"] = ["browser receipt no longer matches the exposed document projection"]
    return response


def self_test() -> dict[str, Any]:
    return _response(
        "self-test",
        "ok",
        "synthetic-browser-self-test",
        summary={
            "tracked_changes_required": True,
            "write_transport": "shared-browser-executor-suggesting-ui",
            "active_tab_collaboration": True,
            "explicit_multi_tab_workspace": True,
            "exact_document_selection": True,
            "oauth_used": False,
        },
    )


def execute(
    request: dict[str, Any],
    browser: BrowserClient | None = None,
) -> dict[str, Any]:
    operation = request.get("operation")
    try:
        if operation == "self-test":
            return self_test()
        active_browser = browser or _default_browser()
        if operation == "inspect":
            return inspect_document(request, active_browser)
        if operation == "plan":
            return plan_suggestions(request, active_browser)
        if operation == "apply":
            return apply_suggestions(request, active_browser)
        if operation == "verify":
            return verify_receipt(request, active_browser)
        return _error(str(operation), "unsupported operation")
    except Exception as exc:
        return _error(str(operation), str(exc))
