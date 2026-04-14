# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, cast

import httpx
import redis.asyncio as redis_async
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..auth.pages import render_archidekt_authorize_page
from ..auth.provider import AUTH_SCOPE, RedisArchidektOAuthProvider
from ..config import RuntimeSettings
from ..integrations.authenticated import ArchidektAuthenticatedClient
from ..runtime_cli import configure_logging
from ..schemas.accounts import ArchidektAccount
from ..services.deckbuilding import DeckbuildingService, LOGGER
from ..server_contracts import SERVER_INSTRUCTIONS
from ..ui.home import render_home_page
from .http_helpers import _compact_optional_text
from .resources import register_resources
from .routes import register_http_routes
from .tools import register_mcp_tools


def create_server(runtime_settings: RuntimeSettings | None = None) -> FastMCP:
    runtime = runtime_settings or RuntimeSettings()
    service_state: dict[str, DeckbuildingService | None] = {"service": None}
    auth_redis_client = None
    auth_provider = None
    auth_settings = None

    if runtime.auth_enabled:
        if runtime.normalized_public_base_url is None:
            raise ValueError("`public_base_url` is required when MCP auth is enabled.")
        auth_redis_client = redis_async.from_url(runtime.redis_url, decode_responses=True)
        auth_provider = RedisArchidektOAuthProvider(
            auth_redis_client,
            key_prefix=runtime.redis_key_prefix,
            issuer_url=runtime.normalized_public_base_url,
            auth_code_ttl_seconds=runtime.auth_code_ttl_seconds,
            access_token_ttl_seconds=runtime.auth_access_token_ttl_seconds,
            refresh_token_ttl_seconds=runtime.auth_refresh_token_ttl_seconds,
        )
        auth_settings = AuthSettings(
            issuer_url=cast(AnyHttpUrl, runtime.normalized_public_base_url),
            resource_server_url=cast(
                AnyHttpUrl,
                f"{runtime.normalized_public_base_url}{runtime.streamable_http_path}",
            ),
            service_documentation_url=cast(AnyHttpUrl, runtime.normalized_public_base_url),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[AUTH_SCOPE],
                default_scopes=[AUTH_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[AUTH_SCOPE],
        )

    def build_service() -> DeckbuildingService:
        return DeckbuildingService(runtime)

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
            active_service = service_state.get("service")
            if active_service is not None:
                await active_service.aclose()
                service_state["service"] = None
            if auth_redis_client is not None:
                await auth_redis_client.aclose()
            LOGGER.info("Shutting down Archidekt Commander MCP server")

    server = FastMCP(
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
        auth=auth_settings,
        auth_server_provider=auth_provider,
    )

    async def get_service() -> DeckbuildingService:
        active_service = service_state["service"]
        if active_service is None:
            LOGGER.info("Creating DeckbuildingService")
            active_service = build_service()
            service_state["service"] = active_service
        return active_service

    @server.custom_route("/", methods=["GET"])
    async def homepage(_: Request) -> Response:
        return HTMLResponse(render_home_page(runtime))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "service": "archidekt-commander-mcp",
                "transport": runtime.transport,
                "mcp_path": runtime.streamable_http_path,
                "stateless_http": runtime.stateless_http,
                "cache_backend": "redis",
                "private_cache_backend": "redis+memory-fallback",
                "archidekt_rate_limit_max_requests": runtime.archidekt_rate_limit_max_requests,
                "archidekt_rate_limit_window_seconds": runtime.archidekt_rate_limit_window_seconds,
                "archidekt_retry_max_attempts": runtime.archidekt_retry_max_attempts,
                "archidekt_retry_base_delay_seconds": runtime.archidekt_retry_base_delay_seconds,
                "archidekt_exact_name_cache_ttl_seconds": runtime.archidekt_exact_name_cache_ttl_seconds,
                "personal_deck_cache_ttl_seconds": runtime.personal_deck_cache_ttl_seconds,
                "mcp_auth_enabled": runtime.auth_enabled,
                "oauth_session_backend": "redis" if runtime.auth_enabled else "disabled",
                "oauth_session_expiration": "never" if runtime.auth_enabled else "disabled",
                "oauth_access_token_ttl_seconds": runtime.auth_access_token_ttl_seconds,
                "oauth_refresh_token_ttl_seconds": runtime.auth_refresh_token_ttl_seconds,
                "oauth_access_token_persistent": runtime.auth_access_token_ttl_seconds is None,
                "oauth_refresh_token_persistent": runtime.auth_refresh_token_ttl_seconds is None,
            }
        )

    if auth_provider is not None:

        @server.custom_route("/auth/archidekt-login", methods=["GET", "POST"])
        async def auth_archidekt_login(request: Request) -> Response:
            if request.method == "GET":
                request_id = _compact_optional_text(request.query_params.get("request_id"))
                if request_id is None:
                    return HTMLResponse(
                        render_archidekt_authorize_page(
                            request_id="",
                            error_message="The MCP authorization request is missing a request id.",
                        ),
                        status_code=400,
                    )
                pending = await auth_provider.get_pending_request(request_id)
                if pending is None:
                    return HTMLResponse(
                        render_archidekt_authorize_page(
                            request_id=request_id,
                            error_message="This MCP authorization request is missing or has expired. Start the app connection again from ChatGPT.",
                        ),
                        status_code=400,
                    )
                return HTMLResponse(render_archidekt_authorize_page(request_id=request_id))

            form = await request.form()
            request_id = _compact_optional_text(form.get("request_id"))
            identifier = _compact_optional_text(form.get("identifier"))
            password = _compact_optional_text(form.get("password"))
            if request_id is None:
                return HTMLResponse(
                    render_archidekt_authorize_page(
                        request_id="",
                        error_message="The MCP authorization request is missing a request id.",
                    ),
                    status_code=400,
                )
            if not identifier or not password:
                return HTMLResponse(
                    render_archidekt_authorize_page(
                        request_id=request_id,
                        error_message="Archidekt username/email and password are both required.",
                    ),
                    status_code=400,
                )
            pending = await auth_provider.get_pending_request(request_id)
            if pending is None:
                return HTMLResponse(
                    render_archidekt_authorize_page(
                        request_id=request_id,
                        error_message="This MCP authorization request is missing or has expired. Start the app connection again from ChatGPT.",
                    ),
                    status_code=400,
                )

            login_account = (
                ArchidektAccount(email=identifier, password=password)
                if "@" in identifier
                else ArchidektAccount(username=identifier, password=password)
            )
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(runtime.http_timeout_seconds),
                    headers={"User-Agent": runtime.user_agent},
                ) as auth_http_client:
                    auth_client = ArchidektAuthenticatedClient(auth_http_client, runtime)
                    resolved_account = await auth_client.login(login_account)
                redirect_url = await auth_provider.complete_authorization(request_id, resolved_account)
            except Exception as error:
                return HTMLResponse(
                    render_archidekt_authorize_page(
                        request_id=request_id,
                        error_message=f"Archidekt login failed: {error}",
                    ),
                    status_code=400,
                )

            return RedirectResponse(redirect_url, status_code=302)

    register_http_routes(server, get_service, runtime)
    register_resources(server, get_service)
    register_mcp_tools(server, get_service, runtime)
    return server
