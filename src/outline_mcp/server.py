"""Server assembly and entrypoint.

Builds the FastMCP instance, registers tools, and runs the selected transport. The ``main()``
console-script is the single process entrypoint (``outline-mcp-server``).
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .auth import BearerAuthMiddleware, build_session_cache
from .client import OutlineClient
from .config import Settings, Transport, get_settings
from .tools import register_tools

logger = logging.getLogger("outline_mcp")

_INSTRUCTIONS = (
    "Search, read, create, update and comment on documents in an Outline knowledge base. "
    "Use search_documents to find content, get_document to read it, and the write tools to edit."
)


def build_mcp(settings: Settings, client: OutlineClient) -> FastMCP:
    """Create the FastMCP server with host-header protection and the configured tools."""
    security = (
        TransportSecuritySettings(
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        )
        if settings.allowed_hosts
        else None
    )
    mcp = FastMCP("Outline", instructions=_INSTRUCTIONS, transport_security=security)
    register_tools(mcp, client, settings)
    return mcp


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _run_http(mcp: FastMCP, settings: Settings) -> None:
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerAuthMiddleware,
        settings=settings,
        cache=build_session_cache(settings),
    )
    logger.info(
        "Outline MCP (http) on %s:%s strategy=%s readonly=%s",
        settings.mcp_host,
        settings.mcp_port,
        settings.auth_strategy().value,
        settings.outline_mcp_readonly,
    )
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, log_level=settings.log_level)


def main() -> None:
    settings = get_settings()
    _configure_logging(settings)
    client = OutlineClient(settings)
    mcp = build_mcp(settings, client)

    if settings.mcp_transport is Transport.stdio:
        logger.info("Outline MCP (stdio) readonly=%s", settings.outline_mcp_readonly)
        mcp.run()
    else:
        _run_http(mcp, settings)


if __name__ == "__main__":
    main()
