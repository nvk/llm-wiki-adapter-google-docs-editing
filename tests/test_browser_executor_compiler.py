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
        self.assertEqual(len(flat), program["limits"]["max_actions"])

    def test_compiler_caps_batch_and_private_value_sizes(self) -> None:
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
        program, _values = compile_suggestion_program(
            DOCUMENT_ID,
            PLAN_SHA256,
            [{"find": "Synthetic old", "replace": "Synthetic new"}],
            COLLABORATION,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "program.json"
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
        self.assertEqual(operations[:boundary].count("wait_ax_private_value"), 1)
        self.assertEqual(operations[boundary + 1:].count("wait_ax_private_value"), 2)
        for index, action in enumerate(flat):
            if action["op"] == "insert_private_text":
                self.assertEqual(flat[index + 1]["op"], "wait_ax_private_value")
                self.assertEqual(flat[index + 1]["slot"], action["slot"])
        self.assertIn(
            {"op": "assert_ax", "locator": {"role": "statictext", "name": "1 of 1"}},
            flat[:boundary],
        )
        self.assertEqual(program["result"]["private_fields"], ["docs.after-ax"])

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

        with self.assertRaisesRegex(ValueError, "exactly one append"):
            compile_suggestion_program(
                DOCUMENT_ID,
                PLAN_SHA256,
                [{"append": "One"}, {"append": "Two"}],
                COLLABORATION,
            )


if __name__ == "__main__":
    unittest.main()
