from __future__ import annotations

import json
import os
import re
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
PLAN_SCHEMA = "google-docs-api-change-plan/v1"
VERIFICATION_SCHEMA = "google-docs-api-suggestion-verification/v1"
WRITE_TRANSPORT = "google-docs-api-suggest-picker-oauth-v1"
EMAIL_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_COMMENT_UTF8_BYTES = 2048


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


def _string_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _person_mentions(document: dict[str, Any]) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    for tab_id, _title, content in _document_tabs(document):
        for elements in _walk_structural_content(content):
            for element in elements:
                person = element.get("person")
                start = element.get("startIndex")
                end = element.get("endIndex")
                properties = (
                    person.get("personProperties") if isinstance(person, dict) else None
                )
                if (
                    not isinstance(person, dict)
                    or not isinstance(properties, dict)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or end < start
                ):
                    continue
                email = properties.get("email")
                name = properties.get("name", "")
                person_id = person.get("personId", "")
                if not isinstance(email, str) or not email:
                    continue
                mentions.append(
                    {
                        "tab_id": tab_id,
                        "start_index": start,
                        "end_index": end,
                        "email": email,
                        "name": name if isinstance(name, str) else "",
                        "person_id": person_id if isinstance(person_id, str) else "",
                        "suggested_insertion_ids": _string_ids(
                            person.get("suggestedInsertionIds")
                        ),
                        "suggested_deletion_ids": _string_ids(
                            person.get("suggestedDeletionIds")
                        ),
                    }
                )
    return sorted(
        mentions, key=lambda value: (value["tab_id"], value["start_index"])
    )


