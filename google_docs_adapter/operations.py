from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from . import __version__
from .client import GoogleDocsClient, GoogleDocsError
from .document import (
    apply_text_edits,
    collect_suggestion_ids,
    document_id_from_resource,
    projection_hashes,
    tab_text_indexes,
    tab_texts,
    text_hash,
)
from .storage import (
    canonical_json_bytes,
    load_json,
    private_artifact,
    sha256_bytes,
    sha256_file,
    write_private_json,
)

INLINE = "SUGGESTIONS_INLINE"
ACCEPTED = "PREVIEW_SUGGESTIONS_ACCEPTED"
REJECTED = "PREVIEW_WITHOUT_SUGGESTIONS"
PLAN_SCHEMA = "google-docs-suggestion-plan/v1"
EDIT_SPEC_SCHEMA = "google-docs-edit-spec/v1"


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


def _stable_views(client: GoogleDocsClient, document_id: str) -> dict[str, dict[str, Any]]:
    for _attempt in range(3):
        inline = client.get_document(document_id, INLINE)
        accepted = client.get_document(document_id, ACCEPTED)
        rejected = client.get_document(document_id, REJECTED)
        revisions = {str(value.get("revisionId", "")) for value in (inline, accepted, rejected)}
        if len(revisions) == 1 and "" not in revisions:
            return {"inline": inline, "accepted": accepted, "rejected": rejected}
    raise RuntimeError("document changed while projections were being read; retry planning")


def _projection_bundle(views: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        "inline": projection_hashes(views["inline"]),
        "accepted": projection_hashes(views["accepted"]),
        "rejected": projection_hashes(views["rejected"]),
    }


def inspect_document(request: dict[str, Any], client: GoogleDocsClient) -> dict[str, Any]:
    resource = str(request["arguments"]["document_resource"])
    document_id = document_id_from_resource(resource)
    views = _stable_views(client, document_id)
    inline = views["inline"]
    output_dir = Path(request["output_dir"]).resolve(strict=False)
    artifact_path = output_dir / "inspection.json"
    inspection = {
        "schema": "google-docs-inspection/v1",
        "document_resource": resource,
        "revision_id": inline["revisionId"],
        "title": inline.get("title", ""),
        "suggestion_ids": sorted(collect_suggestion_ids(inline)),
        "tabs": {
            mode: tab_texts(document)
            for mode, document in views.items()
        },
        "projection_sha256": _projection_bundle(views),
    }
    write_private_json(artifact_path, inspection)
    run_id = sha256_bytes(canonical_json_bytes({"resource": resource, "revision": inline["revisionId"]}))[:24]
    return _response(
        "inspect",
        "ok",
        run_id,
        summary={
            "tab_count": len(tab_texts(inline)),
            "suggestion_count": len(inspection["suggestion_ids"]),
            "revision_sha256": text_hash(str(inline["revisionId"])),
        },
        artifacts=[private_artifact(artifact_path, "google-docs-inspection")],
    )


