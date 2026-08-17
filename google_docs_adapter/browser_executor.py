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
DRIVER_VERSION = "collaboration-2"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_BROWSER_EDITS = 15
# Kept while the unreleased shadow-compiler tests migrate to the production name.
MAX_SHADOW_EDITS = MAX_BROWSER_EDITS
MAX_PRIVATE_VALUE_BYTES = 16_384
SNAPSHOT_FIELDS = ["role", "name", "value", "description"]
SNAPSHOT_LOCATOR = {"name_matches": ".+"}
SNAPSHOT_MAX_ITEMS = 5000


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
            "max_repeat": 8,
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


def _snapshot_action(private_result: str) -> dict[str, Any]:
    return {
        "op": "extract_ax_collection",
        "locator": dict(SNAPSHOT_LOCATOR),
        "fields": list(SNAPSHOT_FIELDS),
        "private_result": private_result,
        "max_items": SNAPSHOT_MAX_ITEMS,
    }


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
        _snapshot_action("docs.ax"),
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
    )


def _mode_actions() -> list[dict[str, Any]]:
    return [
        {
            "op": "first_success",
            "branches": [
                [
                    {"op": "dispatch_key_chord", "keys": ["platform-primary", "shift", "x"]},
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
    return [
        {
            "op": "first_success",
            "branches": [
                [
                    {"op": "click_dom", "locator": {"selector": "#docs-edit-menu", "visible": True}},
                    {
                        "op": "click_ax",
                        "locator": {"roles": ["menuitem", "menuitemradio"], "name": "Find and replace"},
                    },
                ],
                [
                    {"op": "dispatch_key_chord", "keys": ["platform-primary", "shift", "h"]},
                    {
                        "op": "wait_ax",
                        "locator": {"role": "dialog", "name": "Find and replace"},
                        "timeout_ms": 5_000,
                    },
                ],
            ],
        },
    ]


def _preflight_edit_actions(index: int) -> list[dict[str, Any]]:
    dialog = {"role": "dialog", "name": "Find and replace"}
    return [
        {"op": "focus_ax", "locator": {"role": "textbox", "ordinal": 0, "within": dialog}},
        {"op": "insert_private_text", "slot": f"edit.{index:03d}.find", "replace_all": True},
        {"op": "assert_ax", "locator": {"role": "statictext", "name": "1 of 1"}},
    ]


def _apply_edit_actions(index: int) -> list[dict[str, Any]]:
    prefix = f"edit.{index:03d}"
    dialog = {"role": "dialog", "name": "Find and replace"}
    return [
        {"op": "focus_ax", "locator": {"role": "textbox", "ordinal": 0, "within": dialog}},
        {"op": "insert_private_text", "slot": f"{prefix}.find", "replace_all": True},
        {"op": "focus_ax", "locator": {"role": "textbox", "ordinal": 1, "within": dialog}},
        {"op": "insert_private_text", "slot": f"{prefix}.replace", "replace_all": True},
        {"op": "click_ax", "locator": {"role": "button", "name": "Replace"}},
    ]


def compile_suggestion_program(
    document_id: str,
    plan_sha256: str,
    edits: list[dict[str, Any]],
    collaboration: dict[str, str],
    revision_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    if not SHA256.fullmatch(plan_sha256):
        raise ValueError("plan_sha256 must be lowercase hexadecimal SHA-256")
    if revision_sha256 is None:
        # Compatibility for older shadow compiler tests. Production browser-only
        # mutation always supplies the inspected revision fingerprint.
        revision_sha256 = "0" * 64
    if not SHA256.fullmatch(revision_sha256):
        raise ValueError("revision_sha256 must be lowercase hexadecimal SHA-256")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BROWSER_EDITS:
        raise ValueError(f"shared-executor programs require 1-{MAX_BROWSER_EDITS} edits")
    target = _target(document_id, collaboration)

    slots = ["baseline.sha256"]
    private_values = {"baseline.sha256": revision_sha256}
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError("every edit must be an object")
        find = edit.get("find")
        replace = edit.get("replace")
        if not isinstance(find, str) or not find or not isinstance(replace, str) or replace == find:
            raise ValueError("every edit requires different non-empty find and string replace values")
        if any(len(value.encode("utf-8")) > MAX_PRIVATE_VALUE_BYTES for value in (find, replace)):
            raise ValueError("an edit value is too large for the shared executor")
        prefix = f"edit.{index:03d}"
        slots.extend((f"{prefix}.find", f"{prefix}.replace"))
        private_values[f"{prefix}.find"] = find
        private_values[f"{prefix}.replace"] = replace

    actions: list[dict[str, Any]] = [
        {"op": "open_or_focus_exact_url"},
        {"op": "assert_exact_target"},
        {"op": "attach_debugger"},
        *_ready_actions(),
        {
            "op": "assert_ax_private_sha256",
            "slot": "baseline.sha256",
            "locator": dict(SNAPSHOT_LOCATOR),
            "fields": list(SNAPSHOT_FIELDS),
            "max_items": SNAPSHOT_MAX_ITEMS,
        },
        *_mode_actions(),
        *_dialog_actions(),
    ]
    for index in range(len(edits)):
        actions.extend(_preflight_edit_actions(index))
    actions.append({"op": "before_mutation"})
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
        _snapshot_action("docs.after-ax"),
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
    ), private_values
