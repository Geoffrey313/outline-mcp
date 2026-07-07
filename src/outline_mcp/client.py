"""Outline REST client and response shaping.

A single thin wrapper over Outline's ``POST /api/<resource>.<action>`` API. All HTTP, token
resolution, error mapping and envelope handling live here so the tool layer stays declarative and
free of duplicated request/compaction boilerplate.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import httpx

from .config import AuthStrategy, Settings

# Per-request upstream token, populated by the auth middleware in passthrough mode.
request_token: ContextVar[str | None] = ContextVar("request_token", default=None)

# Fields kept when compacting each resource — declared once, reused everywhere.
_DOCUMENT_FIELDS = (
    "id",
    "title",
    "url",
    "collectionId",
    "parentDocumentId",
    "createdAt",
    "updatedAt",
    "publishedAt",
    "archivedAt",
)
_COLLECTION_FIELDS = ("id", "name", "description", "index", "permission", "createdAt", "updatedAt")
_COMMENT_FIELDS = (
    "id",
    "documentId",
    "parentCommentId",
    "createdById",
    "createdAt",
    "updatedAt",
    "resolvedAt",
)


class OutlineAPIError(RuntimeError):
    """Raised when Outline rejects a request or is unreachable; surfaced as an MCP tool error."""


def _pick(source: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Return only ``fields`` that are present and non-null on a dict-like payload."""
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in fields if source.get(key) is not None}


def _prune(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so optional args are simply absent from the request body."""
    return {key: value for key, value in payload.items() if value is not None}


def compact_document(document: Any) -> dict[str, Any]:
    return _pick(document, _DOCUMENT_FIELDS)


def compact_document_with_text(document: Any) -> dict[str, Any]:
    shaped = compact_document(document)
    if isinstance(document, dict) and document.get("text") is not None:
        shaped["text"] = document["text"]
    return shaped


def compact_collection(collection: Any) -> dict[str, Any]:
    return _pick(collection, _COLLECTION_FIELDS)


def compact_comment(comment: Any) -> dict[str, Any]:
    shaped = _pick(comment, _COMMENT_FIELDS)
    if isinstance(comment, dict):
        for key in ("text", "data", "anchorText"):
            if comment.get(key) is not None:
                shaped[key] = comment[key]
    return shaped


def compact_search_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    shaped: dict[str, Any] = {}
    for key in ("ranking", "context"):
        if result.get(key) is not None:
            shaped[key] = result[key]
    shaped["document"] = compact_document(result.get("document"))
    return shaped


def compact_auth_info(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    user = _pick(data.get("user"), ("id", "name", "email", "role"))
    team = _pick(data.get("team"), ("id", "name", "url"))
    return {"user": user, "team": team}


class OutlineClient:
    """Async client that posts to the Outline API with the correctly resolved token."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.outline_api_url,
            timeout=settings.request_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _resolve_token(self) -> str:
        if self._settings.auth_strategy() is AuthStrategy.passthrough:
            token = request_token.get()
            if not token:
                raise OutlineAPIError("No Authorization bearer token found in the request")
            return token
        token = self._settings.outline_api_token
        if not token:
            raise OutlineAPIError("OUTLINE_API_TOKEN is not configured")
        return token

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = self._resolve_token()
        try:
            response = await self._http.post(
                f"/{endpoint}",
                json=_prune(payload),
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.RequestError as exc:
            raise OutlineAPIError(f"Could not reach Outline: {exc}") from exc
        if response.status_code >= 400:
            raise OutlineAPIError(_error_message(endpoint, response))
        return response.json()

    def _clamp_limit(self, limit: int | None) -> int:
        return min(limit or self._settings.default_limit, self._settings.max_limit)

    async def query_list(
        self,
        endpoint: str,
        payload: dict[str, Any],
        compactor: Any,
    ) -> dict[str, Any]:
        """Run a list/search endpoint, returning compacted items plus pagination context."""
        limit = self._clamp_limit(payload.get("limit"))
        offset = payload.get("offset") or 0
        raw = await self._post(endpoint, {**payload, "limit": limit, "offset": offset})
        items = [compactor(item) for item in raw.get("data", [])]
        total = (raw.get("pagination") or {}).get("total")
        return {
            "items": items,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "total": total,
            "truncated": total is not None and offset + len(items) < total,
        }

    async def query_one(
        self,
        endpoint: str,
        payload: dict[str, Any],
        compactor: Any,
    ) -> dict[str, Any]:
        """Run a single-object endpoint, returning the compacted object."""
        raw = await self._post(endpoint, payload)
        return compactor(raw.get("data"))


def _error_message(endpoint: str, response: httpx.Response) -> str:
    """Build a concise, non-leaking error string from an Outline error response."""
    detail = ""
    try:
        body = response.json()
        detail = body.get("message") or body.get("error") or ""
    except ValueError:
        detail = response.text[:200]
    suffix = f": {detail}" if detail else ""
    return f"Outline API {endpoint} failed ({response.status_code}){suffix}"
