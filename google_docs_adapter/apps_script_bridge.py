from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .oauth import get_access_token, load_bridge
from .storage import canonical_json_bytes

APPS_SCRIPT_API_BASE = "https://script.googleapis.com/v1/scripts"
GET_DOCUMENT_FUNCTION = "llmWikiBridgeGetDocument"
BATCH_UPDATE_FUNCTION = "llmWikiBridgeBatchUpdate"
MAX_RESPONSE_BYTES = 24 * 1024 * 1024


class GoogleDocsAppsScriptClient:
    """Docs API client tunneled through the authorized Workspace add-on.

    The local OAuth token authorizes ``scripts.run``. The Apps Script function
    obtains its own short-lived token and calls the Docs API inside Google's
    infrastructure, so the per-file grant never has to cross OAuth clients.
    """

    def __init__(
        self,
        access_token: str,
        deployment_id: str,
        timeout_seconds: float = 50.0,
    ) -> None:
        if not access_token.strip():
            raise ValueError("Google Apps Script access token is empty")
        if not deployment_id.strip():
            raise ValueError("Apps Script deployment ID is empty")
        self._access_token = access_token.strip()
        self._deployment_id = deployment_id.strip()
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "GoogleDocsAppsScriptClient":
        bridge = load_bridge()
        return cls(get_access_token(), bridge["deployment_id"])

    def _run(self, function: str, parameters: list[Any]) -> dict[str, Any]:
        deployment_id = urllib.parse.quote(self._deployment_id, safe="")
        url = f"{APPS_SCRIPT_API_BASE}/{deployment_id}:run"
        request = urllib.request.Request(
            url,
            data=canonical_json_bytes(
                {
                    "function": function,
                    "parameters": parameters,
                    "devMode": False,
                }
            ),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = ""
            try:
                value = json.loads(exc.read().decode("utf-8", errors="replace"))
                error = value.get("error") if isinstance(value, dict) else None
                if isinstance(error, dict) and isinstance(error.get("status"), str):
                    status = f" ({error['status']})"
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            raise RuntimeError(
                f"Apps Script API run failed with HTTP {exc.code}{status}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Apps Script API run transport failed") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("Apps Script API response exceeded the local size limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Apps Script API returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("done") is not True:
            raise RuntimeError("Apps Script API returned an incomplete execution")
        if "error" in value:
            error = value.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            suffix = f" ({code})" if isinstance(code, int) else ""
            raise RuntimeError(f"Apps Script bridge function failed{suffix}")
        response = value.get("response")
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError("Apps Script bridge returned a non-object result")
        return result

    def get_document(self, document_id: str) -> dict[str, Any]:
        return self._run(GET_DOCUMENT_FUNCTION, [document_id])

    def batch_update(
        self, document_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._run(BATCH_UPDATE_FUNCTION, [document_id, body])
