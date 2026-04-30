# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, cast

from mcp.server.fastmcp import FastMCP

from ..config import RuntimeSettings
from ..runtime_cli import configure_logging
from ..services.deckbuilding import LOGGER
from ..server_contracts import SERVER_INSTRUCTIONS
from .health import register_home_and_health_routes
from .oauth import build_auth_components, register_archidekt_auth_routes
from .proxy import ProxyHeaderFastMCP
from .resources import register_resources
from .routes import register_http_routes
from .service_provider import DeckbuildingServiceProvider
from .tools import register_mcp_tools


def create_server(runtime_settings: RuntimeSettings | None = None) -> FastMCP:
    runtime = runtime_settings or RuntimeSettings()
    auth_components = build_auth_components(runtime)
    service_provider = DeckbuildingServiceProvider(runtime, LOGGER)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        normalized_level = configure_logging(runtime.log_level)
        LOGGER.info(
            "Starting Archidekt Commander MCP server transport=%s host=%s port=%s mcp_path=%s log_level=%s",
            runtime.transport,
            runtime.host,
            runtime.port,
            runtime.streamable_http_path,
            normalized_level,
        )
        try:
            yield
        finally:
            await service_provider.close()
            await auth_components.close()
            LOGGER.info("Shutting down Archidekt Commander MCP server")

    server = ProxyHeaderFastMCP(
        name="archidekt-commander",
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://archidekt.com",
        dependencies=["httpx", "mcp", "pydantic", "redis", "starlette", "uvicorn"],
        log_level=cast(
            Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            runtime.log_level,
        ),
        host=runtime.host,
        port=runtime.port,
        streamable_http_path=runtime.streamable_http_path,
        stateless_http=runtime.stateless_http,
        lifespan=lifespan,
        auth=auth_components.settings,
        auth_server_provider=auth_components.provider,
        forwarded_allow_ips=runtime.forwarded_allow_ips,
    )

    register_home_and_health_routes(server, runtime)
    register_archidekt_auth_routes(server, runtime, auth_components.provider)
    register_http_routes(server, service_provider.get, runtime)
    register_resources(server, service_provider.get)
    register_mcp_tools(server, service_provider.get, runtime)
    return server
