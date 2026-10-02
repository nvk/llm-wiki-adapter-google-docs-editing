from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "install_local", ROOT / "scripts" / "install_local.py"
)
assert SPEC is not None and SPEC.loader is not None
install_local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_local)


class InstallLocalTests(unittest.TestCase):
    def test_api_only_registration_is_default(self) -> None:
        command = install_local.registration_command(
            Path("/tmp/llm-wiki"),
            Path("/tmp/adapter"),
            Path("/tmp/data"),
        )
        self.assertIn("google-docs-api:authorized-files", command)
        self.assertNotIn("browser-collaboration:active-tab", command)
        self.assertIn("LLM_WIKI_GOOGLE_DOCS_OAUTH_DIR", command)
        self.assertEqual(command.count("--read-root"), 2)

if __name__ == "__main__":
    unittest.main()
