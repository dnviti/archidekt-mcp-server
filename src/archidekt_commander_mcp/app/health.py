from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from ..config import RuntimeSettings
from ..ui.home import render_home_page, ui_asset_response


def health_payload(runtime: RuntimeSettings) -> dict[str, object]:
    return {
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
        "oauth_archidekt_login_renewal_enabled": runtime.auth_persist_login_credentials,
    }


def register_home_and_health_routes(server: FastMCP, runtime: RuntimeSettings) -> None:
    @server.custom_route("/", methods=["GET"])
    async def homepage(_: Request) -> Response:
        return HTMLResponse(render_home_page(runtime))

    @server.custom_route("/favicon.ico", methods=["GET"])
    async def favicon(_: Request) -> Response:
        return ui_asset_response("favicon.ico")

    @server.custom_route("/assets/{asset_name}", methods=["GET"])
    async def ui_asset(request: Request) -> Response:
        return ui_asset_response(str(request.path_params["asset_name"]))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse(health_payload(runtime))
