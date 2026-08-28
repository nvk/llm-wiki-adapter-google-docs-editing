from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterator
from urllib.parse import urlsplit

from .document import DOCUMENT_ID_RE

BROWSER_PROTOCOL = "llm-wiki-browser-executor/v1"
DRIVER_ID = "google-docs-suggestions"
DRIVER_VERSION = "collaboration-3"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_BROWSER_EDITS = 9
# Compatibility alias while shadow-compiler tests migrate to the production name.
MAX_SHADOW_EDITS = MAX_BROWSER_EDITS
MAX_PRIVATE_VALUE_BYTES = 16_384
SNAPSHOT_FIELDS = ["role", "name", "value", "description"]
SNAPSHOT_LOCATOR = {"name_matches": ".+"}
SNAPSHOT_MAX_ITEMS = 5000
INSPECTION_MAX_SCROLLS = 20
PAGE_ANNOUNCEMENT = re.compile(r"^On page [0-9]+(?: of [0-9]+)?[.]?$")
FIND_RESULT_COUNT = re.compile(r"^[0-9]+ of [0-9]+$")
DOCS_ROOT_TITLE = re.compile(r"^(?P<title>.+?)[ ]+-[ ]+Google Docs$")
DOCS_ROOT_BADGE_PREFIX = re.compile(r"^[0-9][ ](?=\S)")
DOCS_TITLE_PREFIX = re.compile(r"^(?:#+[ ]*|[0-9]+[ ]{2,})")
DOCS_LIVE_REGION_STATUS = re.compile(
    r"^(?:"
    r"Banner hidden|"
    r"Screen reader support enabled[.]?|"
    r"Entered (?:editing|suggesting|viewing) mode[.]?|"
    r"Entering page [0-9]+(?: of [0-9]+)?[.]?|"
    r"On page [0-9]+(?: of [0-9]+)?[.]?|"
    r"[0-9]+ visible tabs? named .+|"
    r"Suggested insert(?: start| end| exited)?|"
    r"new line|blank"
    r")$",
    re.IGNORECASE,
)

EDITOR_CHROME_ROLES = {
    "banner", "button", "checkbox", "combobox", "complementary", "group",
    "inlinetextbox", "link", "listbox", "listitem", "menuitem",
    "menuitemradio", "option", "tab", "textbox", "toolbar", "tooltip",
}


