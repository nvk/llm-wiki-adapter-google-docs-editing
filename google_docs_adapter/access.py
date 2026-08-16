from __future__ import annotations

from pathlib import Path
from typing import Any

from .auth import TokenProvider
from .client import GoogleDocsClient, GoogleDocsError


def exact_document_authorization_status(
    document_id: str,
    token_path: Path,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return a content-free live provider-access decision for one document."""
    token_path = token_path.expanduser().resolve(strict=False)
    if not token_path.is_file():
        return {
            "authorized": False,
            "picker_required": True,
            "reason": "oauth-token-missing",
        }
    try:
        provider = TokenProvider.from_path(token_path)
        docs = client or GoogleDocsClient(provider)
        docs.get_document(document_id, "SUGGESTIONS_INLINE")
    except GoogleDocsError as exc:
        if exc.status in {401, 403, 404}:
            return {
                "authorized": False,
                "picker_required": True,
                "reason": "exact-document-provider-access-missing",
            }
        raise
    except RuntimeError:
        return {
            "authorized": False,
            "picker_required": True,
            "reason": "oauth-token-unusable",
        }
    return {
        "authorized": True,
        "picker_required": False,
        "reason": "live-docs-api-access-confirmed",
    }
