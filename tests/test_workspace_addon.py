from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "google_workspace_addon"


class WorkspaceAddonTests(unittest.TestCase):
    def test_manifest_is_docs_only_and_per_file(self) -> None:
        manifest = json.loads((ADDON / "appsscript.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest["oauthScopes"]),
            {
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/script.external_request",
            },
        )
        self.assertEqual(
            manifest["addOns"]["docs"]["onFileScopeGrantedTrigger"]["runFunction"],
            "onFileScopeGranted",
        )
        self.assertEqual(
            manifest["addOns"]["common"]["homepageTrigger"],
            {"runFunction": "onDocsHomepage", "enabled": True},
        )
        self.assertEqual(
            manifest["urlFetchWhitelist"], ["https://docs.googleapis.com/"]
        )
        self.assertEqual(manifest["executionApi"], {"access": "MYSELF"})
        self.assertNotIn("openLinkUrlPrefixes", manifest["addOns"]["common"])

    def test_addon_grants_current_file_and_exposes_only_bounded_bridge(self) -> None:
        source = (ADDON / "Code.gs").read_text(encoding="utf-8")
        self.assertIn("requestFileScopeForActiveDocument", source)
        self.assertIn("llmWikiBridgeGetDocument", source)
        self.assertIn("llmWikiBridgeBatchUpdate", source)
        self.assertIn("UrlFetchApp", source)
        self.assertIn("ScriptApp.getOAuthToken", source)
        self.assertIn("writeMode !== 'SUGGEST'", source)
        self.assertIn("commentUpdateState !== 'ALL_SAVED'", source)
        self.assertIn("requiredRevisionId", source)
        self.assertNotIn("DocumentApp", source)

    def test_apps_script_source_is_valid_javascript(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        source = (ADDON / "Code.gs").read_text(encoding="utf-8")
        completed = subprocess.run(
            ["node", "--check"],
            input=source,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_bridge_accepts_only_revision_locked_suggestion_batches(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        source = (ADDON / "Code.gs").read_text(encoding="utf-8")
        harness = source + "\nvalidateSuggestionBatch_(JSON.parse(process.argv[1]));\n"
        valid = {
            "requests": [
                {
                    "insertText": {
                        "text": "synthetic",
                        "location": {"index": 1, "tabId": "tab-1"},
                    }
                }
            ],
            "writeControl": {
                "writeMode": "SUGGEST",
                "requiredRevisionId": "revision-1",
            },
            "commentUpdateState": "ALL_SAVED",
        }
        completed = subprocess.run(
            ["node", "-e", harness, json.dumps(valid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        valid["writeControl"]["writeMode"] = "EDIT"
        completed = subprocess.run(
            ["node", "-e", harness, json.dumps(valid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("SUGGEST", completed.stderr)


if __name__ == "__main__":
    unittest.main()
