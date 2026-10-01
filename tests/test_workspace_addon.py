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
            manifest["oauthScopes"],
            ["https://www.googleapis.com/auth/drive.file"],
        )
        self.assertEqual(
            manifest["addOns"]["docs"]["onFileScopeGrantedTrigger"]["runFunction"],
            "onFileScopeGranted",
        )
        self.assertNotIn("urlFetchWhitelist", manifest)
        self.assertNotIn("openLinkUrlPrefixes", manifest["addOns"]["common"])

    def test_addon_only_grants_current_file(self) -> None:
        source = (ADDON / "Code.gs").read_text(encoding="utf-8")
        self.assertIn("requestFileScopeForActiveDocument", source)
        self.assertNotIn("UrlFetchApp", source)
        self.assertNotIn("DocumentApp", source)
        self.assertNotIn("ScriptApp.getOAuthToken", source)

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


if __name__ == "__main__":
    unittest.main()
