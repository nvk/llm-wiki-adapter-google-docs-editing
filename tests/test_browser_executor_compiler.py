from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from google_docs_adapter.browser_executor import (
    MAX_SHADOW_EDITS,
    canonical_program_sha256,
    compile_inspection_program,
    compile_source_preflight_program,
    compile_suggestion_presence_program,
    compile_suggestion_program,
)

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_ID = "SyntheticBrowserExecutorDocument123"
PLAN_SHA256 = "c" * 64
COLLABORATION = {
    "collaboration_id": "d" * 64,
    "url": f"https://docs.google.com/document/d/{DOCUMENT_ID}/edit?tab=t.0",
    "origin": "https://docs.google.com",
}


def flatten(actions: list[dict]) -> list[dict]:
    result: list[dict] = []
    for action in actions:
        result.append(action)
        for branch in action.get("branches", []):
            result.extend(flatten(branch))
    return result


class BrowserExecutorCompilerTests(unittest.TestCase):
    def test_compiler_keeps_edit_text_in_private_slots(self) -> None:
        edits = [
            {"find": "Synthetic old one", "replace": "Synthetic new one"},
            {"find": "Synthetic old two", "replace": "Synthetic new two"},
        ]
        program, private_values = compile_suggestion_program(
            DOCUMENT_ID, PLAN_SHA256, edits, COLLABORATION,
        )
        encoded_program = json.dumps(program)
        for edit in edits:
            self.assertNotIn(edit["find"], encoded_program)
            self.assertNotIn(edit["replace"], encoded_program)
        self.assertEqual(set(private_values), set(program["private_slots"]))
        self.assertEqual(program["program_sha256"], canonical_program_sha256(program))
        self.assertEqual(program["plan_sha256"], PLAN_SHA256)
        self.assertEqual(program["target"]["origin"], "https://docs.google.com")

        flat = flatten(program["actions"])
        operations = [action["op"] for action in flat]
        self.assertEqual(operations.count("before_mutation"), 1)
        self.assertEqual(operations.count("click_ax"), len(edits) + 2)
        self.assertIn(
            {
                "op": "dispatch_key_chord",
                "keys": ["platform-primary", "alt", "shift", "x"],
            },
            flat,
        )
        self.assertIn(
            {
                "op": "dispatch_key_chord",
                "keys": ["platform-primary", "alt", "z"],
            },
            flat,
        )
        self.assertNotIn(
            {"op": "dispatch_key_chord", "keys": ["platform-primary", "shift", "x"]},
            flat,
        )
        self.assertIn(
            {
                "op": "click_ax",
                "locator": {
                    "roles": ["menuitem", "menuitemradio"],
                    "name_contains": "find and replace",
                },
            },
            flat,
        )
        boundary = operations.index("before_mutation")
        self.assertNotIn("first_success", operations[boundary + 1:])
        after_collections = [
            action
            for action in flat[boundary + 1:]
            if action["op"] == "collect_ax_by_scrolling"
        ]
        self.assertEqual(len(after_collections), 1)
        self.assertEqual(after_collections[0]["private_result"], "docs.after-ax")
        self.assertEqual(after_collections[0]["max_scrolls"], 20)
        self.assertEqual(program["limits"]["max_repeat"], 20)
        self.assertEqual(len(flat), program["limits"]["max_actions"])

    def test_compiler_caps_batch_and_private_value_sizes(self) -> None:
        maximum_program, _values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [
                {"find": f"Synthetic old {index}", "replace": f"Synthetic new {index}"}
                for index in range(MAX_SHADOW_EDITS)
            ],
            COLLABORATION,
        )
        self.assertLessEqual(maximum_program["limits"]["max_actions"], 200)
        with self.assertRaisesRegex(ValueError, f"1-{MAX_SHADOW_EDITS}"):
            compile_suggestion_program(
                DOCUMENT_ID,
                PLAN_SHA256,
                [{"find": f"Synthetic {index}", "replace": "Replacement"} for index in range(MAX_SHADOW_EDITS + 1)],
                COLLABORATION,
            )
        with self.assertRaisesRegex(ValueError, "too large"):
            compile_suggestion_program(
                DOCUMENT_ID,
                PLAN_SHA256,
                [{"find": "x" * 16_385, "replace": "Synthetic"}],
                COLLABORATION,
            )

        with self.assertRaisesRegex(ValueError, "requested Google document"):
            compile_suggestion_program(
                DOCUMENT_ID,
                PLAN_SHA256,
                [{"find": "Synthetic old", "replace": "Synthetic new"}],
                {
                    "collaboration_id": "d" * 64,
                    "url": "https://example.invalid/private",
                    "origin": "https://example.invalid",
                },
            )

    @unittest.skipUnless(os.environ.get("LLM_WIKI_BROWSER_EXECUTOR_ROOT"), "shared executor source is not configured")
    def test_compiled_program_passes_shared_validator(self) -> None:
        executor_root = Path(os.environ["LLM_WIKI_BROWSER_EXECUTOR_ROOT"]).resolve(strict=True)
        suggestion_program, _values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [
                {"find": f"Synthetic old {index}", "replace": f"Synthetic new {index}"}
                for index in range(MAX_SHADOW_EDITS)
            ],
            COLLABORATION,
        )
        presence_program, _values = compile_suggestion_presence_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"append": "Synthetic appended suggestion."}],
            COLLABORATION,
        )
        source_program, _values = compile_source_preflight_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"find": "Synthetic old", "replace": "Synthetic new"}],
            COLLABORATION,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "program.json"
            for program in (suggestion_program, presence_program, source_program):
                path.write_text(json.dumps(program), encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import json,sys; from browser_executor.protocol import validate_program; "
                        "validate_program(json.load(open(sys.argv[1])))",
                        str(path),
                    ],
                    cwd=executor_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_compiler_uses_stable_preflights_before_the_governed_boundary(self) -> None:
        program, private_values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"find": "Synthetic old", "replace": "Synthetic new"}],
            COLLABORATION,
        )
        flat = flatten(program["actions"])
        operations = [action["op"] for action in flat]
        boundary = operations.index("before_mutation")
        self.assertNotIn("assert_ax_private_sha256", operations)
        self.assertNotIn("baseline.sha256", private_values)
        self.assertEqual(operations[:boundary].count("wait_ax_private_value"), 2)
        self.assertEqual(operations[boundary + 1:].count("wait_ax_private_value"), 0)
        self.assertEqual(operations[boundary + 1], "click_ax")
        self.assertEqual(flat[boundary + 1]["locator"], {
            "role": "button",
            "name": "Replace",
        })
        for index, action in enumerate(flat):
            if action["op"] == "insert_private_text":
                self.assertEqual(flat[index + 1]["op"], "wait_ax_private_value")
                self.assertEqual(flat[index + 1]["slot"], action["slot"])
        self.assertIn({
            "op": "wait_ax",
            "locator": {
                "name": "1 of 1",
            },
            "timeout_ms": 15_000,
        }, flat[:boundary])
        self.assertIn({
            "op": "focus_ax",
            "locator": {"role": "textbox", "name": "Find", "unique": True},
        }, flat)
        self.assertIn({
            "op": "focus_ax",
            "locator": {
                "role": "textbox",
                "name": "Replace with",
                "unique": True,
            },
        }, flat)
        self.assertEqual(program["result"]["private_fields"], ["docs.after-ax"])

    def test_find_and_replace_readiness_uses_fields_not_a_volatile_dialog_container(self) -> None:
        program, _private_values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"find": "Synthetic old", "replace": "Synthetic new"}],
            COLLABORATION,
        )
        flat = flatten(program["actions"])
        self.assertFalse(any(
            action.get("op") == "wait_ax"
            and action.get("locator", {}).get("role") == "dialog"
            for action in flat
        ))
        self.assertIn({
            "op": "wait_ax",
            "locator": {"role": "textbox", "name": "Find", "unique": True},
            "timeout_ms": 5_000,
        }, flat)
        self.assertIn({
            "op": "wait_ax",
            "locator": {"role": "textbox", "name": "Replace with", "unique": True},
            "timeout_ms": 5_000,
        }, flat)

    def test_inspection_collects_a_bounded_document_scan_and_restores_start(self) -> None:
        program = compile_inspection_program(DOCUMENT_ID, COLLABORATION)
        flat = flatten(program["actions"])
        editor_click = flat.index({
            "op": "click_dom",
            "locator": {"selector": "#docs-editor", "visible": True},
        })
        self.assertEqual(flat[editor_click - 1], {
            "op": "wait_dom",
            "locator": {"selector": "#docs-editor", "visible": True},
            "timeout_ms": 5_000,
        })
        self.assertEqual(flat[editor_click - 2], {
            "op": "dispatch_key_chord",
            "keys": ["escape"],
        })
        collection = next(action for action in flat if action["op"] == "collect_ax_by_scrolling")
        self.assertEqual(collection["scroll_anchor"], {
            "selector": "#docs-editor",
            "visible": True,
        })
        self.assertEqual(collection["max_scrolls"], 20)
        self.assertEqual(program["limits"]["max_repeat"], 20)
        self.assertEqual(
            [action for action in flat if action["op"] == "dispatch_key_chord"].count(
                {"op": "dispatch_key_chord", "keys": ["document-start"]}
            ),
            2,
        )

    def test_compiler_supports_one_private_append_suggestion(self) -> None:
        text = "Synthetic appended suggestion."
        program, private_values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"append": text}],
            COLLABORATION,
        )
        encoded_program = json.dumps(program)
        self.assertNotIn(text, encoded_program)
        self.assertEqual(private_values["edit.000.append"], text)
        operations = [action["op"] for action in flatten(program["actions"])]
        boundary = operations.index("before_mutation")
        self.assertIn("wait_dom", operations[:boundary])
        self.assertIn("click_dom", operations[:boundary])
        self.assertIn("insert_private_text", operations[boundary + 1:])
        self.assertNotIn("focus_ax", operations)
        self.assertIn(
            {"op": "dispatch_key_chord", "keys": ["document-end"]},
            flatten(program["actions"])[:boundary],
        )

        with self.assertRaisesRegex(ValueError, "exactly one append"):
            compile_suggestion_program(
                DOCUMENT_ID,
                PLAN_SHA256,
                [{"append": "One"}, {"append": "Two"}],
                COLLABORATION,
            )

    def test_presence_probe_confines_private_text_to_find_before_boundary(self) -> None:
        text = "Synthetic appended suggestion."
        program, private_values = compile_suggestion_presence_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"append": text}],
            COLLABORATION,
        )
        self.assertNotIn(text, json.dumps(program))
        self.assertEqual(private_values, {"verify.000.text": text})
        flat = flatten(program["actions"])
        operations = [action["op"] for action in flat]
        boundary = operations.index("before_mutation")
        self.assertEqual(operations[boundary + 1 :], ["detach_debugger"])
        self.assertEqual(operations[:boundary].count("insert_private_text"), 1)
        self.assertIn({
            "op": "focus_ax",
            "locator": {"role": "textbox", "name": "Find", "unique": True},
        }, flat[:boundary])
        self.assertIn({
            "op": "wait_ax",
            "locator": {
                "name_matches": r"^1 of [1-9][0-9]*$",
            },
            "timeout_ms": 15_000,
        }, flat[:boundary])

    def test_source_preflight_requires_one_unique_exact_find_result(self) -> None:
        text = "Synthetic source text."
        program, private_values = compile_source_preflight_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"find": text, "replace": "Synthetic replacement."}],
            COLLABORATION,
        )
        self.assertNotIn(text, json.dumps(program))
        self.assertEqual(private_values, {"source.000.text": text})
        flat = flatten(program["actions"])
        operations = [action["op"] for action in flat]
        boundary = operations.index("before_mutation")
        self.assertEqual(operations[boundary + 1 :], ["detach_debugger"])
        self.assertEqual(operations[:boundary].count("insert_private_text"), 1)
        self.assertIn({
            "op": "wait_ax",
            "locator": {
                "name": "1 of 1",
            },
            "timeout_ms": 15_000,
        }, flat[:boundary])

    def test_find_result_locators_avoid_role_casing_and_invalid_ancestry(self) -> None:
        program, _private_values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"find": "Synthetic old", "replace": "Synthetic new"}],
            COLLABORATION,
        )
        result_waits = [
            action for action in flatten(program["actions"])
            if action.get("op") == "wait_ax"
            and action.get("locator", {}).get("name") == "1 of 1"
        ]
        self.assertEqual(len(result_waits), 1)
        self.assertTrue(all(
            "role" not in action["locator"]
            and "roles" not in action["locator"]
            and "within" not in action["locator"]
            and "within_name_contains_any" not in action["locator"]
            for action in result_waits
        ))


if __name__ == "__main__":
    unittest.main()
