# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from ..config import RuntimeSettings
from ..schemas.accounts import (
    ArchidektLoginRequest,
    CollectionDeleteRequest,
    CollectionOverviewRequest,
    CollectionSearchRequest,
    CollectionUpsertRequest,
    PersonalDeckCardsMutationRequest,
    PersonalDeckCardsRequest,
    PersonalDeckCreateRequest,
    PersonalDeckDeleteRequest,
    PersonalDecksRequest,
    PersonalDeckUpdateRequest,
)
from ..schemas.search import ArchidektCardSearchRequest
from ..services.deckbuilding import DeckbuildingService
from .http_helpers import _cap_limit, _handle_api_request


def register_http_routes(
    server: FastMCP,
    get_service: Callable[[], Awaitable[DeckbuildingService]],
    runtime: RuntimeSettings,
) -> None:
    async def with_service(
        handler: Callable[[DeckbuildingService], Awaitable[BaseModel | dict[str, Any]]],
    ) -> BaseModel | dict[str, Any]:
        active_service = await get_service()
        return await handler(active_service)

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

    @server.custom_route("/api/collection/delete", methods=["POST"])
    async def api_delete_collection_entries(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionDeleteRequest,
            lambda payload: with_service(
                lambda active_service: active_service.delete_collection_entries(
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
