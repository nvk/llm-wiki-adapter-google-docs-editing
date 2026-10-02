from __future__ import annotations

import contextlib
import importlib.util
import io
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "google_docs_auth", ROOT / "scripts" / "google_docs_auth.py"
)
assert SPEC is not None and SPEC.loader is not None
google_docs_auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(google_docs_auth)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _FakeServer:
    server_port = 43210

    def __init__(self, *_args, **_kwargs) -> None:
        self.result = None
        self.expected_state = ""
        self.authorization_url = ""
        self.timeout = 0.0

    def handle_request(self) -> None:
        self.result = {
            "state": self.expected_state,
            "code": "synthetic-code",
            "error": "",
            "picked_file_ids": "SYNTHETIC_DOCUMENT_12345",
        }

    def server_close(self) -> None:
        return


class GoogleDocsAuthCliTests(unittest.TestCase):
    def test_manual_flow_prints_only_a_short_loopback_link(self) -> None:
        provider_url = "https://accounts.google.com/o/oauth2/v2/auth?very=long"
        output = io.StringIO()
        with (
            patch.object(google_docs_auth, "OAuthCallbackServer", _FakeServer),
            patch.object(google_docs_auth, "load_client", return_value={}),
            patch.object(
                google_docs_auth,
                "create_authorization_request",
                return_value=(provider_url, "synthetic-state", "synthetic-verifier"),
            ),
            patch.object(
                google_docs_auth,
                "exchange_authorization_code",
                return_value={"synthetic": True},
            ),
            patch.object(google_docs_auth, "store_authorization_response"),
            contextlib.redirect_stdout(output),
        ):
            result = google_docs_auth._authorize(
                Path("/tmp/synthetic-oauth"),
                no_browser=True,
                timeout=30,
                document_id="SYNTHETIC_DOCUMENT_12345",
            )

        self.assertEqual(output.getvalue(), "http://127.0.0.1:43210/start\n")
        self.assertNotIn("accounts.google.com", output.getvalue())
        self.assertEqual(result["selected_file_count"], 1)

    def test_short_start_link_redirects_without_exposing_it_in_the_page(self) -> None:
        server = google_docs_auth.OAuthCallbackServer(
            ("127.0.0.1", 0), google_docs_auth.OAuthCallbackHandler
        )
        server.expected_state = "synthetic-state"
        server.result = None
        server.authorization_url = (
            "https://accounts.google.com/o/oauth2/v2/auth?synthetic=value"
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                opener.open(
                    f"http://127.0.0.1:{server.server_port}/start", timeout=2
                )
            self.assertEqual(raised.exception.code, 302)
            self.assertEqual(
                raised.exception.headers["Location"], server.authorization_url
            )
            self.assertEqual(raised.exception.headers["Cache-Control"], "no-store")
            self.assertEqual(raised.exception.headers["Referrer-Policy"], "no-referrer")
            self.assertIsNone(server.result)
        finally:
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
