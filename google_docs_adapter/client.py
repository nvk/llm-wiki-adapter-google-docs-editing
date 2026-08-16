from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .auth import TokenProvider


class GoogleDocsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class GoogleDocsClient:
    def __init__(self, token_provider: TokenProvider | None = None, api_base: str | None = None) -> None:
        self.token_provider = token_provider or TokenProvider.from_environment()
        self.api_base = (api_base or os.environ.get("LLM_WIKI_GOOGLE_DOCS_API_BASE") or "https://docs.googleapis.com/v1").rstrip("/")
        parsed = urllib.parse.urlparse(self.api_base)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise RuntimeError("Google Docs API base must use HTTPS except for loopback tests")

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token_provider.access_token()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise GoogleDocsError(f"Google Docs API returned HTTP {exc.code}", exc.code, payload) from exc
        except Exception as exc:
            raise GoogleDocsError(f"Google Docs API request failed: {exc}") from exc
        try:
            value = json.loads(payload.decode("utf-8")) if payload else {}
        except json.JSONDecodeError as exc:
            raise GoogleDocsError("Google Docs API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise GoogleDocsError("Google Docs API response must be an object")
        return value

    def get_document(
        self,
        document_id: str,
        suggestions_view_mode: str,
        comments_view_mode: str | None = None,
    ) -> dict[str, Any]:
        query = {
            "includeTabsContent": "true",
            "suggestionsViewMode": suggestions_view_mode,
        }
        if comments_view_mode is not None:
            query["commentsViewMode"] = comments_view_mode
        return self.request(
            "GET",
            f"/documents/{urllib.parse.quote(document_id, safe='')}",
            query=query,
        )

    def batch_update(self, document_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/documents/{urllib.parse.quote(document_id, safe='')}:batchUpdate",
            body=body,
        )
