from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Protocol

from . import __version__
from .browser_executor import MAX_BROWSER_EDITS, document_id_from_expected_url
from .oauth import get_access_token
from .storage import (
    canonical_json_bytes,
    load_json,
    private_artifact,
    sha256_bytes,
    sha256_file,
    write_private_json,
)

API_RESOURCE = "google-docs-api:authorized-files"
API_BASE_URL = "https://docs.googleapis.com/v1"
EDIT_SPEC_SCHEMA = "google-docs-edit-spec/v1"
INSPECTION_SCHEMA = "google-docs-api-inspection/v1"
PLAN_SCHEMA = "google-docs-api-suggestion-plan/v3"
VERIFICATION_SCHEMA = "google-docs-api-suggestion-verification/v1"
WRITE_TRANSPORT = "google-docs-api-suggest-picker-oauth-v1"


class DocsApiClient(Protocol):
    def get_document(self, document_id: str) -> dict[str, Any]: ...

    def batch_update(
        self, document_id: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...


class GoogleDocsRestClient:
    """Small REST client that keeps bearer credentials out of files and errors."""

    def __init__(self, access_token: str, timeout_seconds: float = 30.0) -> None:
        if not access_token.strip():
            raise ValueError("Google Docs API access token is empty")
        self._access_token = access_token.strip()
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "GoogleDocsRestClient":
        return cls(get_access_token())

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        payload = canonical_json_bytes(body) if body is not None else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = ""
            try:
                value = json.loads(exc.read().decode("utf-8", errors="replace"))
                error = value.get("error") if isinstance(value, dict) else None
                if isinstance(error, dict) and isinstance(error.get("status"), str):
                    status = f" ({error['status']})"
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            raise RuntimeError(
                f"Google Docs API {method} failed with HTTP {exc.code}{status}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Google Docs API {method} transport failed") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Google Docs API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Google Docs API returned a non-object response")
        return value

    def get_document(self, document_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/documents/{urllib.parse.quote(document_id, safe='')}",
            query={
                "includeTabsContent": "true",
                "suggestionsViewMode": "SUGGESTIONS_INLINE",
                "commentsViewMode": "COMMENTS_VIEW_MODE_INCLUDED",
            },
        )

    def batch_update(self, document_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/documents/{urllib.parse.quote(document_id, safe='')}:batchUpdate",
            body=body,
        )


def _response(
    operation: str, status: str, run_id: str, **values: Any
) -> dict[str, Any]:
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


def _api_resource(request: dict[str, Any]) -> str:
    value = request.get("arguments", {}).get("api_resource")
    if value != API_RESOURCE:
        raise ValueError(
            f"api_resource must be the registered {API_RESOURCE} capability"
        )
    return value


def _target(request: dict[str, Any]) -> tuple[str, str]:
    _api_resource(request)
    expected_url = request.get("arguments", {}).get("expected_document_url")
    if not isinstance(expected_url, str):
        raise ValueError("expected_document_url must be a Google Docs URL")
    return expected_url, document_id_from_expected_url(expected_url)


def _validate_document(document: dict[str, Any], document_id: str) -> str:
    if document.get("documentId") != document_id:
        raise RuntimeError("Google Docs API returned a different document")
    revision_id = document.get("revisionId")
    if not isinstance(revision_id, str) or not revision_id:
        raise RuntimeError("Google Docs API response has no revisionId")
    if document.get("suggestionsViewMode") not in (None, "SUGGESTIONS_INLINE"):
        raise RuntimeError(
            "Google Docs API did not return the required inline suggestions view"
        )
    return revision_id


def _flatten_tabs(tabs: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if not isinstance(value, list):
            return
        for tab in value:
            if not isinstance(tab, dict):
                continue
            result.append(tab)
            visit(tab.get("childTabs"))

    visit(tabs)
    return result


def _document_tabs(
    document: dict[str, Any],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    result: list[tuple[str, str, list[dict[str, Any]]]] = []
    for tab in _flatten_tabs(document.get("tabs")):
        properties = tab.get("tabProperties")
        document_tab = tab.get("documentTab")
        if not isinstance(properties, dict) or not isinstance(document_tab, dict):
            continue
        tab_id = properties.get("tabId")
        title = properties.get("title", "")
        body = document_tab.get("body")
        content = body.get("content") if isinstance(body, dict) else None
        if isinstance(tab_id, str) and isinstance(content, list):
            result.append((tab_id, title if isinstance(title, str) else "", content))
    if result:
        return result
    body = document.get("body")
    content = body.get("content") if isinstance(body, dict) else None
    if isinstance(content, list):
        return [("", "", content)]
    raise RuntimeError("Google Docs API response has no document body")


def _walk_structural_content(
    content: list[dict[str, Any]],
) -> Iterator[list[dict[str, Any]]]:
    for structural in content:
        if not isinstance(structural, dict):
            continue
        paragraph = structural.get("paragraph")
        if isinstance(paragraph, dict):
            elements = paragraph.get("elements")
            if isinstance(elements, list):
                yield [element for element in elements if isinstance(element, dict)]
        table = structural.get("table")
        if isinstance(table, dict):
            rows = table.get("tableRows")
            if isinstance(rows, list):
                for row in rows:
                    cells = row.get("tableCells") if isinstance(row, dict) else None
                    if not isinstance(cells, list):
                        continue
                    for cell in cells:
                        nested = cell.get("content") if isinstance(cell, dict) else None
                        if isinstance(nested, list):
                            yield from _walk_structural_content(nested)
        table_of_contents = structural.get("tableOfContents")
        if isinstance(table_of_contents, dict):
            nested = table_of_contents.get("content")
            if isinstance(nested, list):
                yield from _walk_structural_content(nested)


def _text_runs(
    document: dict[str, Any],
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for tab_id, _title, content in _document_tabs(document):
        for group, elements in enumerate(_walk_structural_content(content)):
            for element in elements:
                text_run = element.get("textRun")
                start = element.get("startIndex")
                end = element.get("endIndex")
                text = text_run.get("content") if isinstance(text_run, dict) else None
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or not isinstance(text, str)
                    or start < 0
                    or end < start
                ):
                    continue
                insertion_ids = text_run.get("suggestedInsertionIds", [])
                deletion_ids = text_run.get("suggestedDeletionIds", [])
                style_changes = text_run.get("suggestedTextStyleChanges", {})
                runs.append(
                    {
                        "tab_id": tab_id,
                        "group": group,
                        "start_index": start,
                        "end_index": end,
                        "text": text,
                        "suggested_insertion_ids": [
                            value for value in insertion_ids if isinstance(value, str)
                        ]
                        if isinstance(insertion_ids, list)
                        else [],
                        "suggested_deletion_ids": [
                            value for value in deletion_ids if isinstance(value, str)
                        ]
                        if isinstance(deletion_ids, list)
                        else [],
                        "suggested_style_ids": [
                            value for value in style_changes if isinstance(value, str)
                        ]
                        if isinstance(style_changes, dict)
                        else [],
                    }
                )
    return sorted(runs, key=lambda value: (value["tab_id"], value["start_index"]))


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _tab_character_map(
    runs: list[dict[str, Any]], tab_id: str
) -> tuple[str, list[int | None], list[int | None], list[bool]]:
    text: list[str] = []
    starts: list[int | None] = []
    ends: list[int | None] = []
    suggested: list[bool] = []
    previous_end: int | None = None
    previous_group: int | None = None
    for run in (value for value in runs if value["tab_id"] == tab_id):
        start = run["start_index"]
        if previous_end is not None and (
            start != previous_end or run["group"] != previous_group
        ):
            text.append("\x00")
            starts.append(None)
            ends.append(None)
            suggested.append(False)
        offset = start
        is_suggested = bool(
            run["suggested_insertion_ids"]
            or run["suggested_deletion_ids"]
            or run["suggested_style_ids"]
        )
        for character in run["text"]:
            width = _utf16_length(character)
            text.append(character)
            starts.append(offset)
            ends.append(offset + width)
            suggested.append(is_suggested)
            offset += width
        if offset != run["end_index"]:
            raise RuntimeError(
                "Google Docs API text indexes do not match UTF-16 content"
            )
        previous_end = run["end_index"]
        previous_group = run["group"]
    return "".join(text), starts, ends, suggested


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
            raise ValueError("API edit entries must be objects")
        if set(edit) == {"append"}:
            append = edit.get("append")
            if not isinstance(append, str) or not append:
                raise ValueError("append edits require non-empty text")
            normalized.append({"append": append})
            continue
        if set(edit) != {"find", "replace"}:
            raise ValueError("API edit entries accept an exact replacement or append")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find:
            raise ValueError("every edit requires non-empty find text")
        if "\x00" in find:
            raise ValueError("find text cannot contain a null character")
        if not isinstance(replace, str) or replace == find:
            raise ValueError("every edit requires different string replace text")
        if find in seen:
            raise ValueError(
                "duplicate find text is not allowed in one suggestion plan"
            )
        if any(find in prior or prior in find for prior in seen):
            raise ValueError(
                "overlapping find text is not allowed in one suggestion plan"
            )
        seen.add(find)
        normalized.append({"find": find, "replace": replace})
    append_count = sum("append" in edit for edit in normalized)
    if append_count and (append_count != 1 or len(normalized) != 1):
        raise ValueError("append plans must contain exactly one append edit")
    return normalized


def _find_unique_range(document: dict[str, Any], find: str) -> dict[str, Any]:
    runs = _text_runs(document)
    matches: list[dict[str, Any]] = []
    for tab_id, _title, _content in _document_tabs(document):
        text, starts, ends, suggested = _tab_character_map(runs, tab_id)
        offset = 0
        while True:
            position = text.find(find, offset)
            if position < 0:
                break
            last = position + len(find) - 1
            start_index = starts[position]
            end_index = ends[last]
            contiguous = start_index is not None and end_index is not None
            if contiguous:
                for index in range(position + 1, last + 1):
                    if starts[index] != ends[index - 1]:
                        contiguous = False
                        break
            if contiguous:
                matches.append(
                    {
                        "tab_id": tab_id,
                        "start_index": start_index,
                        "end_index": end_index,
                        "touches_existing_suggestion": any(
                            suggested[position : last + 1]
                        ),
                    }
                )
            offset = position + 1
    if len(matches) != 1:
        raise ValueError(
            f"exact source text must occur once across all document tabs; observed {len(matches)}"
        )
    if matches[0]["touches_existing_suggestion"]:
        raise ValueError("exact source text overlaps an existing suggestion")
    matches[0].pop("touches_existing_suggestion")
    return matches[0]


def _location(index: int, tab_id: str) -> dict[str, Any]:
    value: dict[str, Any] = {"index": index}
    if tab_id:
        value["tabId"] = tab_id
    return value


def _range(start: int, end: int, tab_id: str) -> dict[str, Any]:
    value: dict[str, Any] = {"startIndex": start, "endIndex": end}
    if tab_id:
        value["tabId"] = tab_id
    return value


def _compile_requests(
    document: dict[str, Any], edits: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if edits and "append" in edits[0]:
        tabs = _document_tabs(document)
        if len(tabs) != 1:
            raise ValueError(
                "the API transport only appends to single-tab documents; use an exact replacement"
            )
        tab_id = tabs[0][0]
        end: dict[str, Any] = {}
        if tab_id:
            end["tabId"] = tab_id
        compiled = [
            {
                "insertText": {
                    "text": edits[0]["append"],
                    "endOfSegmentLocation": end,
                }
            }
        ]
        return compiled, [{"kind": "append", "tab_id": tab_id}]

    resolved: list[tuple[dict[str, str], dict[str, Any]]] = []
    for edit in edits:
        resolved.append((edit, _find_unique_range(document, edit["find"])))
    occupied: set[tuple[str, int]] = set()
    for _edit, target in resolved:
        for index in range(target["start_index"], target["end_index"]):
            marker = (target["tab_id"], index)
            if marker in occupied:
                raise ValueError("resolved API edit ranges overlap")
            occupied.add(marker)
    resolved.sort(
        key=lambda value: (value[1]["tab_id"], value[1]["start_index"]),
        reverse=True,
    )
    requests: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for edit, target in resolved:
        tab_id = target["tab_id"]
        start = target["start_index"]
        end = target["end_index"]
        requests.append({"deleteContentRange": {"range": _range(start, end, tab_id)}})
        if edit["replace"]:
            requests.append(
                {
                    "insertText": {
                        "text": edit["replace"],
                        "location": _location(start, tab_id),
                    }
                }
            )
        targets.append(
            {
                "kind": "replace",
                "tab_id": tab_id,
                "start_index": start,
                "end_index": end,
                "find": edit["find"],
                "replace": edit["replace"],
            }
        )
    return requests, targets


def _suggestion_ids(document: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    suggestions = document.get("suggestions")
    if isinstance(suggestions, list):
        for suggestion in suggestions:
            value = (
                suggestion.get("suggestionId") if isinstance(suggestion, dict) else None
            )
            if isinstance(value, str) and value:
                result.add(value)
    for run in _text_runs(document):
        result.update(run["suggested_insertion_ids"])
        result.update(run["suggested_deletion_ids"])
        result.update(run["suggested_style_ids"])
    return result


def _inspection(document: dict[str, Any], expected_url: str) -> dict[str, Any]:
    tabs: list[dict[str, Any]] = []
    runs = _text_runs(document)
    for tab_id, title, _content in _document_tabs(document):
        text = "".join(run["text"] for run in runs if run["tab_id"] == tab_id)
        tabs.append({"tab_id": tab_id, "title": title, "text": text})
    return {
        "schema": INSPECTION_SCHEMA,
        "write_transport": WRITE_TRANSPORT,
        "api_resource": API_RESOURCE,
        "target_url": expected_url,
        "document_id": document["documentId"],
        "revision_id": document["revisionId"],
        "title": document.get("title", ""),
        "tabs": tabs,
        "open_suggestion_ids": sorted(_suggestion_ids(document)),
    }


def inspect_document(request: dict[str, Any], client: DocsApiClient) -> dict[str, Any]:
    expected_url, document_id = _target(request)
    document = client.get_document(document_id)
    revision_id = _validate_document(document, document_id)
    inspection = _inspection(document, expected_url)
    output_path = (
        Path(request["output_dir"]).resolve(strict=False) / "api-inspection.json"
    )
    write_private_json(output_path, inspection)
    run_id = sha256_bytes(
        canonical_json_bytes(
            {
                "resource": API_RESOURCE,
                "document_id": document_id,
                "revision_id": revision_id,
            }
        )
    )[:24]
    return _response(
        "api-inspect",
        "ok",
        run_id,
        summary={
            "revision_sha256": sha256_bytes(revision_id.encode("utf-8")),
            "tab_count": len(inspection["tabs"]),
            "open_suggestion_count": len(inspection["open_suggestion_ids"]),
            "oauth_used": True,
            "suggestions_api_ga": True,
        },
        artifacts=[private_artifact(output_path, "google-docs-api-inspection")],
    )


def plan_suggestions(request: dict[str, Any], client: DocsApiClient) -> dict[str, Any]:
    expected_url, document_id = _target(request)
    edit_spec_path = Path(request["arguments"]["edit_spec"]).resolve(strict=True)
    edit_spec = load_json(edit_spec_path, "edit specification")
    edits = _validate_edit_spec(edit_spec)
    document = client.get_document(document_id)
    revision_id = _validate_document(document, document_id)
    requests, targets = _compile_requests(document, edits)
    plan = {
        "schema": PLAN_SCHEMA,
        "write_transport": WRITE_TRANSPORT,
        "api_resource": API_RESOURCE,
        "target": {"url": expected_url, "document_id": document_id},
        "revision_id": revision_id,
        "edit_spec_sha256": sha256_file(edit_spec_path),
        "created_at_unix": int(time.time()),
        "edits": edits,
        "targets": targets,
        "requests": requests,
        "preexisting_suggestion_ids": sorted(_suggestion_ids(document)),
    }
    output_path = Path(request["output_dir"]).resolve(strict=False) / "api-plan.json"
    write_private_json(output_path, plan)
    plan_sha256 = sha256_file(output_path)
    return _response(
        "api-plan",
        "ok",
        plan_sha256[:24],
        summary={
            "edit_count": len(edits),
            "api_request_count": len(requests),
            "plan_sha256": plan_sha256,
            "expected_revision_sha256": sha256_bytes(revision_id.encode("utf-8")),
            "tracked_changes": True,
            "oauth_used": True,
            "suggestions_api_ga": True,
        },
        artifacts=[private_artifact(output_path, "approved-remote-write-plan")],
    )


def _journal_path(idempotency_key: str, plan_path: Path) -> Path:
    raw = os.environ.get("LLM_WIKI_GOOGLE_DOCS_STATE_DIR")
    root = (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else (
            Path.home()
            / ".local"
            / "state"
            / "llm-wiki"
            / "google-docs-editing"
        ).resolve(strict=False)
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    name = sha256_bytes(idempotency_key.encode("utf-8"))
    return root / "docs-api-journal" / f"{name}.json"


def _validate_plan_request(
    request: dict[str, Any],
) -> tuple[dict[str, Any], Path, str, str, str, str]:
    resource = _api_resource(request)
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    plan = load_json(plan_path, "API suggestion plan")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA}")
    if plan.get("write_transport") != WRITE_TRANSPORT:
        raise ValueError(
            "plan does not authorize the Google Docs API SUGGEST transport"
        )
    if plan.get("api_resource") != resource:
        raise ValueError("plan api_resource does not match the request")
    edits = plan.get("edits")
    if not isinstance(edits, list):
        raise ValueError("API plan has no edits")
    normalized_edits = _validate_edit_spec({"schema": EDIT_SPEC_SCHEMA, "edits": edits})
    if normalized_edits != edits:
        raise ValueError("API plan edits are not canonical")
    requests = plan.get("requests")
    targets = plan.get("targets")
    if not isinstance(requests, list) or not requests:
        raise ValueError("API plan has no batch requests")
    if not isinstance(targets, list) or not targets:
        raise ValueError("API plan has no resolved targets")
    remote_write = request.get("remote_write")
    if not isinstance(remote_write, dict):
        raise ValueError("API apply requires remote_write governance")
    plan_sha256 = sha256_file(plan_path)
    if remote_write.get("plan_sha256") != plan_sha256:
        raise ValueError("remote_write plan_sha256 does not match plan")
    expected_revision = str(remote_write.get("expected_revision", ""))
    if expected_revision != str(plan.get("revision_id", "")):
        raise ValueError("remote_write expected_revision does not match plan revision")
    idempotency_key = str(remote_write.get("idempotency_key", ""))
    if not idempotency_key or len(idempotency_key) > 256:
        raise ValueError("remote_write idempotency_key must contain 1-256 characters")
    target = plan.get("target")
    document_id = target.get("document_id") if isinstance(target, dict) else None
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("API plan has no document target")
    return plan, plan_path, plan_sha256, expected_revision, idempotency_key, document_id


def _affected_suggestion_ids(
    response: dict[str, Any], expected_count: int
) -> tuple[set[str], set[str]]:
    values = response.get("suggestionResponses")
    if not isinstance(values, list) or len(values) != expected_count:
        raise RuntimeError(
            "Google Docs API suggestionResponses do not match the approved requests"
        )
    created: set[str] = set()
    affected: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise RuntimeError("Google Docs API returned an invalid suggestionResponse")
        per_request: set[str] = set()
        for key in (
            "createdSuggestionIds",
            "updatedSummarySuggestionIds",
            "deletedSuggestionIds",
            "acceptedSuggestionIds",
            "rejectedSuggestionIds",
        ):
            ids = value.get(key, [])
            if not isinstance(ids, list):
                raise RuntimeError(
                    "Google Docs API returned invalid suggestion identifiers"
                )
            valid = {item for item in ids if isinstance(item, str) and item}
            per_request.update(valid)
            affected.update(valid)
            if key == "createdSuggestionIds":
                created.update(valid)
        if not per_request:
            raise RuntimeError("a Google Docs API update did not affect a suggestion")
    if not created:
        raise RuntimeError("Google Docs API did not report a created suggestion")
    return created, affected


def _suggested_text(
    document: dict[str, Any], suggestion_ids: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    inserted = {value: "" for value in suggestion_ids}
    deleted = {value: "" for value in suggestion_ids}
    for run in _text_runs(document):
        for suggestion_id in run["suggested_insertion_ids"]:
            if suggestion_id in inserted:
                inserted[suggestion_id] += run["text"]
        for suggestion_id in run["suggested_deletion_ids"]:
            if suggestion_id in deleted:
                deleted[suggestion_id] += run["text"]
    return inserted, deleted


def _thread_statuses(document: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    suggestions = document.get("suggestions")
    if not isinstance(suggestions, list):
        return result
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        suggestion_id = suggestion.get("suggestionId")
        status = suggestion.get("status")
        if isinstance(suggestion_id, str) and isinstance(status, str):
            result[suggestion_id] = status
    return result


def _verify_document(
    plan: dict[str, Any],
    document: dict[str, Any],
    suggestion_ids: set[str],
) -> dict[str, Any]:
    if not suggestion_ids:
        raise RuntimeError(
            "no new suggestion identifiers are available for verification"
        )
    present = _suggestion_ids(document)
    missing = sorted(suggestion_ids - present)
    if missing:
        raise RuntimeError("API read-back did not expose every created suggestion")
    statuses = _thread_statuses(document)
    missing_threads = sorted(suggestion_ids - set(statuses))
    if missing_threads:
        raise RuntimeError("API read-back did not expose every suggestion thread")
    not_open = sorted(
        value for value in suggestion_ids if statuses.get(value) != "OPEN"
    )
    if not_open:
        raise RuntimeError("a created suggestion is no longer open during read-back")
    inserted, deleted = _suggested_text(document, suggestion_ids)
    inserted_values = list(inserted.values())
    deleted_values = list(deleted.values())
    for edit in plan.get("edits", []):
        if "append" in edit:
            if not any(edit["append"] in value for value in inserted_values):
                raise RuntimeError(
                    "API read-back did not observe the suggested append text"
                )
            continue
        if edit.get("replace") and not any(
            edit["replace"] in value for value in inserted_values
        ):
            raise RuntimeError(
                "API read-back did not observe replacement suggestion text"
            )
        if not any(edit.get("find", "") in value for value in deleted_values):
            raise RuntimeError(
                "API read-back did not observe source deletion suggestion text"
            )
    return {
        "status": "verified",
        "write_transport": WRITE_TRANSPORT,
        "write_mode": "SUGGEST",
        "required_revision_enforced": True,
        "comment_update_state": "ALL_SAVED",
        "created_suggestion_ids": sorted(suggestion_ids),
        "suggestion_count": len(suggestion_ids),
        "planned_text_observed_after_mutation": True,
        "suggestion_threads_open_after_mutation": True,
    }


def _after_revision(response: dict[str, Any], document: dict[str, Any]) -> str:
    write_control = response.get("writeControl")
    value = (
        write_control.get("requiredRevisionId")
        if isinstance(write_control, dict)
        else None
    )
    if not isinstance(value, str) or not value:
        raise RuntimeError("Google Docs API write returned no requiredRevisionId")
    if document.get("revisionId") != value:
        raise RuntimeError("Google Docs API write and read-back revisions do not match")
    return value


def _successful_apply_response(
    *,
    operation: str,
    plan_sha256: str,
    idempotency_key: str,
    before_revision: str,
    after_revision: str,
    verification: dict[str, Any],
) -> dict[str, Any]:
    run_id = sha256_bytes(idempotency_key.encode("utf-8"))[:24]
    return _response(
        operation,
        "ok",
        run_id,
        summary={
            "tracked_changes": True,
            "suggestion_count": verification["suggestion_count"],
            "verified": True,
            "oauth_used": True,
            "suggestions_api_ga": True,
        },
        remote_receipt={
            "status": "verified",
            "resources": [API_RESOURCE],
            "plan_sha256": plan_sha256,
            "idempotency_key": idempotency_key,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "verification": verification,
        },
    )


def apply_suggestions(request: dict[str, Any], client: DocsApiClient) -> dict[str, Any]:
    (
        plan,
        plan_path,
        plan_sha256,
        expected_revision,
        idempotency_key,
        document_id,
    ) = _validate_plan_request(request)
    journal_path = _journal_path(idempotency_key, plan_path)
    if journal_path.is_file():
        stored = load_json(journal_path, "Docs API idempotency journal")
        if (
            stored.get("plan_sha256") != plan_sha256
            or stored.get("resource") != API_RESOURCE
        ):
            raise RuntimeError(
                "idempotency key was already used for a different remote write"
            )
        response = stored.get("response")
        if isinstance(response, dict):
            return response
        raise RuntimeError(
            "a prior Docs API write crossed the mutation boundary without a verified "
            "receipt; run api-recover instead of sending it again"
        )

    before = client.get_document(document_id)
    live_revision = _validate_document(before, document_id)
    if live_revision != expected_revision:
        raise RuntimeError(
            "the Google document changed after planning; no edit was sent"
        )
    live_requests, live_targets = _compile_requests(before, list(plan.get("edits", [])))
    if live_requests != plan.get("requests") or live_targets != plan.get("targets"):
        raise RuntimeError(
            "the live API edit projection no longer matches the approved plan"
        )

    pending = {
        "status": "pending",
        "plan_sha256": plan_sha256,
        "resource": API_RESOURCE,
        "expected_revision": expected_revision,
        "idempotency_key_sha256": sha256_bytes(idempotency_key.encode("utf-8")),
        "preexisting_suggestion_ids": plan.get("preexisting_suggestion_ids", []),
    }
    write_private_json(journal_path, pending)
    batch = client.batch_update(
        document_id,
        {
            "requests": plan["requests"],
            "writeControl": {
                "writeMode": "SUGGEST",
                "requiredRevisionId": expected_revision,
            },
        },
    )
    pending["batch_response"] = batch
    write_private_json(journal_path, pending)
    if batch.get("documentId") != document_id:
        raise RuntimeError("Google Docs API write returned a different document")
    if batch.get("commentUpdateState") != "ALL_SAVED":
        raise RuntimeError(
            "Google Docs API did not report ALL_SAVED; the write is ambiguous and "
            "must be recovered without retrying"
        )
    created, affected = _affected_suggestion_ids(batch, len(plan["requests"]))
    pending.update(
        {
            "created_suggestion_ids": sorted(created),
            "affected_suggestion_ids": sorted(affected),
        }
    )
    write_private_json(journal_path, pending)
    after = client.get_document(document_id)
    _validate_document(after, document_id)
    after_revision = _after_revision(batch, after)
    if after_revision == expected_revision:
        raise RuntimeError("Google Docs API read-back did not observe a new revision")
    verification = _verify_document(plan, after, created)
    response = _successful_apply_response(
        operation="api-apply",
        plan_sha256=plan_sha256,
        idempotency_key=idempotency_key,
        before_revision=expected_revision,
        after_revision=after_revision,
        verification=verification,
    )
    write_private_json(
        journal_path,
        {
            "plan_sha256": plan_sha256,
            "resource": API_RESOURCE,
            "response": response,
        },
    )
    return response


def recover_suggestions(
    request: dict[str, Any], client: DocsApiClient
) -> dict[str, Any]:
    (
        plan,
        plan_path,
        plan_sha256,
        expected_revision,
        idempotency_key,
        document_id,
    ) = _validate_plan_request(request)
    journal_path = _journal_path(idempotency_key, plan_path)
    if not journal_path.is_file():
        raise RuntimeError("no pending Docs API write exists for this idempotency key")
    stored = load_json(journal_path, "Docs API idempotency journal")
    if (
        stored.get("plan_sha256") != plan_sha256
        or stored.get("resource") != API_RESOURCE
    ):
        raise RuntimeError(
            "idempotency key was already used for a different remote write"
        )
    stored_response = stored.get("response")
    if isinstance(stored_response, dict):
        recovered = dict(stored_response)
        recovered["operation"] = "api-recover"
        return recovered
    if stored.get("status") != "pending":
        raise RuntimeError("the Docs API write journal is not recoverable")
    created = {
        value
        for value in stored.get("created_suggestion_ids", [])
        if isinstance(value, str) and value
    }
    batch_response = stored.get("batch_response")
    if (
        not created
        and isinstance(batch_response, dict)
        and batch_response.get("commentUpdateState") == "ALL_SAVED"
    ):
        created, _affected = _affected_suggestion_ids(
            batch_response, len(plan["requests"])
        )
    if not created:
        raise RuntimeError(
            "the pending Docs API write has no returned suggestion IDs; attribution "
            "is ambiguous, so it cannot be retried or automatically receipted"
        )
    document = client.get_document(document_id)
    live_revision = _validate_document(document, document_id)
    if live_revision == expected_revision:
        raise RuntimeError("Docs API recovery did not observe a new document revision")
    verification = _verify_document(plan, document, created)
    response = _successful_apply_response(
        operation="api-recover",
        plan_sha256=plan_sha256,
        idempotency_key=idempotency_key,
        before_revision=expected_revision,
        after_revision=live_revision,
        verification=verification,
    )
    write_private_json(
        journal_path,
        {
            "plan_sha256": plan_sha256,
            "resource": API_RESOURCE,
            "response": {**response, "operation": "api-apply"},
        },
    )
    return response


def verify_receipt(request: dict[str, Any], client: DocsApiClient) -> dict[str, Any]:
    _api_resource(request)
    plan_path = Path(request["arguments"]["plan"]).resolve(strict=True)
    receipt_path = Path(request["arguments"]["receipt"]).resolve(strict=True)
    plan = load_json(plan_path, "API suggestion plan")
    if (
        plan.get("schema") != PLAN_SCHEMA
        or plan.get("write_transport") != WRITE_TRANSPORT
    ):
        raise ValueError("verification plan is not a Docs API suggestion plan")
    receipt_document = load_json(receipt_path, "Docs API remote receipt")
    remote_receipt = receipt_document.get("remote_receipt")
    if not isinstance(remote_receipt, dict):
        raise ValueError("receipt has no remote_receipt")
    if remote_receipt.get("resources") != [API_RESOURCE]:
        raise ValueError("receipt does not belong to the Google Docs API capability")
    if remote_receipt.get("plan_sha256") != sha256_file(plan_path):
        raise ValueError("verification plan does not match the remote receipt")
    if remote_receipt.get("status") != "verified":
        raise ValueError("remote receipt was not verified")
    target = plan.get("target")
    document_id = target.get("document_id") if isinstance(target, dict) else None
    if not isinstance(document_id, str):
        raise ValueError("API plan has no document target")
    previous = remote_receipt.get("verification")
    if not isinstance(previous, dict) or previous.get("status") != "verified":
        raise ValueError("receipt has no verified suggestion record")
    ids = previous.get("created_suggestion_ids") if isinstance(previous, dict) else None
    if not isinstance(ids, list):
        raise ValueError("receipt has no created suggestion identifiers")
    suggestion_ids = {value for value in ids if isinstance(value, str) and value}
    document = client.get_document(document_id)
    revision_id = _validate_document(document, document_id)
    verification = _verify_document(plan, document, suggestion_ids)
    report = {
        "schema": VERIFICATION_SCHEMA,
        "status": "verified",
        "api_resource": API_RESOURCE,
        "receipt_sha256": sha256_file(receipt_path),
        "revision_id": revision_id,
        "created_suggestion_ids": sorted(suggestion_ids),
        "verification": verification,
    }
    output_path = (
        Path(request["output_dir"]).resolve(strict=False) / "api-verification.json"
    )
    write_private_json(output_path, report)
    return _response(
        "api-verify",
        "ok",
        report["receipt_sha256"][:24],
        summary={
            "verified": True,
            "suggestion_count": len(suggestion_ids),
            "oauth_used": True,
            "suggestions_api_ga": True,
        },
        artifacts=[
            private_artifact(output_path, "google-docs-api-suggestion-verification")
        ],
    )


def execute_api(
    request: dict[str, Any], client: DocsApiClient | None = None
) -> dict[str, Any]:
    operation = request.get("operation")
    try:
        active_client = client or GoogleDocsRestClient.from_environment()
        if operation == "api-inspect":
            return inspect_document(request, active_client)
        if operation == "api-plan":
            return plan_suggestions(request, active_client)
        if operation == "api-apply":
            return apply_suggestions(request, active_client)
        if operation == "api-recover":
            return recover_suggestions(request, active_client)
        if operation == "api-verify":
            return verify_receipt(request, active_client)
        return _error(str(operation), "unsupported API operation")
    except Exception as exc:
        return _error(str(operation), str(exc))