def canonical_program_sha256(program: dict[str, Any]) -> str:
    value = copy.deepcopy(program)
    value.pop("program_sha256", None)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_sha256(snapshot: list[dict[str, Any]]) -> str:
    """Hash the exact ordered AX projection used by the extension boundary."""
    if not isinstance(snapshot, list) or not snapshot:
        raise ValueError("browser inspection returned no accessibility snapshot")
    for row in snapshot:
        if not isinstance(row, dict) or set(row) != set(SNAPSHOT_FIELDS):
            raise ValueError("browser inspection returned an invalid accessibility snapshot")
        if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in row.values()):
            raise ValueError("browser inspection returned an invalid accessibility value")
    encoded = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_text_fragments(snapshot: list[dict[str, Any]]) -> list[str]:
    """Return bounded browser-visible text fragments without pretending they are a Docs API model."""
    preferred_roles = {
        "heading", "inlinetextbox", "paragraph", "statictext", "text", "textbox",
    }
    preferred: list[str] = []
    fallback: list[str] = []
    for row in snapshot:
        role = str(row.get("role") or "").lower()
        for key in ("name", "value"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            target = preferred if role in preferred_roles else fallback
            if not target or target[-1] != value:
                target.append(value)
    combined: list[str] = []
    for value in preferred + fallback:
        if value not in combined:
            combined.append(value)
    return combined


def _normalized_ax_text(value: str) -> str:
    return value.replace("\u00a0", " ").strip()


def _document_title_identities(snapshot: list[dict[str, Any]]) -> set[str]:
    identities: set[str] = set()
    for row in snapshot:
        if str(row.get("role") or "").lower() != "rootwebarea":
            continue
        name = row.get("name")
        if not isinstance(name, str):
            continue
        match = DOCS_ROOT_TITLE.fullmatch(_normalized_ax_text(name))
        if match:
            title = match.group("title")
            identities.add(title.casefold())
            # Docs can prefix the root accessible name with a one-digit badge
            # count while rendering the actual document title without it.
            identities.add(DOCS_ROOT_BADGE_PREFIX.sub("", title).casefold())
    return identities


def _editor_chrome_identities(snapshot: list[dict[str, Any]]) -> set[str]:
    identities: set[str] = set()
    for row in snapshot:
        if str(row.get("role") or "").lower() not in EDITOR_CHROME_ROLES:
            continue
        for key in ("name", "value"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                identities.add(_normalized_ax_text(value).casefold())
    return identities


def _is_document_title_echo(value: str, titles: set[str]) -> bool:
    candidate = value.strip()
    while True:
        reduced = DOCS_TITLE_PREFIX.sub("", candidate, count=1).strip()
        if reduced == candidate:
            break
        candidate = reduced
    return candidate.casefold() in titles


def _is_volatile_or_chrome_text(
    value: str,
    *,
    titles: set[str],
    chrome: set[str],
) -> bool:
    return (
        DOCS_LIVE_REGION_STATUS.fullmatch(value) is not None
        or value.casefold() in chrome
        or _is_document_title_echo(value, titles)
    )


def document_projection(snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the stable Google Docs content slice, excluding volatile editor chrome."""
    snapshot_sha256(snapshot)
    projection: list[dict[str, Any]] = []
    titles = _document_title_identities(snapshot)
    chrome = _editor_chrome_identities(snapshot)
    for index, row in enumerate(snapshot):
        role = str(row.get("role") or "").lower()
        name = row.get("name")
        if role != "statictext" or not isinstance(name, str) or not PAGE_ANNOUNCEMENT.fullmatch(name):
            continue
        for candidate in snapshot[index + 1:]:
            candidate_role = str(candidate.get("role") or "").lower()
            candidate_name = candidate.get("name")
            if (
                candidate_role == "statictext"
                and isinstance(candidate_name, str)
                and PAGE_ANNOUNCEMENT.fullmatch(_normalized_ax_text(candidate_name))
            ):
                break
            if (
                candidate_role == "statictext"
                and isinstance(candidate_name, str)
                and candidate_name.strip()
            ):
                normalized_name = _normalized_ax_text(candidate_name)
                if (
                    FIND_RESULT_COUNT.fullmatch(normalized_name)
                    or _is_volatile_or_chrome_text(
                        normalized_name,
                        titles=titles,
                        chrome=chrome,
                    )
                ):
                    continue
                projection.append(dict(candidate))
                break
    if projection:
        return projection

    # Docs' screen-reader live region does not always repeat the page marker.
    # Its virtualized AX rows can move across the suggested-insert boundary
    # between otherwise identical reads, so use a canonical content set rather
    # than treating transient accessibility order as document order.
    banner_index = next((
        index
        for index, row in enumerate(snapshot)
        if str(row.get("role") or "").lower() == "statictext"
        and isinstance(row.get("name"), str)
        and row["name"].replace("\u00a0", " ").strip().casefold() == "banner hidden"
    ), None)
    if banner_index is not None:
        live_segment = snapshot[banner_index + 1:]
        live_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        skip_next_find_text = False
        for row in live_segment:
            role = str(row.get("role") or "").lower()
            name = row.get("name")
            if role != "statictext" or not isinstance(name, str) or not name.strip():
                continue
            normalized_name = _normalized_ax_text(name)
            if FIND_RESULT_COUNT.fullmatch(normalized_name):
                skip_next_find_text = True
                continue
            if _is_volatile_or_chrome_text(
                normalized_name,
                titles=titles,
                chrome=chrome,
            ):
                continue
            if skip_next_find_text:
                skip_next_find_text = False
                continue
            candidate = dict(row)
            identity = json.dumps(
                candidate,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if identity in seen:
                continue
            seen.add(identity)
            live_rows.append(candidate)
        if live_rows:
            return sorted(
                live_rows,
                key=lambda row: json.dumps(
                    row,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            )

    # Synthetic fixtures and future Docs projections may expose semantic text
    # roles without the screen-reader live-region page announcement.
    semantic_roles = {"document", "heading", "paragraph", "text"}
    projection = [
        dict(row)
        for row in snapshot
        if str(row.get("role") or "").lower() in semantic_roles
        and any(isinstance(row.get(key), str) and row[key].strip() for key in ("name", "value"))
    ]
    if not projection:
        raise ValueError("browser inspection returned no stable document content projection")
    return projection


def document_projection_sha256(snapshot: list[dict[str, Any]]) -> str:
    return snapshot_sha256(document_projection(snapshot))


def document_text_fragments(snapshot: list[dict[str, Any]]) -> list[str]:
    return snapshot_text_fragments(document_projection(snapshot))


def _iter_actions(actions: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for action in actions:
        yield action
        for branch in action.get("branches", []):
            yield from _iter_actions(branch)


def _validated_target(
    document_id: str,
    collaboration: dict[str, str],
) -> tuple[str, str, str, str]:
    if not DOCUMENT_ID_RE.fullmatch(document_id):
        raise ValueError("document identifier is invalid")
    if not isinstance(collaboration, dict) or set(collaboration) != {
        "collaboration_id", "url", "origin",
    }:
        raise ValueError("an exact active-tab collaboration is required")
    collaboration_id = collaboration.get("collaboration_id")
    target_url = collaboration.get("url")
    target_origin = collaboration.get("origin")
    if (
        not isinstance(collaboration_id, str)
        or not SHA256.fullmatch(collaboration_id)
        or not isinstance(target_url, str)
        or not isinstance(target_origin, str)
    ):
        raise ValueError("the active-tab collaboration is invalid")
    parsed = urlsplit(target_url)
    document_prefix = f"/document/d/{document_id}/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "docs.google.com"
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith(document_prefix)
        or target_origin != "https://docs.google.com"
    ):
        raise ValueError("the active collaboration is not the requested Google document")
    return collaboration_id, target_url, target_origin, document_prefix


def document_id_from_collaboration(collaboration: dict[str, str]) -> str:
    raw_url = collaboration.get("url") if isinstance(collaboration, dict) else None
    if not isinstance(raw_url, str):
        raise ValueError("an exact active-tab collaboration is required")
    parsed = urlsplit(raw_url)
    parts = parsed.path.split("/")
    if len(parts) < 5 or parts[1:3] != ["document", "d"]:
        raise ValueError("the exposed tab is not a Google document")
    document_id = parts[3]
    _validated_target(document_id, collaboration)
    return document_id


def document_id_from_expected_url(expected_url: str) -> str:
    if not isinstance(expected_url, str) or not expected_url:
        raise ValueError("expected_document_url is required")
    expected_parts = urlsplit(expected_url)
    if (
        expected_parts.scheme != "https"
        or expected_parts.netloc != "docs.google.com"
        or expected_parts.username is not None
        or expected_parts.password is not None
    ):
        raise ValueError("expected_document_url must identify a Google document")
    parts = expected_parts.path.split("/")
    if len(parts) < 5 or parts[1:3] != ["document", "d"]:
        raise ValueError("expected_document_url must identify a Google document")
    expected_id = parts[3]
    if not DOCUMENT_ID_RE.fullmatch(expected_id):
        raise ValueError("expected_document_url must identify a Google document")
    return expected_id


def assert_expected_document_url(expected_url: str, collaboration: dict[str, str]) -> str:
    expected_id = document_id_from_expected_url(expected_url)
    live_id = document_id_from_collaboration(collaboration)
    if expected_id != live_id:
        raise ValueError("the exposed tab is not the requested Google document")
    return live_id


def _target(document_id: str, collaboration: dict[str, str]) -> dict[str, Any]:
    collaboration_id, target_url, target_origin, document_prefix = _validated_target(
        document_id, collaboration,
    )
    return {
        "url": target_url,
        "origin": target_origin,
        "path_prefixes": [document_prefix],
        "collaboration_id": collaboration_id,
        # Google Docs' editor and suggestion UI live in the root frame. Avoid
        # unrelated OOPIF accessibility sessions, which can stall full-tree reads.
        "include_child_frames": False,
    }


def _program(
    *,
    program_id: str,
    plan_sha256: str,
    capability: str,
    target: dict[str, Any],
    actions: list[dict[str, Any]],
    private_slots: list[str],
    private_fields: list[str],
    timeout_ms: int,
    max_repeat: int = 8,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "protocol": BROWSER_PROTOCOL,
        "program_id": program_id,
        "program_sha256": "0" * 64,
        "plan_sha256": plan_sha256,
        "driver": {"id": DRIVER_ID, "version": DRIVER_VERSION},
        "capability": capability,
        "target": target,
        "limits": {
            "timeout_ms": timeout_ms,
            "max_actions": len(list(_iter_actions(actions))),
            "max_repeat": max_repeat,
        },
        "private_slots": private_slots,
        "actions": actions,
        "result": {
            "public_fields": [
                "status", "action_count", "mutation_started", "private_result_count",
            ],
            "private_fields": private_fields,
        },
    }
    value["program_sha256"] = canonical_program_sha256(value)
    return value


def _ready_actions() -> list[dict[str, Any]]:
    return [
        {
            "op": "wait_ax",
            "locator": {
                "role": "button",
                "name_contains_any": ["editing", "suggesting", "viewing"],
            },
            "timeout_ms": 30_000,
        },
        {
            "op": "first_success",
            "branches": [
                [
                    {
                        "op": "assert_ax",
                        "locator": {
                            "role": "link",
                            "name_contains": "turn on screen reader support",
                        },
                    },
                    {
                        "op": "dispatch_key_chord",
                        "keys": ["platform-primary", "alt", "z"],
                    },
                    {
                        "op": "wait_ax",
                        "locator": {
                            "role": "button",
                            "name_contains_any": ["editing", "suggesting", "viewing"],
                        },
                        "timeout_ms": 5_000,
                    },
                ],
                [
                    {
                        "op": "assert_ax",
                        "locator": {
                            "role": "button",
                            "name_contains_any": ["editing", "suggesting", "viewing"],
                        },
                    },
                ],
            ],
        },
        {"op": "dispatch_key_chord", "keys": ["escape"]},
        {
            "op": "wait_ax",
            "locator": {
                "role": "button",
                "name_contains_any": ["editing", "suggesting", "viewing"],
            },
            "timeout_ms": 5_000,
        },
    ]


def _bounded_snapshot_actions(private_result: str) -> list[dict[str, Any]]:
    return [
        {
            "op": "wait_dom",
            "locator": {"selector": "#docs-editor", "visible": True},
            "timeout_ms": 5_000,
        },
        {"op": "click_dom", "locator": {"selector": "#docs-editor", "visible": True}},
        {"op": "dispatch_key_chord", "keys": ["document-start"]},
        {"op": "wait_duration", "duration_ms": 250},
        {
            "op": "collect_ax_by_scrolling",
            "locator": dict(SNAPSHOT_LOCATOR),
            "fields": list(SNAPSHOT_FIELDS),
            "private_result": private_result,
            "max_items": SNAPSHOT_MAX_ITEMS,
            "direction": "down",
            "distance_px": 640,
            "max_scrolls": INSPECTION_MAX_SCROLLS,
            "settle_ms": 250,
            "dedupe_fields": list(SNAPSHOT_FIELDS),
            "stable_rounds": 3,
            "scroll_anchor": {"selector": "#docs-editor", "visible": True},
        },
        {"op": "dispatch_key_chord", "keys": ["document-start"]},
    ]


def compile_inspection_program(
    document_id: str,
    collaboration: dict[str, str],
) -> dict[str, Any]:
    target = _target(document_id, collaboration)
    plan_sha256 = hashlib.sha256(json.dumps({
        "purpose": "google-docs-browser-inspection",
        "collaboration_id": collaboration["collaboration_id"],
        "url": collaboration["url"],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    actions = [
        {"op": "open_or_focus_exact_url"},
        {"op": "assert_exact_target"},
        {"op": "attach_debugger"},
        *_ready_actions(),
        *_bounded_snapshot_actions("docs.ax"),
        {"op": "detach_debugger"},
    ]
    return _program(
        program_id="google-docs-inspection-v1",
        plan_sha256=plan_sha256,
        capability="read",
        target=target,
        actions=actions,
        private_slots=[],
        private_fields=["docs.ax"],
        timeout_ms=60_000,
        max_repeat=INSPECTION_MAX_SCROLLS,
    )


def _mode_actions() -> list[dict[str, Any]]:
    return [
        {
            "op": "first_success",
            "branches": [
                [
                    {
                        "op": "dispatch_key_chord",
                        "keys": ["platform-primary", "alt", "shift", "x"],
                    },
                    {
                        "op": "wait_ax",
                        "locator": {"role": "button", "name_contains": "suggesting"},
                        "timeout_ms": 3_000,
                    },
                ],
                [
                    {
                        "op": "click_dom",
                        "locator": {"selector": "#docs-mode-switcher-select", "visible": True},
                    },
                    {
                        "op": "click_ax",
                        "locator": {
                            "roles": ["menuitem", "menuitemradio"],
                            "name": "Suggesting",
                        },
                    },
                ],
            ],
        },
        {"op": "assert_ax", "locator": {"role": "button", "name_contains": "suggesting"}},
    ]


def _dialog_actions() -> list[dict[str, Any]]:
    find_field = {"role": "textbox", "name": "Find", "unique": True}
    replace_field = {"role": "textbox", "name": "Replace with", "unique": True}
    return [
        {
            "op": "first_success",
            "branches": [
                [
                    {"op": "click_dom", "locator": {"selector": "#docs-edit-menu", "visible": True}},
                    {
                        "op": "click_ax",
                        "locator": {
                            "roles": ["menuitem", "menuitemradio"],
                            "name_contains": "find and replace",
                        },
                    },
                    {
                        "op": "wait_ax",
                        "locator": dict(find_field),
                        "timeout_ms": 5_000,
                    },
                ],
                [
                    {"op": "dispatch_key_chord", "keys": ["escape"]},
                    {"op": "dispatch_key_chord", "keys": ["platform-primary", "shift", "h"]},
                    {
                        "op": "wait_ax",
                        "locator": dict(find_field),
                        "timeout_ms": 5_000,
                    },
                ],
            ],
        },
        {
            "op": "wait_ax",
            "locator": dict(find_field),
            "timeout_ms": 5_000,
        },
        {
            "op": "wait_ax",
            "locator": dict(replace_field),
            "timeout_ms": 5_000,
        },
    ]


def _preflight_edit_actions(index: int) -> list[dict[str, Any]]:
    return [
        {
            "op": "focus_ax",
            "locator": {"role": "textbox", "name": "Find", "unique": True},
        },
        {"op": "dispatch_key_chord", "keys": ["platform-primary", "a"]},
        {"op": "dispatch_key_chord", "keys": ["backspace"]},
        {"op": "insert_private_text", "slot": f"edit.{index:03d}.find", "replace_all": False},
        {
            "op": "wait_ax_private_value",
            "slot": f"edit.{index:03d}.find",
            "timeout_ms": 5_000,
        },
        {
            "op": "wait_ax",
            "locator": {"role": "statictext", "name": "1 of 1"},
            "timeout_ms": 5_000,
        },
    ]


def _apply_edit_actions(index: int) -> list[dict[str, Any]]:
    prefix = f"edit.{index:03d}"
    return [
        {
            "op": "focus_ax",
            "locator": {"role": "textbox", "name": "Find", "unique": True},
        },
        {"op": "dispatch_key_chord", "keys": ["platform-primary", "a"]},
        {"op": "dispatch_key_chord", "keys": ["backspace"]},
        {"op": "insert_private_text", "slot": f"{prefix}.find", "replace_all": False},
        {"op": "wait_ax_private_value", "slot": f"{prefix}.find", "timeout_ms": 5_000},
        {
            "op": "wait_ax",
            "locator": {"role": "statictext", "name": "1 of 1"},
            "timeout_ms": 5_000,
        },
        {
            "op": "focus_ax",
            "locator": {"role": "textbox", "name": "Replace with", "unique": True},
        },
        {"op": "dispatch_key_chord", "keys": ["platform-primary", "a"]},
        {"op": "dispatch_key_chord", "keys": ["backspace"]},
        {"op": "insert_private_text", "slot": f"{prefix}.replace", "replace_all": False},
        {"op": "wait_ax_private_value", "slot": f"{prefix}.replace", "timeout_ms": 5_000},
        {"op": "click_ax", "locator": {"role": "button", "name": "Replace"}},
    ]


def compile_suggestion_presence_program(
    document_id: str,
    plan_sha256: str,
    edits: list[dict[str, Any]],
    collaboration: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Compile an exact Docs Find probe that cannot change document content."""
    if not SHA256.fullmatch(plan_sha256):
        raise ValueError("plan_sha256 must be lowercase hexadecimal SHA-256")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BROWSER_EDITS:
        raise ValueError(f"presence probes require 1-{MAX_BROWSER_EDITS} edits")
    private_values: dict[str, str] = {}
    actions = [
        {"op": "open_or_focus_exact_url"},
        {"op": "assert_exact_target"},
        {"op": "attach_debugger"},
        *_ready_actions(),
        *_dialog_actions(),
    ]
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) not in ({"append"}, {"find", "replace"}):
            raise ValueError("every presence probe needs one exact replacement or append")
        text = edit.get("append") if "append" in edit else edit.get("replace")
        if not isinstance(text, str) or not text:
            raise ValueError("presence probe text must be non-empty")
        if len(text.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES:
            raise ValueError("presence probe text is too large for the shared executor")
        slot = f"verify.{index:03d}.text"
        private_values[slot] = text
        actions.extend([
            {
                "op": "focus_ax",
                "locator": {"role": "textbox", "name": "Find", "unique": True},
            },
            {"op": "dispatch_key_chord", "keys": ["platform-primary", "a"]},
            {"op": "dispatch_key_chord", "keys": ["backspace"]},
            {
                "op": "focus_ax",
                "locator": {"role": "textbox", "name": "Find", "unique": True},
            },
            {"op": "insert_private_text", "slot": slot, "replace_all": False},
            {"op": "wait_ax_private_value", "slot": slot, "timeout_ms": 5_000},
            {
                "op": "wait_ax",
                "locator": {
                    "role": "statictext",
                    "name_matches": r"^1 of [1-9][0-9]*$",
                },
                "timeout_ms": 5_000,
            },
        ])
    # The executor treats private text entry as a mutation-capability action,
    # even though every entry above is confined to Docs' Find dialog. Keep the
    # required boundary last so no action after it can alter document content.
    actions.extend([
        {"op": "dispatch_key_chord", "keys": ["escape"]},
        {
            "op": "wait_ax",
            "locator": {"role": "button", "name_contains": "suggesting"},
            "timeout_ms": 5_000,
        },
        {"op": "before_mutation"},
        {"op": "detach_debugger"},
    ])
    return _program(
        program_id="google-docs-suggestion-presence-v1",
        plan_sha256=plan_sha256,
        capability="mutation",
        target=_target(document_id, collaboration),
        actions=actions,
        private_slots=list(private_values),
        private_fields=[],
        timeout_ms=60_000,
    ), private_values


def compile_suggestion_program(
    document_id: str,
    plan_sha256: str,
    edits: list[dict[str, Any]],
    collaboration: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    if not SHA256.fullmatch(plan_sha256):
        raise ValueError("plan_sha256 must be lowercase hexadecimal SHA-256")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BROWSER_EDITS:
        raise ValueError(f"shared-executor programs require 1-{MAX_BROWSER_EDITS} edits")
    target = _target(document_id, collaboration)

    slots: list[str] = []
    private_values: dict[str, str] = {}
    append_indexes: list[int] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError("every edit must be an object")
        prefix = f"edit.{index:03d}"
        if set(edit) == {"append"}:
            append = edit.get("append")
            if not isinstance(append, str) or not append:
                raise ValueError("append edits require non-empty text")
            if len(append.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES:
                raise ValueError("an edit value is too large for the shared executor")
            append_indexes.append(index)
            slots.append(f"{prefix}.append")
            private_values[f"{prefix}.append"] = append
            continue
        if set(edit) != {"find", "replace"}:
            raise ValueError("every edit must be one exact replacement or append")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str) or replace == find:
            raise ValueError("every replacement requires different non-empty find and replace text")
        if any(len(value.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES for value in (find, replace)):
            raise ValueError("an edit value is too large for the shared executor")
        slots.extend((f"{prefix}.find", f"{prefix}.replace"))
        private_values[f"{prefix}.find"] = find
        private_values[f"{prefix}.replace"] = replace
    if append_indexes and (len(append_indexes) != 1 or len(edits) != 1):
        raise ValueError("append plans must contain exactly one append edit")

    actions: list[dict[str, Any]] = [
        {"op": "open_or_focus_exact_url"},
        {"op": "assert_exact_target"},
        {"op": "attach_debugger"},
        *_ready_actions(),
        *_mode_actions(),
    ]
    if append_indexes:
        actions.extend([
            {
                "op": "wait_dom",
                "locator": {"selector": "#docs-editor", "visible": True},
                "timeout_ms": 5_000,
            },
            {"op": "click_dom", "locator": {"selector": "#docs-editor", "visible": True}},
            {"op": "dispatch_key_chord", "keys": ["document-end"]},
            {"op": "assert_ax", "locator": {"role": "button", "name_contains": "suggesting"}},
        ])
    else:
        actions.extend(_dialog_actions())
        for index in range(len(edits)):
            actions.extend(_preflight_edit_actions(index))
    actions.append({"op": "before_mutation"})
    if append_indexes:
        index = append_indexes[0]
        actions.extend([
            {"op": "dispatch_key_chord", "keys": ["enter"]},
            {
                "op": "insert_private_text",
                "slot": f"edit.{index:03d}.append",
                "replace_all": False,
            },
        ])
    else:
        for index in range(len(edits)):
            actions.extend(_apply_edit_actions(index))
    actions.extend([
        {"op": "assert_ax", "locator": {"role": "button", "name_contains": "suggesting"}},
        {"op": "dispatch_key_chord", "keys": ["escape"]},
        {
            "op": "wait_ax",
            "locator": {"role": "button", "name_contains": "suggesting"},
            "timeout_ms": 5_000,
        },
        *_bounded_snapshot_actions("docs.after-ax"),
        {"op": "detach_debugger"},
    ])
    return _program(
        program_id="google-docs-suggestions-v2",
        plan_sha256=plan_sha256,
        capability="mutation",
        target=target,
        actions=actions,
        private_slots=slots,
        private_fields=["docs.after-ax"],
        timeout_ms=180_000,
        max_repeat=INSPECTION_MAX_SCROLLS,
    ), private_values
