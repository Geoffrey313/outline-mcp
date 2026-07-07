"""Inbound authentication for the Streamable HTTP transport.

Implemented as a pure ASGI middleware (not Starlette's ``BaseHTTPMiddleware``) so the per-request
``request_token`` ContextVar is set in the *same* coroutine that runs the tool, guaranteeing
propagation. Behaviour by strategy is described in docs/design-spec.md §4.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .cache import TTLCache
from .client import request_token
from .config import AuthStrategy, Settings

_BEARER_PREFIX = "bearer "
_SESSION_HEADER = "mcp-session-id"


def build_session_cache(settings: Settings) -> TTLCache:
    return TTLCache(max_entries=settings.mcp_session_max, ttl_seconds=settings.mcp_session_ttl)


def _bearer(headers: Headers) -> str | None:
    value = headers.get("authorization")
    if value and value.lower().startswith(_BEARER_PREFIX):
        return value[len(_BEARER_PREFIX) :].strip() or None
    return None


async def _unauthorized(send: Send, reason: str) -> None:
    await JSONResponse({"error": "unauthorized", "detail": reason}, status_code=401)(
        {"type": "http"}, _empty_receive, send
    )


async def _empty_receive() -> dict[str, object]:  # pragma: no cover - Starlette contract
    return {"type": "http.request", "body": b"", "more_body": False}


class BearerAuthMiddleware:
    """Applies the configured inbound strategy and, for passthrough, primes ``request_token``."""

    def __init__(self, app: ASGIApp, *, settings: Settings, cache: TTLCache) -> None:
        self._app = app
        self._settings = settings
        self._cache = cache
        self._strategy = settings.auth_strategy()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        session_id = headers.get(_SESSION_HEADER)

        if self._strategy is AuthStrategy.gateway:
            if _bearer(headers) != self._settings.mcp_auth_token:
                await _unauthorized(send, "invalid gateway token")
                return
            await self._app(scope, receive, send)
            return

        if self._strategy is AuthStrategy.open:
            await self._app(scope, receive, send)
            return

        # Passthrough: forward the caller's own token. The session cache lets bridges that drop the
        # Authorization header on the session GET/SSE stream still resolve.
        bearer = _bearer(headers)
        token = bearer or (self._cache.get(session_id) if session_id else None)
        if not token:
            await _unauthorized(send, "missing bearer token")
            return

        # Later requests carry both header and session id — keep the cache warm.
        if bearer and session_id:
            self._cache.set(session_id, bearer)

        # On the bootstrap request the session id is assigned by the *server* (response header), so
        # wrap send to learn session_id -> token for the later header-less requests.
        outbound_send = send
        if bearer and not session_id:
            outbound_send = self._capture_session_id(send, bearer)

        reset = request_token.set(token)
        try:
            await self._app(scope, receive, outbound_send)
        finally:
            request_token.reset(reset)
            if scope.get("method") == "DELETE" and session_id:
                self._cache.delete(session_id)

    def _capture_session_id(self, send: Send, token: str) -> Send:
        """Wrap ``send`` to cache ``token`` under the session id the server assigns downstream."""
        cache = self._cache

        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start":
                for key, value in message.get("headers", []):
                    if key.decode("latin-1").lower() == _SESSION_HEADER:
                        cache.set(value.decode("latin-1"), token)
                        break
            await send(message)

        return wrapped