def _validate_edit_spec(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("schema") != EDIT_SPEC_SCHEMA:
        raise ValueError(f"edit spec schema must be {EDIT_SPEC_SCHEMA}")
    edits = value.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > 100:
        raise ValueError("edit spec must contain 1-100 edits")
    if not all(isinstance(edit, dict) for edit in edits):
        raise ValueError("every edit must be an object")
    return edits


def plan_suggestions(request: dict[str, Any], client: GoogleDocsClient) -> dict[str, Any]:
    resource = str(request["arguments"]["document_resource"])
    document_id = document_id_from_resource(resource)
    edit_spec_path = Path(request["arguments"]["edit_spec"]).resolve(strict=True)
    edit_spec = load_json(edit_spec_path, "edit specification")
    edits = _validate_edit_spec(edit_spec)
    views = _stable_views(client, document_id)
    inline = views["inline"]
    indexes = tab_text_indexes(inline)
    if not indexes:
        raise ValueError("document has no editable body text")
    resolved: list[dict[str, Any]] = []
    document_ranges: list[tuple[str, int, int]] = []
    normalized_edits: list[dict[str, Any]] = []
    for edit in edits:
        tab_id_value = edit.get("tab_id")
        if tab_id_value is None:
            if len(indexes) != 1:
                raise ValueError("tab_id is required for a document with multiple tabs")
            tab_id = next(iter(indexes))
        elif isinstance(tab_id_value, str) and tab_id_value in indexes:
            tab_id = tab_id_value
        else:
            raise ValueError("edit tab_id is not present in the document")
        find = edit.get("find")
        replace = edit.get("replace")
        occurrence = edit.get("occurrence")
        if not isinstance(find, str) or not find:
            raise ValueError("every edit requires non-empty find text")
        if not isinstance(replace, str) or replace == find:
            raise ValueError("every edit requires different string replace text")
        if occurrence is not None and (not isinstance(occurrence, int) or occurrence < 1):
            raise ValueError("edit occurrence must be a positive integer")
        _flat_start, _flat_end, start, end = indexes[tab_id].locate(find, occurrence)
        for existing_tab, existing_start, existing_end in document_ranges:
            if tab_id == existing_tab and start < existing_end and end > existing_start:
                raise ValueError("edit ranges overlap")
        document_ranges.append((tab_id, start, end))
        normalized = {"tab_id": tab_id, "find": find, "replace": replace}
        if occurrence is not None:
            normalized["occurrence"] = occurrence
        normalized_edits.append(normalized)
        resolved.append(
            {
                **normalized,
                "start_index": start,
                "end_index": end,
            }
        )
    accepted_before = tab_texts(views["accepted"])
    accepted_expected, _accepted_ranges = apply_text_edits(accepted_before, normalized_edits)
    requests: list[dict[str, Any]] = []
    for edit in sorted(resolved, key=lambda item: (item["tab_id"], item["start_index"]), reverse=True):
        range_value: dict[str, Any] = {
            "startIndex": edit["start_index"],
            "endIndex": edit["end_index"],
        }
        location: dict[str, Any] = {"index": edit["start_index"]}
        if edit["tab_id"]:
            range_value["tabId"] = edit["tab_id"]
            location["tabId"] = edit["tab_id"]
        requests.append({"deleteContentRange": {"range": range_value}})
        if edit["replace"]:
            requests.append({"insertText": {"text": edit["replace"], "location": location}})
    plan = {
        "schema": PLAN_SCHEMA,
        "document_resource": resource,
        "revision_id": str(inline["revisionId"]),
        "edit_spec_sha256": sha256_file(edit_spec_path),
        "created_at_unix": int(time.time()),
        "edits": resolved,
        "requests": requests,
        "baseline_suggestion_ids": sorted(collect_suggestion_ids(inline)),
        "projections": {
            "rejected_before": projection_hashes(views["rejected"]),
            "accepted_before": projection_hashes(views["accepted"]),
            "accepted_expected": {
                tab_id: text_hash(text) for tab_id, text in sorted(accepted_expected.items())
            },
        },
    }
    output_dir = Path(request["output_dir"]).resolve(strict=False)
    plan_path = output_dir / "plan.json"
    write_private_json(plan_path, plan)
    plan_sha256 = sha256_file(plan_path)
    run_id = plan_sha256[:24]
    return _response(
        "plan",
        "ok",
        run_id,
        summary={
            "edit_count": len(resolved),
            "request_count": len(requests),
            "plan_sha256": plan_sha256,
            "expected_revision_sha256": text_hash(str(inline["revisionId"])),
            "tracked_changes": True,
        },
        artifacts=[private_artifact(plan_path, "approved-remote-write-plan")],
    )


def _verify_views(
    plan: dict[str, Any],
    views: dict[str, dict[str, Any]],
    suggestion_ids: set[str],
) -> dict[str, Any]:
    rejected_hashes = projection_hashes(views["rejected"])
    accepted_hashes = projection_hashes(views["accepted"])
    expected_rejected = plan["projections"]["rejected_before"]
    expected_accepted = plan["projections"]["accepted_expected"]
    live_ids = collect_suggestion_ids(views["inline"])
    missing_ids = sorted(suggestion_ids - live_ids)
    if rejected_hashes != expected_rejected:
        raise RuntimeError("rejected projection changed; remote write cannot be verified")
    if accepted_hashes != expected_accepted:
        raise RuntimeError("accepted projection does not match the approved edit plan")
    if not suggestion_ids or missing_ids:
        raise RuntimeError("created tracked-change suggestion IDs were not found during read-back")
    return {
        "status": "verified",
        "created_suggestion_ids": sorted(suggestion_ids),
        "suggestion_count": len(suggestion_ids),
        "rejected_projection_sha256": rejected_hashes,
        "accepted_projection_sha256": accepted_hashes,
        "inline_projection_sha256": projection_hashes(views["inline"]),
    }


def _new_suggestion_ids(plan: dict[str, Any], views: dict[str, dict[str, Any]]) -> set[str]:
    return collect_suggestion_ids(views["inline"]) - set(plan["baseline_suggestion_ids"])


def _journal_path(idempotency_key: str) -> Path:
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    root = (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else (Path.home() / ".local" / "state" / "llm-wiki" / "google-docs-editing")
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root / "journal" / f"{text_hash(idempotency_key)}.json"


def apply_suggestions(request: dict[str, Any], client: GoogleDocsClient) -> dict[str, Any]:
    resource = str(request["arguments"]["document_resource"])
    document_id = document_id_from_resource(resource)
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    plan = load_json(plan_path, "suggestion plan")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA}")
    if plan.get("document_resource") != resource:
        raise ValueError("plan document_resource does not match the request")
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
    journal_path = _journal_path(idempotency_key)
    if journal_path.is_file():
        stored = load_json(journal_path, "idempotency journal")
        if stored.get("plan_sha256") != plan_sha256 or stored.get("resource") != resource:
            raise RuntimeError("idempotency key was already used for a different remote write")
        response = stored.get("response")
        if not isinstance(response, dict):
            raise RuntimeError("idempotency journal is invalid")
        return response

    current = client.get_document(document_id, INLINE)
    current_revision = str(current.get("revisionId", ""))
    run_id = text_hash(idempotency_key)[:24]
    if current_revision != expected_revision:
        views = _stable_views(client, document_id)
        candidates = _new_suggestion_ids(plan, views)
        verification = _verify_views(plan, views, candidates)
        verification["recovered_after_revision_change"] = True
        response = _successful_apply_response(
            resource, plan_sha256, idempotency_key, expected_revision,
            str(views["inline"]["revisionId"]), verification, run_id,
        )
        write_private_json(journal_path, {"plan_sha256": plan_sha256, "resource": resource, "response": response})
        return response

    body = {
        "requests": plan["requests"],
        "writeControl": {
            "writeMode": "SUGGEST",
            "requiredRevisionId": expected_revision,
        },
    }
    try:
        result = client.batch_update(document_id, body)
    except GoogleDocsError:
        views = _stable_views(client, document_id)
        candidates = _new_suggestion_ids(plan, views)
        verification = _verify_views(plan, views, candidates)
        verification["recovered_after_ambiguous_transport"] = True
        response = _successful_apply_response(
            resource, plan_sha256, idempotency_key, expected_revision,
            str(views["inline"]["revisionId"]), verification, run_id,
        )
        write_private_json(journal_path, {"plan_sha256": plan_sha256, "resource": resource, "response": response})
        return response
    if result.get("commentUpdateState") != "ALL_SAVED":
        raise RuntimeError(
            f"Google did not save all tracked-change threads: {result.get('commentUpdateState', 'missing state')}"
        )
    created_ids: set[str] = set()
    suggestion_responses = result.get("suggestionResponses", [])
    if isinstance(suggestion_responses, list):
        for suggestion_response in suggestion_responses:
            if isinstance(suggestion_response, dict):
                values = suggestion_response.get("createdSuggestionIds", [])
                if isinstance(values, list):
                    created_ids.update(str(value) for value in values if isinstance(value, str))
    views = _stable_views(client, document_id)
    verification = _verify_views(plan, views, created_ids)
    verification["comment_update_state"] = "ALL_SAVED"
    response = _successful_apply_response(
        resource, plan_sha256, idempotency_key, expected_revision,
        str(views["inline"]["revisionId"]), verification, run_id,
    )
    write_private_json(journal_path, {"plan_sha256": plan_sha256, "resource": resource, "response": response})
    return response


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


def verify_receipt(request: dict[str, Any], client: GoogleDocsClient) -> dict[str, Any]:
    resource = str(request["arguments"]["document_resource"])
    document_id = document_id_from_resource(resource)
    receipt_document = load_json(Path(request["arguments"]["receipt"]).resolve(strict=True), "remote receipt")
    remote_receipt = receipt_document.get("remote_receipt")
    if not isinstance(remote_receipt, dict) or remote_receipt.get("resources") != [resource]:
        raise ValueError("receipt does not belong to the requested document")
    previous = remote_receipt.get("verification")
    if not isinstance(previous, dict):
        raise ValueError("receipt has no verification record")
    views = _stable_views(client, document_id)
    live_ids = collect_suggestion_ids(views["inline"])
    expected_ids = set(previous.get("created_suggestion_ids", []))
    accepted = projection_hashes(views["accepted"])
    rejected = projection_hashes(views["rejected"])
    verified = (
        bool(expected_ids)
        and expected_ids.issubset(live_ids)
        and accepted == previous.get("accepted_projection_sha256")
        and rejected == previous.get("rejected_projection_sha256")
    )
    report = {
        "schema": "google-docs-suggestion-verification/v1",
        "status": "verified" if verified else "drifted",
        "document_resource": resource,
        "receipt_sha256": sha256_file(Path(request["arguments"]["receipt"])),
        "revision_id": str(views["inline"].get("revisionId", "")),
        "suggestions_present": len(expected_ids & live_ids),
        "suggestions_expected": len(expected_ids),
        "accepted_projection_sha256": accepted,
        "rejected_projection_sha256": rejected,
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "verification.json"
    write_private_json(output_path, report)
    run_id = report["receipt_sha256"][:24]
    response = _response(
        "verify",
        "ok" if verified else "error",
        run_id,
        summary={"verified": verified, "suggestion_count": len(expected_ids)},
        artifacts=[private_artifact(output_path, "google-docs-suggestion-verification")],
    )
    if not verified:
        response["errors"] = ["tracked-change receipt no longer matches the live document"]
    return response


def self_test() -> dict[str, Any]:
    synthetic = {
        "revisionId": "synthetic-revision",
        "tabs": [{
            "tabProperties": {"tabId": "synthetic-tab", "title": "Synthetic"},
            "documentTab": {"body": {"content": [{
                "startIndex": 1,
                "endIndex": 13,
                "paragraph": {"elements": [{
                    "startIndex": 1,
                    "endIndex": 13,
                    "textRun": {"content": "hello 🌎!\n"},
                }]},
            }]}}
        }],
    }
    index = tab_text_indexes(synthetic)["synthetic-tab"]
    _flat_start, _flat_end, start, end = index.locate("🌎")
    if (start, end) != (7, 9):
        raise RuntimeError("UTF-16 index self-test failed")
    return _response(
        "self-test",
        "ok",
        "synthetic-self-test",
        summary={"utf16_indexes": True, "tracked_changes_required": True},
    )


def execute(request: dict[str, Any], client: GoogleDocsClient | None = None) -> dict[str, Any]:
    operation = request.get("operation")
    try:
        if operation == "self-test":
            return self_test()
        active_client = client or GoogleDocsClient()
        if operation == "inspect":
            return inspect_document(request, active_client)
        if operation == "plan":
            return plan_suggestions(request, active_client)
        if operation == "apply":
            return apply_suggestions(request, active_client)
        if operation == "verify":
            return verify_receipt(request, active_client)
        return _error(str(operation), "unsupported operation")
    except Exception as exc:
        return _error(str(operation), str(exc))