def _comment_threads(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    comments = document.get("comments")
    if not isinstance(comments, list):
        return result
    for comment in comments:
        comment_id = comment.get("commentId") if isinstance(comment, dict) else None
        if isinstance(comment_id, str) and comment_id:
            result[comment_id] = comment
    return result


def _comment_anchor_ranges(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for tab in _flatten_tabs(document.get("tabs")):
        properties = tab.get("tabProperties")
        document_tab = tab.get("documentTab")
        if not isinstance(properties, dict) or not isinstance(document_tab, dict):
            continue
        current_tab_id = properties.get("tabId")
        anchors = document_tab.get("commentAnchors")
        if not isinstance(current_tab_id, str) or not isinstance(anchors, dict):
            continue
        for key, anchor in anchors.items():
            if not isinstance(anchor, dict):
                continue
            anchor_id = anchor.get("anchorId", key)
            ranges = anchor.get("ranges")
            if not isinstance(anchor_id, str) or not isinstance(ranges, list):
                continue
            normalized: list[dict[str, Any]] = []
            for value in ranges:
                if not isinstance(value, dict):
                    continue
                item = {
                    field: item_value
                    for field, item_value in value.items()
                    if item_value not in (None, "")
                }
                if current_tab_id and "tabId" not in item:
                    item["tabId"] = current_tab_id
                normalized.append(item)
            result[anchor_id] = normalized
    return result


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


def _valid_email(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not EMAIL_ADDRESS.fullmatch(value)
        or len(value.encode("utf-8")) > MAX_COMMENT_UTF8_BYTES
    ):
        raise ValueError(f"{field} must be a valid email address")
    return value


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    if len(value.encode("utf-8")) > MAX_COMMENT_UTF8_BYTES:
        raise ValueError(f"{field} exceeds {MAX_COMMENT_UTF8_BYTES} UTF-8 bytes")
    return value


def _validate_edit_spec(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("schema") != EDIT_SPEC_SCHEMA:
        raise ValueError(f"edit spec schema must be {EDIT_SPEC_SCHEMA}")
    edits = value.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BROWSER_EDITS:
        raise ValueError(f"edit spec must contain 1-{MAX_BROWSER_EDITS} edits")
    normalized: list[dict[str, Any]] = []
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
        if set(edit) == {"comment"}:
            comment = edit.get("comment")
            if not isinstance(comment, dict):
                raise ValueError("comment edits require an object")
            if not {"quote", "content"} <= set(comment) or not set(comment) <= {
                "quote",
                "content",
                "assignee_email",
            }:
                raise ValueError(
                    "comment edits require quote and content, with optional assignee_email"
                )
            quote = _bounded_text(comment.get("quote"), "comment quote")
            if "\x00" in quote:
                raise ValueError("comment quote cannot contain a null character")
            content = _bounded_text(comment.get("content"), "comment content")
            normalized_comment: dict[str, str] = {
                "quote": quote,
                "content": content,
            }
            if "assignee_email" in comment:
                normalized_comment["assignee_email"] = _valid_email(
                    comment.get("assignee_email"), "comment assignee_email"
                )
            normalized.append({"comment": normalized_comment})
            continue
        if set(edit) == {"person_mention"}:
            mention = edit.get("person_mention")
            if not isinstance(mention, dict):
                raise ValueError("person_mention edits require an object")
            allowed = {"email", "name", "before", "after"}
            if not set(mention) <= allowed or "email" not in mention:
                raise ValueError(
                    "person_mention requires email, optional name, and one anchor"
                )
            anchors = [key for key in ("before", "after") if key in mention]
            if len(anchors) != 1:
                raise ValueError(
                    "person_mention requires exactly one of before or after"
                )
            anchor_key = anchors[0]
            anchor = _bounded_text(
                mention.get(anchor_key), f"person_mention {anchor_key}"
            )
            if "\x00" in anchor:
                raise ValueError("person_mention anchor cannot contain a null character")
            normalized_mention: dict[str, str] = {
                "email": _valid_email(
                    mention.get("email"), "person_mention email"
                ),
                anchor_key: anchor,
            }
            if "name" in mention:
                normalized_mention["name"] = _bounded_text(
                    mention.get("name"), "person_mention name"
                )
            normalized.append({"person_mention": normalized_mention})
            continue
        if set(edit) != {"find", "replace"}:
            raise ValueError(
                "API edit entries accept replacement, append, comment, or person_mention"
            )
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
                "duplicate find text is not allowed in one change plan"
            )
        if any(find in prior or prior in find for prior in seen):
            raise ValueError(
                "overlapping find text is not allowed in one change plan"
            )
        seen.add(find)
        normalized.append({"find": find, "replace": replace})
    anchors: list[str] = []
    for edit in normalized:
        if "find" in edit:
            anchors.append(edit["find"])
        elif "comment" in edit:
            anchors.append(edit["comment"]["quote"])
        elif "person_mention" in edit:
            mention = edit["person_mention"]
            anchors.append(mention.get("before", mention.get("after")))
    if len(set(anchors)) != len(anchors):
        raise ValueError("duplicate source anchors are not allowed in one API plan")
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
    document: dict[str, Any], edits: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
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
        return (
            compiled,
            [{"kind": "append", "tab_id": tab_id, "action_index": 0}],
            [{"kind": "suggestion", "action_index": 0}],
        )

    resolved: list[dict[str, Any]] = []
    for action_index, edit in enumerate(edits):
        if "find" in edit:
            source = edit["find"]
            action_kind = "replace"
        elif "comment" in edit:
            source = edit["comment"]["quote"]
            action_kind = "comment"
        elif "person_mention" in edit:
            mention = edit["person_mention"]
            source = mention.get("before", mention.get("after"))
            action_kind = "person_mention"
        else:
            raise ValueError("unsupported canonical API edit action")
        target = _find_unique_range(document, source)
        resolved.append(
            {
                "action_index": action_index,
                "kind": action_kind,
                "edit": edit,
                "target": target,
            }
        )
    occupied: set[tuple[str, int]] = set()
    for item in resolved:
        target = item["target"]
        for index in range(target["start_index"], target["end_index"]):
            marker = (target["tab_id"], index)
            if marker in occupied:
                raise ValueError("resolved API action anchors overlap")
            occupied.add(marker)
    resolved.sort(
        key=lambda value: (
            value["target"]["tab_id"],
            value["target"]["start_index"],
        ),
        reverse=True,
    )
    requests: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    request_effects: list[dict[str, Any]] = []
    for item in resolved:
        edit = item["edit"]
        target = item["target"]
        action_index = item["action_index"]
        action_kind = item["kind"]
        tab_id = target["tab_id"]
        start = target["start_index"]
        end = target["end_index"]
        if action_kind == "replace":
            requests.append(
                {"deleteContentRange": {"range": _range(start, end, tab_id)}}
            )
            request_effects.append(
                {"kind": "suggestion", "action_index": action_index}
            )
            if edit["replace"]:
                requests.append(
                    {
                        "insertText": {
                            "text": edit["replace"],
                            "location": _location(start, tab_id),
                        }
                    }
                )
                request_effects.append(
                    {"kind": "suggestion", "action_index": action_index}
                )
            targets.append(
                {
                    "kind": "replace",
                    "action_index": action_index,
                    "tab_id": tab_id,
                    "start_index": start,
                    "end_index": end,
                    "find": edit["find"],
                    "replace": edit["replace"],
                }
            )
            continue
        if action_kind == "comment":
            comment = edit["comment"]
            body: dict[str, Any] = {
                "content": comment["content"],
                "range": _range(start, end, tab_id),
            }
            if "assignee_email" in comment:
                body["assigneeEmailAddress"] = comment["assignee_email"]
            requests.append({"insertComment": body})
            request_effects.append(
                {"kind": "comment", "action_index": action_index}
            )
            target_value = {
                "kind": "comment",
                "action_index": action_index,
                "tab_id": tab_id,
                "start_index": start,
                "end_index": end,
                "quote": comment["quote"],
                "content": comment["content"],
            }
            if "assignee_email" in comment:
                target_value["assignee_email"] = comment["assignee_email"]
            targets.append(target_value)
            continue
        if action_kind == "person_mention":
            mention = edit["person_mention"]
            index = start if "before" in mention else end
            properties = {"email": mention["email"]}
            if "name" in mention:
                properties["name"] = mention["name"]
            requests.append(
                {
                    "insertPerson": {
                        "personProperties": properties,
                        "location": _location(index, tab_id),
                    }
                }
            )
            request_effects.append(
                {"kind": "suggestion", "action_index": action_index}
            )
            target_value = {
                "kind": "person_mention",
                "action_index": action_index,
                "tab_id": tab_id,
                "index": index,
                "email": mention["email"],
                "anchor": mention.get("before", mention.get("after")),
                "anchor_side": "before" if "before" in mention else "after",
            }
            if "name" in mention:
                target_value["name"] = mention["name"]
            targets.append(target_value)
            continue
        raise ValueError("unsupported canonical API edit action")
    return requests, targets, request_effects


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
    for mention in _person_mentions(document):
        result.update(mention["suggested_insertion_ids"])
        result.update(mention["suggested_deletion_ids"])
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
        "open_comment_ids": sorted(
            comment_id
            for comment_id, comment in _comment_threads(document).items()
            if comment.get("status") == "OPEN"
        ),
        "person_mention_count": len(_person_mentions(document)),
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
            "open_comment_count": len(inspection["open_comment_ids"]),
            "person_mention_count": inspection["person_mention_count"],
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
    requests, targets, request_effects = _compile_requests(document, edits)
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
        "request_effects": request_effects,
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
            "comment_count": sum("comment" in edit for edit in edits),
            "person_mention_count": sum("person_mention" in edit for edit in edits),
            "plan_sha256": plan_sha256,
            "expected_revision_sha256": sha256_bytes(revision_id.encode("utf-8")),
            "tracked_changes": any("comment" not in edit for edit in edits),
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
        else plan_path.parent / ".google-docs-state"
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
    plan = load_json(plan_path, "API change plan")
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
    request_effects = plan.get("request_effects")
    if not isinstance(requests, list) or not requests:
        raise ValueError("API plan has no batch requests")
    if not isinstance(targets, list) or not targets:
        raise ValueError("API plan has no resolved targets")
    if (
        not isinstance(request_effects, list)
        or len(request_effects) != len(requests)
        or any(
            not isinstance(effect, dict)
            or effect.get("kind") not in {"suggestion", "comment"}
            or not isinstance(effect.get("action_index"), int)
            for effect in request_effects
        )
    ):
        raise ValueError("API plan has invalid request effects")
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


def _batch_effects(
    response: dict[str, Any], request_effects: list[dict[str, Any]]
) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    values = response.get("suggestionResponses")
    if not isinstance(values, list) or len(values) != len(request_effects):
        raise RuntimeError(
            "Google Docs API suggestionResponses do not match the approved requests"
        )
    created: set[str] = set()
    affected: set[str] = set()
    comment_requests = any(
        effect.get("kind") == "comment" for effect in request_effects
    )
    replies = response.get("replies")
    if comment_requests and (
        not isinstance(replies, list) or len(replies) != len(request_effects)
    ):
        raise RuntimeError(
            "Google Docs API replies do not match the approved comment requests"
        )
    created_comments: list[dict[str, Any]] = []
    for index, (value, effect) in enumerate(zip(values, request_effects)):
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
        if effect.get("kind") == "suggestion" and not per_request:
            raise RuntimeError("a Google Docs API update did not affect a suggestion")
        if effect.get("kind") != "comment":
            continue
        reply = replies[index] if isinstance(replies, list) else None
        insert = reply.get("insertComment") if isinstance(reply, dict) else None
        thread = insert.get("commentThread") if isinstance(insert, dict) else None
        comment_id = thread.get("commentId") if isinstance(thread, dict) else None
        if not isinstance(comment_id, str) or not comment_id:
            raise RuntimeError("Google Docs API returned no created comment identifier")
        created_comments.append(
            {
                "action_index": effect["action_index"],
                "comment_id": comment_id,
            }
        )
    if any(effect.get("kind") == "suggestion" for effect in request_effects) and not created:
        raise RuntimeError("Google Docs API did not report a created suggestion")
    return created, affected, created_comments


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


def _verify_person_mention(
    target: dict[str, Any],
    document: dict[str, Any],
    suggestion_ids: set[str],
) -> None:
    for mention in _person_mentions(document):
        inserted = set(mention["suggested_insertion_ids"])
        if not inserted.intersection(suggestion_ids):
            continue
        if (
            mention["tab_id"] == target["tab_id"]
            and mention["start_index"] == target["index"]
            and mention["email"].casefold() == target["email"].casefold()
            and (
                "name" not in target
                or mention["name"] == target["name"]
            )
        ):
            return
    raise RuntimeError("API read-back did not observe the suggested person mention")


def _verify_comment(
    target: dict[str, Any],
    document: dict[str, Any],
    comment_id: str,
) -> None:
    comment = _comment_threads(document).get(comment_id)
    if not isinstance(comment, dict):
        raise RuntimeError("API read-back did not expose a created comment")
    if comment.get("status") != "OPEN":
        raise RuntimeError("a created comment is no longer open during read-back")
    post = comment.get("headPost")
    if not isinstance(post, dict) or post.get("content") != target["content"]:
        raise RuntimeError("API read-back did not preserve created comment content")
    assignee = target.get("assignee_email")
    observed_assignee = post.get("assigneeEmail")
    if assignee is not None and (
        not isinstance(observed_assignee, str)
        or observed_assignee.casefold() != assignee.casefold()
    ):
        raise RuntimeError("API read-back did not preserve the comment assignee")
    if assignee is None and observed_assignee not in (None, ""):
        raise RuntimeError("API read-back unexpectedly assigned the comment")
    if comment.get("plainTextQuote") != target["quote"]:
        raise RuntimeError("API read-back did not preserve the comment quote")
    anchor_id = comment.get("anchorId")
    if not isinstance(anchor_id, str) or not anchor_id:
        raise RuntimeError("API read-back returned an unanchored comment")
    expected = _range(
        target["start_index"], target["end_index"], target["tab_id"]
    )
    anchors = _comment_anchor_ranges(document)
    if expected not in anchors.get(anchor_id, []):
        raise RuntimeError("API read-back did not preserve the comment anchor range")


def _verify_document(
    plan: dict[str, Any],
    document: dict[str, Any],
    suggestion_ids: set[str],
    created_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    suggestion_actions = [
        edit for edit in plan.get("edits", []) if "comment" not in edit
    ]
    if suggestion_actions and not suggestion_ids:
        raise RuntimeError(
            "no new suggestion identifiers are available for verification"
        )
    if suggestion_ids:
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
    targets_by_action = {
        target["action_index"]: target for target in plan.get("targets", [])
    }
    comments_by_action = {
        value["action_index"]: value["comment_id"] for value in created_comments
    }
    for action_index, edit in enumerate(plan.get("edits", [])):
        target = targets_by_action.get(action_index)
        if not isinstance(target, dict):
            raise RuntimeError("API verification has no resolved action target")
        if "append" in edit:
            if not any(edit["append"] in value for value in inserted_values):
                raise RuntimeError(
                    "API read-back did not observe the suggested append text"
                )
            continue
        if "person_mention" in edit:
            _verify_person_mention(target, document, suggestion_ids)
            continue
        if "comment" in edit:
            comment_id = comments_by_action.get(action_index)
            if not isinstance(comment_id, str):
                raise RuntimeError("no created comment identifier is available")
            _verify_comment(target, document, comment_id)
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
        "created_comments": sorted(
            created_comments, key=lambda value: value["action_index"]
        ),
        "suggestion_count": len(suggestion_ids),
        "comment_count": len(created_comments),
        "person_mention_count": sum(
            "person_mention" in edit for edit in plan.get("edits", [])
        ),
        "planned_text_observed_after_mutation": True,
        "suggestion_threads_open_after_mutation": bool(suggestion_ids),
        "comment_threads_open_after_mutation": bool(created_comments),
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
            "tracked_changes": verification["suggestion_count"] > 0,
            "write_mode": "SUGGEST",
            "suggestion_count": verification["suggestion_count"],
            "comment_count": verification["comment_count"],
            "person_mention_count": verification["person_mention_count"],
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
    live_requests, live_targets, live_effects = _compile_requests(
        before, list(plan.get("edits", []))
    )
    if (
        live_requests != plan.get("requests")
        or live_targets != plan.get("targets")
        or live_effects != plan.get("request_effects")
    ):
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
    created, affected, created_comments = _batch_effects(
        batch, plan["request_effects"]
    )
    pending.update(
        {
            "created_suggestion_ids": sorted(created),
            "affected_suggestion_ids": sorted(affected),
            "created_comments": created_comments,
        }
    )
    write_private_json(journal_path, pending)
    after = client.get_document(document_id)
    _validate_document(after, document_id)
    after_revision = _after_revision(batch, after)
    if after_revision == expected_revision:
        raise RuntimeError("Google Docs API read-back did not observe a new revision")
    verification = _verify_document(plan, after, created, created_comments)
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
    created_comments = [
        value
        for value in stored.get("created_comments", [])
        if isinstance(value, dict)
        and isinstance(value.get("action_index"), int)
        and isinstance(value.get("comment_id"), str)
        and value["comment_id"]
    ]
    batch_response = stored.get("batch_response")
    if (
        (not created or not created_comments)
        and isinstance(batch_response, dict)
        and batch_response.get("commentUpdateState") == "ALL_SAVED"
    ):
        created, _affected, created_comments = _batch_effects(
            batch_response, plan["request_effects"]
        )
    expects_suggestions = any(
        effect.get("kind") == "suggestion" for effect in plan["request_effects"]
    )
    expects_comments = any(
        effect.get("kind") == "comment" for effect in plan["request_effects"]
    )
    if (expects_suggestions and not created) or (
        expects_comments and not created_comments
    ):
        raise RuntimeError(
            "the pending Docs API write has no complete returned effect IDs; attribution "
            "is ambiguous, so it cannot be retried or automatically receipted"
        )
    document = client.get_document(document_id)
    live_revision = _validate_document(document, document_id)
    if live_revision == expected_revision:
        raise RuntimeError("Docs API recovery did not observe a new document revision")
    verification = _verify_document(plan, document, created, created_comments)
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
    plan = load_json(plan_path, "API change plan")
    if (
        plan.get("schema") != PLAN_SCHEMA
        or plan.get("write_transport") != WRITE_TRANSPORT
    ):
        raise ValueError("verification plan is not a Docs API change plan")
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
    created_comments = previous.get("created_comments")
    if not isinstance(created_comments, list):
        raise ValueError("receipt has no created comment identifiers")
    normalized_comments = [
        value
        for value in created_comments
        if isinstance(value, dict)
        and isinstance(value.get("action_index"), int)
        and isinstance(value.get("comment_id"), str)
        and value["comment_id"]
    ]
    if len(normalized_comments) != len(created_comments):
        raise ValueError("receipt has invalid created comment identifiers")
    document = client.get_document(document_id)
    revision_id = _validate_document(document, document_id)
    verification = _verify_document(
        plan, document, suggestion_ids, normalized_comments
    )
    report = {
        "schema": VERIFICATION_SCHEMA,
        "status": "verified",
        "api_resource": API_RESOURCE,
        "receipt_sha256": sha256_file(receipt_path),
        "revision_id": revision_id,
        "created_suggestion_ids": sorted(suggestion_ids),
        "created_comments": normalized_comments,
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
            "comment_count": len(normalized_comments),
            "person_mention_count": verification["person_mention_count"],
            "oauth_used": True,
            "suggestions_api_ga": True,
        },
        artifacts=[
            private_artifact(output_path, "google-docs-api-suggestion-verification")
        ],
    )


def self_test() -> dict[str, Any]:
    return _response(
        "self-test",
        "ok",
        "synthetic-api-transport-self-test",
        summary={
            "suggest_mode_required": True,
            "tracked_changes_supported": True,
            "preferred_write_transport": WRITE_TRANSPORT,
            "browser_transport_available": False,
            "oauth_used": False,
            "docs_api_native_suggestions_ga": True,
            "docs_api_comments_ga": True,
            "docs_api_assigned_comments": True,
            "docs_api_person_mentions": True,
            "docs_api_write_mode": "SUGGEST",
            "desktop_oauth_pkce": True,
            "stored_oauth_refresh": True,
            "google_picker_per_file_grant": True,
        },
    )


def execute_api(
    request: dict[str, Any], client: DocsApiClient | None = None
) -> dict[str, Any]:
    operation = request.get("operation")
    try:
        if operation == "self-test":
            return self_test()
        if operation not in {
            "api-inspect",
            "api-plan",
            "api-apply",
            "api-recover",
            "api-verify",
        }:
            return _error(str(operation), "unsupported API operation")
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
