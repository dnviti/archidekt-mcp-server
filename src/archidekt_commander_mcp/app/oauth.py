from __future__ import annotations

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
from ..schemas.accounts import ArchidektAccount
from .http_helpers import _compact_optional_text


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

    redis_client = redis_async.from_url(runtime.redis_url, decode_responses=True)
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
                return HTMLResponse(
                    render_archidekt_authorize_page(
                        request_id="",
                        error_message="The MCP authorization request is missing a request id.",
                        persist_login_credentials=runtime.auth_persist_login_credentials,
                    ),
                    status_code=400,
                )
            pending = await auth_provider.get_pending_request(request_id)
            if pending is None:
                return HTMLResponse(
                    render_archidekt_authorize_page(
                        request_id=request_id,
                        error_message="This MCP authorization request is missing or has expired. Start the app connection again from ChatGPT.",
                        persist_login_credentials=runtime.auth_persist_login_credentials,
                    ),
                    status_code=400,
                )
            return HTMLResponse(
                render_archidekt_authorize_page(
                    request_id=request_id,
                    persist_login_credentials=runtime.auth_persist_login_credentials,
                )
            )

        form = await request.form()
        request_id = _compact_optional_text(form.get("request_id"))
        identifier = _compact_optional_text(form.get("identifier"))
        password = _compact_optional_text(form.get("password"))
        if request_id is None:
            return HTMLResponse(
                render_archidekt_authorize_page(
                    request_id="",
                    error_message="The MCP authorization request is missing a request id.",
                    persist_login_credentials=runtime.auth_persist_login_credentials,
                ),
                status_code=400,
            )
        if not identifier or not password:
            return HTMLResponse(
                render_archidekt_authorize_page(
                    request_id=request_id,
                    error_message="Archidekt username/email and password are both required.",
                    persist_login_credentials=runtime.auth_persist_login_credentials,
                ),
                status_code=400,
            )
        pending = await auth_provider.get_pending_request(request_id)
        if pending is None:
            return HTMLResponse(
                render_archidekt_authorize_page(
                    request_id=request_id,
                    error_message="This MCP authorization request is missing or has expired. Start the app connection again from ChatGPT.",
                    persist_login_credentials=runtime.auth_persist_login_credentials,
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
            redirect_url = await auth_provider.complete_authorization(
                request_id,
                resolved_account,
                login_account=login_account if runtime.auth_persist_login_credentials else None,
            )
        except Exception as error:
            return HTMLResponse(
                render_archidekt_authorize_page(
                    request_id=request_id,
                    error_message=f"Archidekt login failed: {error}",
                    persist_login_credentials=runtime.auth_persist_login_credentials,
                ),
                status_code=400,
            )

        return RedirectResponse(redirect_url, status_code=302)
