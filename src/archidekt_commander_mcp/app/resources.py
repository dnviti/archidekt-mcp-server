from __future__ import annotations

# pyright: reportMissingImports=false

from typing import TYPE_CHECKING, Awaitable, Callable

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from ..services.deckbuilding import DeckbuildingService


def register_resources(
    server: FastMCP,
    get_service: Callable[[], Awaitable[DeckbuildingService]],
) -> None:
    del get_service

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
