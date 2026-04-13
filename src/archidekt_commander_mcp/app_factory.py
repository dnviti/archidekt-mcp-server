from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, TypeVar

import httpx
import redis.asyncio as redis_async
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .clients import ArchidektAuthenticatedClient
from .config import RuntimeSettings
from .mcp_auth import AUTH_SCOPE, RedisArchidektOAuthProvider, render_archidekt_authorize_page
from .models import (
    ArchidektAccount,
    ArchidektCardSearchFilters,
    ArchidektCardSearchRequest,
    ArchidektLoginRequest,
    CardSearchFilters,
    CollectionCardUpsert,
    CollectionLocator,
    CollectionOverviewRequest,
    CollectionSearchRequest,
    CollectionUpsertRequest,
    PersonalDeckCardMutation,
    PersonalDeckCardsMutationRequest,
    PersonalDeckCardsRequest,
    PersonalDeckCreateInput,
    PersonalDeckCreateRequest,
    PersonalDeckDeleteRequest,
    PersonalDecksRequest,
    PersonalDeckUpdateInput,
    PersonalDeckUpdateRequest,
)
from .server_contracts import (
    DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
    NON_DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
    READ_ONLY_TOOL_ANNOTATIONS,
    SERVER_INSTRUCTIONS,
    SESSION_TOOL_ANNOTATIONS,
)
from .webui import render_home_page


ModelT = TypeVar("ModelT", bound=BaseModel)


