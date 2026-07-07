"""Centralized configuration.

Every tunable value the server uses lives here as a typed, env-overridable field — there are no
hardcoded values or magic numbers elsewhere in the codebase. Loaded once via ``get_settings()``.

The auth model separates two concerns (see docs/design-spec.md §4):

* **inbound**  — how an MCP client proves it may use this server;
* **upstream** — which Outline API token we send to ``/api/*``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated env value into a trimmed, non-empty list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Transport(StrEnum):
    """Selected MCP transport."""

    stdio = "stdio"
    http = "http"


class AuthStrategy(StrEnum):
    """Resolved inbound authentication strategy (which also fixes the upstream token source)."""

    stdio = "stdio"  # local process; upstream = env token
    passthrough = "passthrough"  # per-user; upstream = caller's forwarded token
    gateway = "gateway"  # shared secret gate; upstream = env token
    open = "open"  # no inbound auth; upstream = env token


class Settings(BaseSettings):
    """Process configuration, populated from the environment (and an optional ``.env``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Upstream Outline API -------------------------------------------------
    outline_api_url: str = "https://your-outline.example.com/api"
    outline_api_token: str | None = None

    # --- Transport ------------------------------------------------------------
    mcp_transport: Transport = Transport.stdio
    mcp_host: str = "0.0.0.0"  # bind address for http mode (container behind a proxy)
    mcp_port: int = 9000

    # --- Inbound auth strategies (http mode; exactly one must be selected) -----
    mcp_allow_outline_token_auth: bool = False  # Passthrough
    mcp_auth_token: str | None = None  # Gateway shared secret
    mcp_allow_unauthenticated: bool = False  # Open

    # --- Passthrough session-token cache --------------------------------------
    mcp_session_ttl: int = 1800  # seconds of inactivity before eviction
    mcp_session_max: int = 10000  # max cached sessions (LRU)

    # --- Transport security ---------------------------------------------------
    # Comma-separated public host(s) for DNS-rebinding protection. Required in http mode.
    mcp_allowed_hosts: str | None = None
    # Comma-separated allowed Origins. When unset, derived as http(s)://<each allowed host>.
    mcp_allowed_origins: str | None = None

    # --- Behaviour ------------------------------------------------------------
    outline_mcp_readonly: bool = False  # drop all write tools when true
    default_limit: int = 25  # applied when a list/search tool omits `limit`
    max_limit: int = 100  # hard cap for any list/search `limit`
    request_timeout_seconds: float = 30.0
    log_level: str = "info"

    # --- Validators / derived -------------------------------------------------
    @field_validator("mcp_transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: object) -> object:
        """Accept the ``streamable-http`` aliases as ``http``."""
        if isinstance(value, str):
            token = value.strip().lower().replace("_", "-")
            if token in {"http", "streamable-http", "streamable"}:
                return Transport.http.value
            if token == "stdio":
                return Transport.stdio.value
        return value

    @field_validator("outline_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def allowed_hosts(self) -> list[str]:
        """Parsed ``mcp_allowed_hosts`` (empty list when unset)."""
        return _split_csv(self.mcp_allowed_hosts)

    @property
    def allowed_origins(self) -> list[str]:
        """Explicit ``mcp_allowed_origins``, or http(s)://<host> derived from the allowed hosts.

        Left empty, ``TransportSecuritySettings`` rejects every request that carries an ``Origin``
        header, which breaks browser-like and hosted MCP clients that send one.
        """
        explicit = _split_csv(self.mcp_allowed_origins)
        if explicit:
            return explicit
        origins: list[str] = []
        for host in self.allowed_hosts:
            origins.extend((f"https://{host}", f"http://{host}"))
        return origins

    def auth_strategy(self) -> AuthStrategy:
        """The single resolved inbound strategy. Validated at construction time."""
        if self.mcp_transport is Transport.stdio:
            return AuthStrategy.stdio
        return self._enabled_http_strategies()[0]

    def _enabled_http_strategies(self) -> list[AuthStrategy]:
        enabled: list[AuthStrategy] = []
        if self.mcp_allow_outline_token_auth:
            enabled.append(AuthStrategy.passthrough)
        if self.mcp_auth_token:
            enabled.append(AuthStrategy.gateway)
        if self.mcp_allow_unauthenticated:
            enabled.append(AuthStrategy.open)
        return enabled

    @model_validator(mode="after")
    def _fail_fast(self) -> Settings:
        """Reject ambiguous or unusable configurations at startup (never mid-request)."""
        if self.mcp_transport is Transport.stdio:
            if not self.outline_api_token:
                raise ValueError("stdio transport requires OUTLINE_API_TOKEN to be set")
            return self

        enabled = self._enabled_http_strategies()
        if len(enabled) != 1:
            raise ValueError(
                "http mode requires exactly one inbound strategy; set exactly one of "
                "MCP_ALLOW_OUTLINE_TOKEN_AUTH (passthrough), MCP_AUTH_TOKEN (gateway), "
                f"MCP_ALLOW_UNAUTHENTICATED (open). Currently enabled: {len(enabled)}."
            )

        strategy = enabled[0]
        if strategy in (AuthStrategy.gateway, AuthStrategy.open) and not self.outline_api_token:
            raise ValueError(f"{strategy.value} strategy requires OUTLINE_API_TOKEN to be set")

        if not self.allowed_hosts:
            raise ValueError(
                "http mode requires MCP_ALLOWED_HOSTS set to the explicit public host(s), "
                "e.g. 'outline-mcp.example.com'"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
