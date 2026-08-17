from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "design" / "browser-executor-extraction-ledger.json"
PROGRAM = ROOT / "tests" / "fixtures" / "browser-actions" / "google-docs-suggestions-v1.json"
WORKER = ROOT / "extension" / "service-worker.js"
FORBIDDEN_OPS = {"evaluate", "runtime_evaluate", "javascript", "execute_script"}


def actions(value: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for action in value:
        yield action
        for branch in action.get("branches", []):
            yield from actions(branch)


class BrowserExecutorSpikeTests(unittest.TestCase):
    def test_extraction_ledger_accounts_for_every_worker_function(self) -> None:
        source = WORKER.read_text(encoding="utf-8")
        declared = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(", source, re.MULTILINE))
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        classified = set(ledger["functions"])
        self.assertEqual(classified, declared)
        self.assertEqual(
            ledger["functions"]["evaluate"],
            {"category": "forbidden-generic", "disposition": "remove"},
        )

    def test_synthetic_program_is_exact_target_typed_and_governed(self) -> None:
        program = json.loads(PROGRAM.read_text(encoding="utf-8"))
        self.assertEqual(program["protocol"], "llm-wiki-browser-executor/v1")
        self.assertEqual(program["capability"], "mutation")
        self.assertEqual(program["target"]["origin"], "https://docs.google.com")
        self.assertRegex(program["plan_sha256"], r"^[a-f0-9]{64}$")
        flat = list(actions(program["actions"]))
        operations = [action["op"] for action in flat]
        self.assertTrue(FORBIDDEN_OPS.isdisjoint(operations))
        self.assertNotIn("script", json.dumps(program).lower())
        self.assertEqual(operations.count("before_mutation"), 1)
        boundary = operations.index("before_mutation")
        self.assertIn("click_ax", operations[boundary + 1 :])
        self.assertLessEqual(len(flat), program["limits"]["max_actions"])
        self.assertEqual(program["result"]["private_fields"], [])


if __name__ == "__main__":
    unittest.main()
