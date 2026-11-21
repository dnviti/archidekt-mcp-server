from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

import httpx
import redis.asyncio as redis_async
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..auth.pages import render_archidekt_authorize_page
from ..auth.provider import AUTH_SCOPE, RedisArchidektOAuthProvider
from ..config import RuntimeSettings
from ..integrations.authenticated import ArchidektAuthenticatedClient
from ..integrations.redis_client import create_redis_client
from ..schemas.accounts import ArchidektAccount
from .http_helpers import _compact_optional_text

LOGGER = logging.getLogger(__name__)

# The login page collects an Archidekt password: never cache it, never let another
# site frame it, and never leak the request id through the Referer header.
_LOGIN_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _login_page_response(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=_LOGIN_PAGE_HEADERS)


def _login_failure_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in {400, 401, 403}:
            return "Archidekt rejected the username/email or password."
        return f"Archidekt login failed with HTTP {error.response.status_code}. Try again in a moment."
    if isinstance(error, httpx.HTTPError):
        return "Archidekt could not be reached. Try again in a moment."
    if isinstance(error, RuntimeError):
        return str(error)
    return "The login could not be completed because of a server error. Try again in a moment."


@dataclass(slots=True)
class ServerAuthComponents:
    redis_client: redis_async.Redis | None = None
    provider: RedisArchidektOAuthProvider | None = None
    settings: AuthSettings | None = None

    async def close(self) -> None:
        if self.redis_client is not None:
            await self.redis_client.aclose()


def build_auth_components(runtime: RuntimeSettings) -> ServerAuthComponents:
    if not runtime.auth_enabled:
        return ServerAuthComponents()

    if runtime.normalized_public_base_url is None:
        raise ValueError("`public_base_url` is required when MCP auth is enabled.")

    redis_client = create_redis_client(runtime.redis_url)
    provider = RedisArchidektOAuthProvider(
        redis_client,
        key_prefix=runtime.redis_key_prefix,
        issuer_url=runtime.normalized_public_base_url,
        auth_code_ttl_seconds=runtime.auth_code_ttl_seconds,
        access_token_ttl_seconds=runtime.auth_access_token_ttl_seconds,
        refresh_token_ttl_seconds=runtime.auth_refresh_token_ttl_seconds,
    )
    settings = AuthSettings(
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
        # Tokens are only minted by this server for this single MCP resource.
        validate_token_resource=False,
    )
    return ServerAuthComponents(
        redis_client=redis_client,
        provider=provider,
        settings=settings,
    )


def register_archidekt_auth_routes(
    server: FastMCP,
    runtime: RuntimeSettings,
    auth_provider: RedisArchidektOAuthProvider | None,
) -> None:
    if auth_provider is None:
        return

    @server.custom_route("/auth/archidekt-login", methods=["GET", "POST"])
    async def auth_archidekt_login(request: Request) -> Response:
        if request.method == "GET":
            request_id = _compact_optional_text(request.query_params.get("request_id"))
            if request_id is None:
                return _login_page_response(render_archidekt_authorize_page(
                    request_id="",
                    error_message="The MCP authorization request is missing a request id.",
                    persist_login_credentials=runtime.auth_persist_login_credentials,
                ),
                status_code=400,)
            pending = await auth_provider.get_pending_request(request_id)
            if pending is None:
                return _login_page_response(render_archidekt_authorize_page(
                    request_id=request_id,
                    error_message="This MCP authorization request is missing or has expired. Start the app connection again from ChatGPT.",
                    persist_login_credentials=runtime.auth_persist_login_credentials,
                ),
                status_code=400,)
            return _login_page_response(render_archidekt_authorize_page(
                request_id=request_id,
                persist_login_credentials=runtime.auth_persist_login_credentials,
            ))

        form = await request.form()
        request_id = _compact_optional_text(form.get("request_id"))
        identifier = _compact_optional_text(form.get("identifier"))
        raw_password = form.get("password")
        password = raw_password if isinstance(raw_password, str) and raw_password.strip() else None
        if request_id is None:
            return _login_page_response(render_archidekt_authorize_page(
                request_id="",
                error_message="The MCP authorization request is missing a request id.",
                persist_login_credentials=runtime.auth_persist_login_credentials,
            ),
            status_code=400,)
        if not identifier or not password:
            return _login_page_response(render_archidekt_authorize_page(
                request_id=request_id,
                error_message="Archidekt username/email and password are both required.",
                persist_login_credentials=runtime.auth_persist_login_credentials,
            ),
            status_code=400,)
        pending = await auth_provider.get_pending_request(request_id)
        if pending is None:
            return _login_page_response(render_archidekt_authorize_page(
                request_id=request_id,
                error_message="This MCP authorization request is missing or has expired. Start the app connection again from ChatGPT.",
                persist_login_credentials=runtime.auth_persist_login_credentials,
            ),
            status_code=400,)

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
            redirect_url = await auth_provider.complete_authorization(
                request_id,
                resolved_account,
                login_account=login_account if runtime.auth_persist_login_credentials else None,
            )
        except Exception as error:
            if not isinstance(error, (httpx.HTTPError, RuntimeError)):
                LOGGER.exception("Archidekt OAuth login failed")
            return _login_page_response(render_archidekt_authorize_page(
                request_id=request_id,
                error_message=f"Archidekt login failed: {_login_failure_message(error)}",
                persist_login_credentials=runtime.auth_persist_login_credentials,
            ),
            status_code=400,)

        return RedirectResponse(redirect_url, status_code=302)
