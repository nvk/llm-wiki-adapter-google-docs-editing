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
DRIVER_VERSION = "collaboration-1"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_SHADOW_EDITS = 16
MAX_PRIVATE_VALUE_BYTES = 16_384


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


def _iter_actions(actions: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for action in actions:
        yield action
        for branch in action.get("branches", []):
            yield from _iter_actions(branch)


def _mode_actions() -> list[dict[str, Any]]:
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
                        "op": "dispatch_key_chord",
                        "keys": ["platform-primary", "shift", "x"],
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
    return [
        {
            "op": "first_success",
            "branches": [
                [
                    {
                        "op": "click_dom",
                        "locator": {"selector": "#docs-edit-menu", "visible": True},
                    },
                    {
                        "op": "click_ax",
                        "locator": {
                            "roles": ["menuitem", "menuitemradio"],
                            "name": "Find and replace",
                        },
                    },
                ],
                [
                    {
                        "op": "dispatch_key_chord",
                        "keys": ["platform-primary", "shift", "h"],
                    },
                    {
                        "op": "wait_ax",
                        "locator": {"role": "dialog", "name": "Find and replace"},
                        "timeout_ms": 5_000,
                    },
                ],
            ],
        }
    ]


def _edit_actions(index: int) -> list[dict[str, Any]]:
    prefix = f"edit.{index:03d}"
    dialog = {"role": "dialog", "name": "Find and replace"}
    return [
        {
            "op": "focus_ax",
            "locator": {"role": "textbox", "ordinal": 0, "within": dialog},
        },
        {"op": "insert_private_text", "slot": f"{prefix}.find", "replace_all": True},
        {"op": "assert_ax_private_value", "slot": f"{prefix}.find"},
        {
            "op": "focus_ax",
            "locator": {"role": "textbox", "ordinal": 1, "within": dialog},
        },
        {"op": "insert_private_text", "slot": f"{prefix}.replace", "replace_all": True},
        {"op": "assert_ax_private_value", "slot": f"{prefix}.replace"},
        {"op": "assert_ax", "locator": {"role": "statictext", "name": "1 of 1"}},
    ]


def compile_suggestion_program(
    document_id: str,
    plan_sha256: str,
    edits: list[dict[str, Any]],
    collaboration: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    if not DOCUMENT_ID_RE.fullmatch(document_id):
        raise ValueError("document identifier is invalid")
    if not SHA256.fullmatch(plan_sha256):
        raise ValueError("plan_sha256 must be lowercase hexadecimal SHA-256")
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_SHADOW_EDITS:
        raise ValueError(f"shared-executor shadow programs require 1-{MAX_SHADOW_EDITS} edits")
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

    slots: list[str] = []
    private_values: dict[str, str] = {}
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
        *_mode_actions(),
        *_dialog_actions(),
    ]
    for index in range(len(edits)):
        actions.extend(_edit_actions(index))
        if index == 0:
            actions.append({"op": "before_mutation"})
        actions.append({"op": "click_ax", "locator": {"role": "button", "name": "Replace"}})
    actions.extend([
        {"op": "assert_ax", "locator": {"role": "button", "name_contains": "suggesting"}},
        {"op": "detach_debugger"},
    ])

    program: dict[str, Any] = {
        "protocol": BROWSER_PROTOCOL,
        "program_id": "google-docs-suggestions-v1",
        "program_sha256": "0" * 64,
        "plan_sha256": plan_sha256,
        "driver": {"id": DRIVER_ID, "version": DRIVER_VERSION},
        "capability": "mutation",
        "target": {
            "url": target_url,
            "origin": target_origin,
            "path_prefixes": [document_prefix],
            "collaboration_id": collaboration_id,
        },
        "limits": {
            "timeout_ms": 120_000,
            "max_actions": len(list(_iter_actions(actions))),
            "max_repeat": 8,
        },
        "private_slots": slots,
        "actions": actions,
        "result": {
            "public_fields": ["status", "action_count", "mutation_started"],
            "private_fields": [],
        },
    }
    program["program_sha256"] = canonical_program_sha256(program)
    return program, private_values