def create_server(runtime_settings: RuntimeSettings | None = None) -> FastMCP:
    from .server import (
        DeckbuildingService,
        LOGGER,
        configure_logging,
        describe_account,
        describe_collection_locator,
    )

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
        )
        auth_settings = AuthSettings(
            issuer_url=runtime.normalized_public_base_url,
            resource_server_url=f"{runtime.normalized_public_base_url}{runtime.streamable_http_path}",
            service_documentation_url=runtime.normalized_public_base_url,
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
        log_level=runtime.log_level,
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

    async def with_service(
        handler: Callable[[DeckbuildingService], Awaitable[BaseModel | dict[str, Any]]],
    ) -> BaseModel | dict[str, Any]:
        active_service = await get_service()
        return await handler(active_service)

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
                "mcp_auth_enabled": runtime.auth_enabled,
                "oauth_session_backend": "redis" if runtime.auth_enabled else "disabled",
                "oauth_session_expiration": "never" if runtime.auth_enabled else "disabled",
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

    @server.custom_route("/api/login", methods=["POST"])
    async def api_login(request: Request) -> Response:
        return await _handle_api_request(
            request,
            ArchidektLoginRequest,
            lambda payload: with_service(
                lambda active_service: active_service.login_archidekt(payload.account)
            ),
        )

    @server.custom_route("/api/personal-decks", methods=["POST"])
    async def api_personal_decks(request: Request) -> Response:
        return await _handle_api_request(
            request,
            PersonalDecksRequest,
            lambda payload: with_service(
                lambda active_service: active_service.list_personal_decks(payload.account)
            ),
        )

    @server.custom_route("/api/cards/search", methods=["POST"])
    async def api_search_archidekt_cards(request: Request) -> Response:
        return await _handle_api_request(
            request,
            ArchidektCardSearchRequest,
            lambda payload: with_service(
                lambda active_service: active_service.search_archidekt_cards(payload.filters)
            ),
        )

    @server.custom_route("/api/personal-deck-cards", methods=["POST"])
    async def api_personal_deck_cards(request: Request) -> Response:
        return await _handle_api_request(
            request,
            PersonalDeckCardsRequest,
            lambda payload: with_service(
                lambda active_service: active_service.get_personal_deck_cards(
                    deck_id=payload.deck_id,
                    include_deleted=payload.include_deleted,
                    account=payload.account,
                )
            ),
        )

    @server.custom_route("/api/personal-decks/create", methods=["POST"])
    async def api_create_personal_deck(request: Request) -> Response:
        return await _handle_api_request(
            request,
            PersonalDeckCreateRequest,
            lambda payload: with_service(
                lambda active_service: active_service.create_personal_deck(
                    deck=payload.deck,
                    account=payload.account,
                )
            ),
        )

    @server.custom_route("/api/personal-decks/update", methods=["POST"])
    async def api_update_personal_deck(request: Request) -> Response:
        return await _handle_api_request(
            request,
            PersonalDeckUpdateRequest,
            lambda payload: with_service(
                lambda active_service: active_service.update_personal_deck(
                    deck_id=payload.deck_id,
                    deck=payload.deck,
                    account=payload.account,
                )
            ),
        )

    @server.custom_route("/api/personal-decks/delete", methods=["POST"])
    async def api_delete_personal_deck(request: Request) -> Response:
        return await _handle_api_request(
            request,
            PersonalDeckDeleteRequest,
            lambda payload: with_service(
                lambda active_service: active_service.delete_personal_deck(
                    deck_id=payload.deck_id,
                    account=payload.account,
                )
            ),
        )

    @server.custom_route("/api/personal-decks/modify-cards", methods=["POST"])
    async def api_modify_personal_deck_cards(request: Request) -> Response:
        return await _handle_api_request(
            request,
            PersonalDeckCardsMutationRequest,
            lambda payload: with_service(
                lambda active_service: active_service.modify_personal_deck_cards(
                    deck_id=payload.deck_id,
                    cards=payload.cards,
                    account=payload.account,
                )
            ),
        )

    @server.custom_route("/api/collection/upsert", methods=["POST"])
    async def api_upsert_collection_entries(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionUpsertRequest,
            lambda payload: with_service(
                lambda active_service: active_service.upsert_collection_entries(
                    entries=payload.entries,
                    account=payload.account,
                )
            ),
        )

    @server.custom_route("/api/overview", methods=["POST"])
    async def api_overview(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionOverviewRequest,
            lambda payload: with_service(
                lambda active_service: active_service.get_collection_overview(
                    payload.collection,
                    payload.account,
                )
            ),
        )

    @server.custom_route("/api/search-owned", methods=["POST"])
    async def api_search_owned(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionSearchRequest,
            lambda payload: with_service(
                lambda active_service: active_service.search_owned_cards(
                    payload.collection,
                    _cap_limit(payload.filters, runtime.max_search_results),
                    payload.account,
                ),
            ),
        )

    @server.custom_route("/api/search-unowned", methods=["POST"])
    async def api_search_unowned(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionSearchRequest,
            lambda payload: with_service(
                lambda active_service: active_service.search_unowned_cards(
                    payload.collection,
                    _cap_limit(payload.filters, runtime.max_search_results),
                    payload.account,
                ),
            ),
        )

    @server.resource("deckbuilder://collection-contract")
    def collection_contract() -> str:
        return (
            "Collection overview and collection search tools require `collection` with one of collection_id, "
            "collection_url, or username. The server is stateless, so do not rely on implicit session state. "
            "Deck and collection mutation tools need an authenticated Archidekt identity, which can come from an "
            "explicit `account` object or the current MCP OAuth session."
        )

    @server.resource("deckbuilder://account-contract")
    def account_contract() -> str:
        return (
            "Authenticated Archidekt calls accept `account` with either token, or username/email plus password. "
            "Prefer calling `login_archidekt` once, then reuse the returned `account` object without the password. "
            "That login response also includes the current personal deck list when Archidekt returns it successfully. "
            "If this MCP server is connected through MCP OAuth, private tools may omit `account` and use the current "
            "authenticated MCP session instead. "
            "When MCP OAuth is active, the private read and write tools can all reuse that session-scoped identity "
            "without repeating credentials in the tool payload."
        )

    @server.resource("deckbuilder://filter-reference")
    def filter_reference() -> str:
        return (
            "Primary filters: exact_name, name_terms_all, oracle_terms_all, oracle_terms_any, "
            "oracle_terms_exclude, type_includes, subtype_includes, supertypes_includes, keywords_any, "
            "color_identity, color_identity_mode, colors, colors_mode, cmc_min, cmc_max, mana_values, "
            "commander_legal, rarities, set_codes, finishes, collection_tags_any, min_quantity, max_quantity, "
            "max_price, price_source, include_tokens, unique_by, sort_by, sort_direction, limit, and page. "
            "Preferred sorting contract: use `sort_by` with one of name, cmc, quantity, unit_price, "
            "total_value, updated_at, added_at, edhrec_rank, or rarity, plus `sort_direction` set to "
            "`asc` or `desc`. Example: `sort_by=unit_price` and `sort_direction=desc` for highest price first."
        )

    @server.resource("deckbuilder://routing-guide")
    def routing_guide() -> str:
        return (
            "Use search_owned_cards for owned cards and search_unowned_cards for missing cards. "
            "For Commander requests, prefer color_identity over current colors. "
            "If authenticated credentials are available and current deck context matters, start with login_archidekt "
            "because it returns the normalized account, inferred collection locator, and current personal decks. "
            "When the server is already connected through MCP OAuth, call login_archidekt without an account payload. "
            "When search_owned_cards returns personal_deck_usage, ask whether already-slotted cards may be reused. "
            "Use search_archidekt_cards to resolve Archidekt card ids before deck or collection writes. "
            "If many exact card names must be checked, send them together as one `exact_name` list instead of one call per card. "
            "When the user requests sorting, prefer canonical filters such as `sort_by=unit_price` with "
            "`sort_direction=desc` instead of shorthand `sort` strings."
        )

    @server.tool(
        annotations=SESSION_TOOL_ANNOTATIONS,
        description=(
            "Log into Archidekt using username/email plus password, or normalize an already-known token. "
            "Returns a normalized `account` object, the inferred personal collection locator, and the current "
            "personal deck list when available. If the MCP session is already authenticated through OAuth, "
            "this tool can be called without an `account` payload."
        ),
    )
    async def login_archidekt(account: ArchidektAccount | None = None) -> dict:
        active_service = await get_service()
        LOGGER.info("Tool call: login_archidekt identity=%s", describe_account(account))
        response = await active_service.login_archidekt(account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "List personal Archidekt decks for the authenticated account, including private and unlisted decks "
            "when Archidekt returns them to the logged-in user. If the MCP session is already authenticated through "
            "OAuth, this tool can be called without an `account` payload."
        ),
    )
    async def list_personal_decks(account: ArchidektAccount | None = None) -> dict:
        active_service = await get_service()
        LOGGER.info("Tool call: list_personal_decks identity=%s", describe_account(account))
        response = await active_service.list_personal_decks(account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "Search the Archidekt card catalog and return Archidekt `card_id` values that can be reused in "
            "deck or collection write operations. `exact_name` may be one name or a list of exact card names "
            "to batch several lookups in one request."
        ),
    )
    async def search_archidekt_cards(filters: ArchidektCardSearchFilters) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: search_archidekt_cards page=%s filters=%s",
            filters.page,
            filters.model_dump(mode="json"),
        )
        response = await active_service.search_archidekt_cards(filters)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "Return the cards currently in one personal deck, including `deck_relation_id` values needed to "
            "modify or remove existing entries."
        ),
    )
    async def get_personal_deck_cards(
        deck_id: int,
        include_deleted: bool = False,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: get_personal_deck_cards identity=%s deck_id=%s include_deleted=%s",
            describe_account(account),
            deck_id,
            include_deleted,
        )
        response = await active_service.get_personal_deck_cards(
            deck_id=deck_id,
            include_deleted=include_deleted,
            account=account,
        )
        return response.model_dump(mode="json")

    @server.tool(
        annotations=NON_DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description=(
            "Create a new personal Archidekt deck for the authenticated account. Use "
            "`modify_personal_deck_cards` afterwards to populate it with cards."
        ),
    )
    async def create_personal_deck(
        deck: PersonalDeckCreateInput,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: create_personal_deck identity=%s name=%s deck_format=%s",
            describe_account(account),
            deck.name,
            deck.deck_format,
        )
        response = await active_service.create_personal_deck(deck=deck, account=account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description=(
            "Update personal deck metadata such as name, description, format, visibility, bracket, folder, "
            "or related deck settings."
        ),
    )
    async def update_personal_deck(
        deck_id: int,
        deck: PersonalDeckUpdateInput,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: update_personal_deck identity=%s deck_id=%s fields=%s",
            describe_account(account),
            deck_id,
            deck.model_dump(mode="json", exclude_none=True),
        )
        response = await active_service.update_personal_deck(
            deck_id=deck_id,
            deck=deck,
            account=account,
        )
        return response.model_dump(mode="json")

    @server.tool(
        annotations=DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description="Delete one personal Archidekt deck owned by the authenticated account.",
    )
    async def delete_personal_deck(deck_id: int, account: ArchidektAccount | None = None) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: delete_personal_deck identity=%s deck_id=%s",
            describe_account(account),
            deck_id,
        )
        response = await active_service.delete_personal_deck(deck_id=deck_id, account=account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description=(
            "Add, modify, or remove cards in a personal Archidekt deck. Use `search_archidekt_cards`, "
            "`search_owned_cards`, or `get_personal_deck_cards` first to resolve the needed ids. "
            "Use `modifications.quantity` for the exact copy count when a card should appear more than once. "
            "Commander decks should normally keep non-basic cards at one copy, while non-Commander decks "
            "usually cap non-basic cards at four copies and allow unlimited basic lands."
        ),
    )
    async def modify_personal_deck_cards(
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: modify_personal_deck_cards identity=%s deck_id=%s count=%s",
            describe_account(account),
            deck_id,
            len(cards),
        )
        response = await active_service.modify_personal_deck_cards(
            deck_id=deck_id,
            cards=cards,
            account=account,
        )
        return response.model_dump(mode="json")

    @server.tool(
        annotations=DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description=(
            "Create or update authenticated collection entries for the logged-in user's own collection using "
            "Archidekt `card_id` values."
        ),
    )
    async def upsert_collection_entries(
        entries: list[CollectionCardUpsert],
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: upsert_collection_entries identity=%s count=%s",
            describe_account(account),
            len(entries),
        )
        response = await active_service.upsert_collection_entries(entries=entries, account=account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "Return an overview of the requested Archidekt collection. "
            "Always requires a `collection` object and optionally accepts `account` for private collections."
        ),
    )
    async def get_collection_overview(
        collection: CollectionLocator,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: get_collection_overview locator=%s account=%s",
            describe_collection_locator(collection),
            describe_account(account),
        )
        overview = await active_service.get_collection_overview(collection, account)
        return overview.model_dump(mode="json")

    @server.tool(
        annotations=NON_DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description=(
            "Force-refresh the cache entry for the requested collection. "
            "Always requires a `collection` object and optionally accepts `account` for private collections."
        ),
    )
    async def refresh_collection_cache(
        collection: CollectionLocator,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: refresh_collection_cache locator=%s account=%s",
            describe_collection_locator(collection),
            describe_account(account),
        )
        await active_service.get_snapshot(collection, force_refresh=True, account=account)
        overview = await active_service.get_collection_overview(collection, account)
        return overview.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "Search only cards owned in the Archidekt collection provided in the request. "
            "Requires `collection`, accepts `filters`, and optionally accepts `account` to include "
            "private collection access plus personal deck usage annotations."
        ),
    )
    async def search_owned_cards(
        collection: CollectionLocator,
        filters: CardSearchFilters | None = None,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        capped_filters = _cap_limit(_coerce_filters(filters), runtime.max_search_results)
        LOGGER.info(
            "Tool call: search_owned_cards locator=%s account=%s page=%s limit=%s filters=%s",
            describe_collection_locator(collection),
            describe_account(account),
            capped_filters.page,
            capped_filters.limit,
            capped_filters.model_dump(mode="json"),
        )
        response = await active_service.search_owned_cards(collection, capped_filters, account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "Search Scryfall for cards not owned in the Archidekt collection provided in the request. "
            "Requires `collection`, accepts `filters`, and optionally accepts `account` for private collections."
        ),
    )
    async def search_unowned_cards(
        collection: CollectionLocator,
        filters: CardSearchFilters | None = None,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        capped_filters = _cap_limit(_coerce_filters(filters), runtime.max_search_results)
        LOGGER.info(
            "Tool call: search_unowned_cards locator=%s account=%s page=%s limit=%s filters=%s",
            describe_collection_locator(collection),
            describe_account(account),
            capped_filters.page,
            capped_filters.limit,
            capped_filters.model_dump(mode="json"),
        )
        response = await active_service.search_unowned_cards(collection, capped_filters, account)
        return response.model_dump(mode="json")

    return server


async def _handle_api_request(
    request: Request,
    model_cls: type[ModelT],
    handler: Callable[[ModelT], Awaitable[BaseModel | dict[str, Any]]],
) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body.")

    try:
        parsed = model_cls.model_validate(payload)
    except ValidationError as error:
        return _json_error(422, "Invalid payload.", error.errors())

    try:
        result = await handler(parsed)
    except httpx.HTTPStatusError as error:
        return _json_error(502, "Remote HTTP error from Archidekt or Scryfall.", str(error))
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        return _json_error(400, str(error))
    except Exception as error:  # pragma: no cover
        LOGGER.exception("Unhandled API error")
        return _json_error(500, "Internal server error.", str(error))

    if isinstance(result, BaseModel):
        return JSONResponse(result.model_dump(mode="json"))
    return JSONResponse(result)


def _json_error(status_code: int, message: str, details: Any | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(payload, status_code=status_code)


def _coerce_filters(filters: CardSearchFilters | None) -> CardSearchFilters:
    return filters if filters is not None else CardSearchFilters()


def _cap_limit(filters: CardSearchFilters, max_limit: int) -> CardSearchFilters:
    if filters.limit <= max_limit:
        return filters
    return filters.model_copy(update={"limit": max_limit})


def _compact_optional_text(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    return compact or None
