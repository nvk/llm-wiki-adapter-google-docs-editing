from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

RESOURCE_PREFIX = "google-docs:"
DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


def document_id_from_resource(resource: str) -> str:
    if not resource.startswith(RESOURCE_PREFIX):
        raise ValueError("document_resource must use the google-docs: prefix")
    document_id = resource[len(RESOURCE_PREFIX) :]
    if not DOCUMENT_ID_RE.fullmatch(document_id):
        raise ValueError("document_resource contains an invalid Google Docs identifier")
    return document_id


def iter_tabs(document: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    tabs = document.get("tabs")
    if not isinstance(tabs, list):
        body = document.get("body")
        if isinstance(body, dict):
            yield "", "Document", {"body": body}
        return

    def walk(values: list[Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
        for value in values:
            if not isinstance(value, dict):
                continue
            properties = value.get("tabProperties") if isinstance(value.get("tabProperties"), dict) else {}
            tab_id = str(properties.get("tabId", ""))
            title = str(properties.get("title", "Untitled tab"))
            document_tab = value.get("documentTab")
            if isinstance(document_tab, dict):
                yield tab_id, title, document_tab
            children = value.get("childTabs")
            if isinstance(children, list):
                yield from walk(children)

    yield from walk(tabs)


def _suggestion_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"suggestedInsertionIds", "suggestedDeletionIds"} and isinstance(child, list):
                found.update(str(item) for item in child if isinstance(item, str))
            elif key.startswith("suggested") and key.endswith("Changes") and isinstance(child, dict):
                found.update(str(item) for item in child if isinstance(item, str))
            found.update(_suggestion_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_suggestion_ids(child))
    return found


def collect_suggestion_ids(document: dict[str, Any]) -> set[str]:
    return _suggestion_ids(document)


@dataclass(frozen=True)
class TextSpan:
    start_index: int
    end_index: int
    content: str
    suggestion_ids: frozenset[str]


@dataclass
class TabTextIndex:
    tab_id: str
    title: str
    text: str
    starts: list[int]
    ends: list[int]
    suggestions: list[frozenset[str]]

    def locate(self, needle: str, occurrence: int | None = None) -> tuple[int, int, int, int]:
        if not needle:
            raise ValueError("edit find text must not be empty")
        positions: list[int] = []
        offset = 0
        while True:
            found = self.text.find(needle, offset)
            if found < 0:
                break
            positions.append(found)
            offset = found + 1
        if occurrence is None:
            if len(positions) != 1:
                raise ValueError(
                    f"find text must occur exactly once in the selected tab; found {len(positions)}"
                )
            selected = positions[0]
            occurrence_value = 1
        else:
            if occurrence < 1 or occurrence > len(positions):
                raise ValueError(
                    f"occurrence {occurrence} is unavailable in the selected tab; found {len(positions)}"
                )
            selected = positions[occurrence - 1]
            occurrence_value = occurrence
        final = selected + len(needle)
        if final > len(self.starts):
            raise ValueError("text match has no document index mapping")
        for index in range(selected, final - 1):
            if self.ends[index] != self.starts[index + 1]:
                raise ValueError("text match crosses a non-contiguous document structure")
        if any(self.suggestions[index] for index in range(selected, final)):
            raise ValueError("edit target overlaps an unresolved existing suggestion")
        return selected, final, self.starts[selected], self.ends[final - 1]


def _text_spans(body: dict[str, Any]) -> list[TextSpan]:
    spans: list[TextSpan] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            text_run = value.get("textRun")
            if isinstance(text_run, dict) and isinstance(text_run.get("content"), str):
                start = value.get("startIndex")
                end = value.get("endIndex")
                if isinstance(start, int) and isinstance(end, int) and end >= start:
                    spans.append(
                        TextSpan(
                            start,
                            end,
                            text_run["content"],
                            frozenset(_suggestion_ids(value)),
                        )
                    )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(body)
    unique = {(span.start_index, span.end_index, span.content): span for span in spans}
    return sorted(unique.values(), key=lambda span: (span.start_index, span.end_index))


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def tab_text_indexes(document: dict[str, Any]) -> dict[str, TabTextIndex]:
    result: dict[str, TabTextIndex] = {}
    for tab_id, title, document_tab in iter_tabs(document):
        body = document_tab.get("body")
        if not isinstance(body, dict):
            continue
        text_parts: list[str] = []
        starts: list[int] = []
        ends: list[int] = []
        suggestions: list[frozenset[str]] = []
        for span in _text_spans(body):
            cursor = span.start_index
            for character in span.content:
                units = _utf16_units(character)
                text_parts.append(character)
                starts.append(cursor)
                ends.append(cursor + units)
                suggestions.append(span.suggestion_ids)
                cursor += units
        result[tab_id] = TabTextIndex(tab_id, title, "".join(text_parts), starts, ends, suggestions)
    return result


def tab_texts(document: dict[str, Any]) -> dict[str, str]:
    return {tab_id: index.text for tab_id, index in tab_text_indexes(document).items()}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def projection_hashes(document: dict[str, Any]) -> dict[str, str]:
    return {tab_id: text_hash(text) for tab_id, text in sorted(tab_texts(document).items())}


def apply_text_edits(
    texts: dict[str, str], edits: list[dict[str, Any]]
) -> tuple[dict[str, str], list[tuple[str, int, int]]]:
    updated = dict(texts)
    ranges: list[tuple[str, int, int]] = []
    resolved: list[tuple[str, int, int, str]] = []
    for edit in edits:
        tab_id = str(edit.get("tab_id", ""))
        if tab_id not in updated:
            raise ValueError("edit refers to an unknown tab_id")
        needle = edit.get("find")
        replacement = edit.get("replace")
        occurrence = edit.get("occurrence")
        if not isinstance(needle, str) or not needle:
            raise ValueError("every edit requires non-empty find text")
        if not isinstance(replacement, str):
            raise ValueError("every edit requires string replace text")
        if replacement == needle:
            raise ValueError("edit replacement must differ from find text")
        if occurrence is not None and (not isinstance(occurrence, int) or occurrence < 1):
            raise ValueError("edit occurrence must be a positive integer")
        positions: list[int] = []
        offset = 0
        while True:
            found = updated[tab_id].find(needle, offset)
            if found < 0:
                break
            positions.append(found)
            offset = found + 1
        if occurrence is None:
            if len(positions) != 1:
                raise ValueError(
                    f"find text must occur exactly once in accepted projection; found {len(positions)}"
                )
            start = positions[0]
        else:
            if occurrence > len(positions):
                raise ValueError(
                    f"occurrence {occurrence} is unavailable in accepted projection; found {len(positions)}"
                )
            start = positions[occurrence - 1]
        resolved.append((tab_id, start, start + len(needle), replacement))
    for tab_id, start, end, replacement in sorted(resolved, key=lambda row: (row[0], row[1]), reverse=True):
        for existing_tab, existing_start, existing_end in ranges:
            if tab_id == existing_tab and start < existing_end and end > existing_start:
                raise ValueError("edits overlap in accepted projection")
        ranges.append((tab_id, start, end))
        text = updated[tab_id]
        updated[tab_id] = text[:start] + replacement + text[end:]
    return updated, ranges
