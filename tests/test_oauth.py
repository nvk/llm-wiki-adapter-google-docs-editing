from __future__ import annotations

import json
import io
import os
import stat
import tempfile
import time
import unittest
import urllib.parse
import urllib.error
from pathlib import Path
from unittest.mock import patch

from google_docs_adapter import oauth
from google_docs_adapter.storage import write_private_json


class OAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "oauth"
        self.download = Path(self.temporary.name) / "client-download.json"
        self.download.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "synthetic.apps.googleusercontent.com",
                        "client_secret": "synthetic-client-secret",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": oauth.TOKEN_ENDPOINT,
                        "redirect_uris": ["http://localhost"],
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure(self) -> dict:
        oauth.configure_client(self.download, self.root)
        return oauth.load_client(self.root)

    def token(self, *, expires_at: int, access_token: str = "access-old") -> dict:
        value = {
            "schema": oauth.TOKEN_SCHEMA,
            "access_token": access_token,
            "refresh_token": "refresh-secret",
            "expires_at": expires_at,
            "scope": [oauth.DRIVE_FILE_SCOPE],
            "token_type": "Bearer",
        }
        write_private_json(oauth.token_path(self.root), value)
        return value

    def test_configure_accepts_only_desktop_google_client(self) -> None:
        result = oauth.configure_client(self.download, self.root)
        self.assertTrue(result["configured"])
        stored = oauth.load_client(self.root)
        self.assertEqual(stored["scopes"], [oauth.DRIVE_FILE_SCOPE])
        mode = stat.S_IMODE(oauth.client_path(self.root).stat().st_mode)
        self.assertEqual(mode, 0o600)

        self.download.write_text(
            json.dumps(
                {
                    "web": {
                        "client_id": "synthetic.apps.googleusercontent.com",
                        "auth_uri": oauth.AUTHORIZATION_ENDPOINT,
                        "token_uri": oauth.TOKEN_ENDPOINT,
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(oauth.OAuthError, "Desktop app"):
            oauth.configure_client(self.download, self.root)

    def test_configure_rejects_non_google_token_endpoint(self) -> None:
        value = json.loads(self.download.read_text(encoding="utf-8"))
        value["installed"]["token_uri"] = "https://example.test/token"
        self.download.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(oauth.OAuthError, "unexpected Google endpoints"):
            oauth.configure_client(self.download, self.root)

    def test_authorization_request_uses_pkce_state_and_narrow_scope(self) -> None:
        client = self.configure()
        url, state, verifier = oauth.create_authorization_request(
            client, "http://127.0.0.1:54321"
        )
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            oauth.AUTHORIZATION_ENDPOINT,
        )
        self.assertEqual(query["scope"], [oauth.DRIVE_FILE_SCOPE])
        self.assertEqual(query["state"], [state])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotIn("synthetic-client-secret", url)

    def test_environment_access_token_remains_an_ephemeral_override(self) -> None:
        with patch.dict(
            os.environ, {oauth.ACCESS_TOKEN_ENV: " environment-token "}, clear=True
        ):
            self.assertEqual(oauth.get_access_token(self.root), "environment-token")

    def test_fresh_stored_token_does_not_refresh(self) -> None:
        self.token(expires_at=int(time.time()) + 3600)
        with patch.object(oauth, "_post_form") as post:
            self.assertEqual(oauth.get_access_token(self.root), "access-old")
        post.assert_not_called()

    def test_expired_token_refreshes_and_preserves_refresh_token(self) -> None:
        self.configure()
        self.token(expires_at=int(time.time()) - 10)
        response = {
            "access_token": "access-new",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        with patch.object(oauth, "_post_form", return_value=response) as post:
            result = oauth.get_access_token(self.root)
        self.assertEqual(result, "access-new")
        fields = post.call_args.args[1]
        self.assertEqual(fields["refresh_token"], "refresh-secret")
        self.assertEqual(fields["grant_type"], "refresh_token")
        stored = oauth.load_token(self.root)
        self.assertEqual(stored["refresh_token"], "refresh-secret")
        self.assertEqual(stored["access_token"], "access-new")
        self.assertEqual(
            stat.S_IMODE(oauth.token_path(self.root).stat().st_mode), 0o600
        )

    def test_token_file_with_broad_permissions_is_rejected(self) -> None:
        self.token(expires_at=int(time.time()) + 3600)
        oauth.token_path(self.root).chmod(0o644)
        with self.assertRaisesRegex(oauth.OAuthError, "permissions"):
            oauth.load_token(self.root)

    def test_oauth_http_errors_do_not_echo_provider_or_token_details(self) -> None:
        error = urllib.error.HTTPError(
            oauth.TOKEN_ENDPOINT,
            400,
            "bad request",
            {},
            io.BytesIO(
                b'{"error":"invalid_grant","error_description":"refresh-secret"}'
            ),
        )
        with patch.object(oauth.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(oauth.OAuthError) as raised:
                oauth._post_form(
                    oauth.TOKEN_ENDPOINT,
                    {"refresh_token": "refresh-secret"},
                )
        self.assertIn("invalid_grant", str(raised.exception))
        self.assertNotIn("refresh-secret", str(raised.exception))

    def test_status_and_disconnect_never_return_token_values(self) -> None:
        self.configure()
        self.token(expires_at=int(time.time()) + 3600)
        status = oauth.oauth_status(self.root)
        self.assertTrue(status["configured"])
        self.assertTrue(status["connected"])
        self.assertNotIn("access_token", status)
        self.assertNotIn("refresh_token", status)
        self.assertTrue(oauth.disconnect(self.root))
        self.assertFalse(oauth.oauth_status(self.root)["connected"])


if __name__ == "__main__":
    unittest.main()
