# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from typing import Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from ..config import RuntimeSettings
from ..schemas.accounts import ArchidektAccount, CollectionLocator
from ..schemas.collections import (
    CollectionAvailabilityCardRequest,
    CollectionAvailabilityOptions,
    CollectionCardDelete,
    CollectionCardUpsert,
    CollectionReadOptions,
)
from ..schemas.decks import PersonalDeckCardMutation, PersonalDeckCreateInput, PersonalDeckUpdateInput
from ..schemas.search import ArchidektCardSearchFilters, CardSearchFilters
from ..services.account_resolution import describe_account, describe_collection_locator
from ..services.deckbuilding import LOGGER
from ..services.deckbuilding import DeckbuildingService
from ..server_contracts import (
    DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
    NON_DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
    READ_ONLY_TOOL_ANNOTATIONS,
    SESSION_TOOL_ANNOTATIONS,
)
from .http_helpers import _cap_limit, _coerce_filters


def register_mcp_tools(
    server: FastMCP,
    get_service: Callable[[], Awaitable[DeckbuildingService]],
    runtime: RuntimeSettings,
) -> None:
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
            "usually cap non-basic cards at four copies and allow unlimited basic lands. If the user has "
            "asked to use only collection cards, call `check_collection_card_availability` first and do not "
            "add cards where `must_not_use=true` or `enough_copies=false`."
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
            "Archidekt `card_id` values. Provide `record_id` when updating an existing collection row; "
            "omitting it creates a new row. Use `delete_collection_entries` instead of quantity tricks when a "
            "collection row should be removed."
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
        annotations=DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS,
        description=(
            "Delete one or more authenticated collection entries by `record_id`. Use `search_owned_cards` "
            "first and reuse the returned `archidekt_record_ids` values."
        ),
    )
    async def delete_collection_entries(
        entries: list[CollectionCardDelete],
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: delete_collection_entries identity=%s count=%s",
            describe_account(account),
            len(entries),
        )
        response = await active_service.delete_collection_entries(entries=entries, account=account)
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
            "Read the requested Archidekt collection through Archidekt's own "
            "`/api/collection/export/v2/{user_id}/` CSV export endpoint. Use this instead of raw curl "
            "when complete collection data is needed for ownership checks. `options` defaults to fetching "
            "all export pages with Archidekt's standard fields and returns a parsed preview. Set "
            "`options.include_csv_content=true` when the model needs the full CSV content in the tool "
            "response. Set `options.export_to_file=true` or provide `options.file_path` when the user asks "
            "to export the collection as a CSV file; the response returns the written file path."
        ),
    )
    async def read_collection(
        collection: CollectionLocator,
        options: CollectionReadOptions | None = None,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: read_collection locator=%s account=%s export_to_file=%s include_csv_content=%s",
            describe_collection_locator(collection),
            describe_account(account),
            bool(options and (options.export_to_file or options.file_path)),
            bool(options and options.include_csv_content),
        )
        response = await active_service.read_collection(collection, options, account)
        return response.model_dump(mode="json")

    @server.tool(
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
        description=(
            "Check whether requested cards have enough free copies for a new or edited deck. "
            "This combines the authenticated collection snapshot with personal deck usage: "
            "`available_quantity = collection_quantity - used_in_decks_quantity`. Use this before "
            "`modify_personal_deck_cards` when the user says to use only collection cards. If "
            "`collection_only=true`, cards with `must_not_use=true` or `enough_copies=false` must be "
            "replaced with other owned cards. `options.exclude_deck_ids` can ignore the deck currently "
            "being edited."
        ),
    )
    async def check_collection_card_availability(
        collection: CollectionLocator,
        cards: list[CollectionAvailabilityCardRequest],
        options: CollectionAvailabilityOptions | None = None,
        account: ArchidektAccount | None = None,
    ) -> dict:
        active_service = await get_service()
        LOGGER.info(
            "Tool call: check_collection_card_availability locator=%s account=%s count=%s",
            describe_collection_locator(collection),
            describe_account(account),
            len(cards),
        )
        response = await active_service.check_collection_card_availability(
            collection=collection,
            cards=cards,
            options=options,
            account=account,
        )
        return response.model_dump(mode="json")

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
            "private collection access plus personal deck usage and collection-only availability annotations."
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
