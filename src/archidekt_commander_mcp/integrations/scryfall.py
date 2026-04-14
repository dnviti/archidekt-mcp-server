from __future__ import annotations

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import logging
from typing import Any, Sequence

import httpx

from ..config import RuntimeSettings
from ..filtering import compare_color_sets, scryfall_price_key
from ..schemas.search import CardSearchFilters
from .serialization import _normalize_legality


LOGGER = logging.getLogger("archidekt_commander_mcp.clients")


class ScryfallClient:
    def __init__(self, http_client: httpx.AsyncClient, settings: RuntimeSettings) -> None:
        self.http_client = http_client
        self.settings = settings

    async def search_unowned_cards(
        self,
        filters: CardSearchFilters,
        owned_oracle_ids: set[str],
        owned_names: set[str],
    ) -> tuple[list[dict[str, Any]], str, bool | None, list[str]]:
        query = build_scryfall_query(filters)
        notes = [f"Query Scryfall deterministica: {query}"]
        LOGGER.info(
            "Starting Scryfall unowned search query=%s page=%s limit=%s",
            query,
            filters.page,
            filters.limit,
        )

        desired_end_index = filters.page * filters.limit
        kept_results: list[dict[str, Any]] = []
        current_page = 1
        has_more = False

        while current_page <= self.settings.scryfall_max_pages:
            LOGGER.info(
                "Requesting Scryfall search page %s/%s",
                current_page,
                self.settings.scryfall_max_pages,
            )
            response = await self.http_client.get(
                f"{self.settings.normalized_scryfall_base_url}/cards/search",
                params={
                    "q": query,
                    "page": current_page,
                    "order": map_scryfall_order(filters.sort_by, filters.price_source),
                    "dir": filters.sort_direction,
                    "unique": "cards" if filters.unique_by == "oracle" else "prints",
                },
                headers={"Accept": "application/json"},
            )

            if response.status_code == 404:
                return [], query, False, notes + ["Scryfall returned no results."]

            response.raise_for_status()
            payload = response.json()
            page_total = len(payload.get("data", []))
            excluded_owned = 0

            for card in payload.get("data", []):
                oracle_id = card.get("oracle_id")
                normalized_name = str(card.get("name") or "").casefold()
                if oracle_id and oracle_id in owned_oracle_ids:
                    excluded_owned += 1
                    continue
                if normalized_name in owned_names:
                    excluded_owned += 1
                    continue
                kept_results.append(card)

            has_more = bool(payload.get("has_more"))
            LOGGER.info(
                "Processed Scryfall page %s: raw=%s excluded_owned=%s kept_so_far=%s has_more=%s",
                current_page,
                page_total,
                excluded_owned,
                len(kept_results),
                has_more,
            )
            if len(kept_results) >= desired_end_index or not has_more:
                break
            current_page += 1

        if has_more and len(kept_results) < desired_end_index:
            notes.append(
                "Unowned pagination stopped after reaching the configured maximum number of pages."
            )

        LOGGER.info(
            "Completed Scryfall unowned search: kept_results=%s query=%s",
            len(kept_results),
            query,
        )
        return kept_results, query, has_more, notes


def build_scryfall_query(filters: CardSearchFilters) -> str:
    parts: list[str] = []

    if filters.exact_name:
        parts.append(_or_group([f'name:{_quote(name)}' for name in filters.exact_name]))
    parts.extend(f"name:{_quote(term)}" for term in filters.name_terms_all)
    parts.extend(f'oracle:{_quote(term)}' for term in filters.oracle_terms_all)

    if filters.oracle_terms_any:
        parts.append(_or_group([f'oracle:{_quote(term)}' for term in filters.oracle_terms_any]))

    parts.extend(f'-oracle:{_quote(term)}' for term in filters.oracle_terms_exclude)
    parts.extend(f"t:{_simple_token(term)}" for term in filters.type_includes)
    parts.extend(f"-t:{_simple_token(term)}" for term in filters.type_excludes)
    parts.extend(f"t:{_simple_token(term)}" for term in filters.subtype_includes)
    parts.extend(f"-t:{_simple_token(term)}" for term in filters.subtype_excludes)
    parts.extend(f"t:{_simple_token(term)}" for term in filters.supertypes_includes)
    parts.extend(f"-t:{_simple_token(term)}" for term in filters.supertypes_excludes)

    if filters.keywords_any:
        parts.append(_or_group([f"keyword:{_simple_token(term)}" for term in filters.keywords_any]))

    color_query = _color_query("c", filters.colors, filters.colors_mode)
    identity_query = _color_query("id", filters.color_identity, filters.color_identity_mode)
    if color_query:
        parts.append(color_query)
    if identity_query:
        parts.append(identity_query)

    if filters.cmc_min is not None:
        parts.append(f"cmc>={filters.cmc_min:g}")
    if filters.cmc_max is not None:
        parts.append(f"cmc<={filters.cmc_max:g}")
    if filters.mana_values:
        parts.append(_or_group([f"cmc={value}" for value in filters.mana_values]))

    if filters.commander_legal is True:
        parts.append("legal:commander")
    elif filters.commander_legal is False:
        parts.append("-legal:commander")

    if filters.rarities:
        parts.append(_or_group([f"r:{rarity}" for rarity in filters.rarities]))
    parts.extend(f"set:{set_code}" for set_code in filters.set_codes)

    if not filters.include_tokens:
        parts.append("-t:token")

    if filters.max_price is not None:
        price_field = scryfall_price_key(filters.price_source)
        parts.append(f"{price_field}<={filters.max_price:g}")

    return " ".join(part for part in parts if part).strip() or "game:paper"


def map_scryfall_order(sort_by: str, price_source: str) -> str:
    if sort_by == "name":
        return "name"
    if sort_by == "cmc":
        return "cmc"
    if sort_by == "rarity":
        return "rarity"
    if sort_by == "edhrec_rank":
        return "edhrec"
    if sort_by in {"unit_price", "total_value"}:
        return scryfall_price_key(price_source)
    return "name"


def card_matches_scryfall_filters(card: dict[str, Any], filters: CardSearchFilters) -> bool:
    type_line = (card.get("type_line") or "").casefold()
    oracle_text = (card.get("oracle_text") or "").casefold()
    name = (card.get("name") or "").casefold()
    keywords = {str(keyword).casefold() for keyword in (card.get("keywords") or [])}
    colors = tuple(str(color).upper() for color in (card.get("colors") or []))
    color_identity = tuple(str(color).upper() for color in (card.get("color_identity") or []))
    rarity = (card.get("rarity") or "").casefold()
    set_code = (card.get("set") or "").casefold()
    prices = card.get("prices") or {}
    legalities = card.get("legalities") or {}

    if filters.exact_name:
        if name not in {candidate.casefold() for candidate in filters.exact_name}:
            return False

    if any(term.casefold() not in name for term in filters.name_terms_all):
        return False
    if any(term.casefold() not in oracle_text for term in filters.oracle_terms_all):
        return False
    if filters.oracle_terms_any and not any(
        term.casefold() in oracle_text for term in filters.oracle_terms_any
    ):
        return False
    if any(term.casefold() in oracle_text for term in filters.oracle_terms_exclude):
        return False
    if any(term.casefold() not in type_line for term in filters.type_includes):
        return False
    if any(term.casefold() in type_line for term in filters.type_excludes):
        return False
    if any(term.casefold() not in type_line for term in filters.subtype_includes):
        return False
    if any(term.casefold() in type_line for term in filters.subtype_excludes):
        return False
    if any(term.casefold() not in type_line for term in filters.supertypes_includes):
        return False
    if any(term.casefold() in type_line for term in filters.supertypes_excludes):
        return False
    if filters.keywords_any and not any(term.casefold() in keywords for term in filters.keywords_any):
        return False

    if not compare_color_sets(colors, filters.colors, filters.colors_mode):
        return False
    if not compare_color_sets(color_identity, filters.color_identity, filters.color_identity_mode):
        return False

    cmc = card.get("cmc")
    if filters.cmc_min is not None and (cmc is None or float(cmc) < filters.cmc_min):
        return False
    if filters.cmc_max is not None and (cmc is None or float(cmc) > filters.cmc_max):
        return False
    if filters.mana_values and int(float(cmc or -1)) not in set(filters.mana_values):
        return False

    commander_legal = _normalize_legality(legalities.get("commander"))
    if filters.commander_legal is not None and commander_legal != filters.commander_legal:
        return False

    if filters.rarities and rarity not in set(filters.rarities):
        return False
    if filters.set_codes and set_code not in set(filters.set_codes):
        return False

    if filters.max_price is not None:
        price_field = scryfall_price_key(filters.price_source)
        raw_price = prices.get(price_field)
        if not isinstance(raw_price, (str, int, float)):
            return False
        try:
            price_value = float(raw_price)
        except (TypeError, ValueError):
            return False
        if price_value > filters.max_price:
            return False

    if not filters.include_tokens and "token" in type_line:
        return False

    return True


def _simple_token(value: str) -> str:
    return value.strip().replace('"', "")


def _quote(value: str) -> str:
    escaped = value.strip().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _or_group(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + " or ".join(parts) + ")"


def _color_query(prefix: str, colors: Sequence[str], mode: str) -> str:
    if mode == "ignore":
        return ""
    compact = "".join(color.lower() for color in colors)
    if mode == "subset":
        return f"{prefix}<={compact or '0'}"
    if mode == "exact":
        return f"{prefix}={compact or '0'}"
    if mode == "overlap":
        return _or_group([f"{prefix}:{color.lower()}" for color in colors])
    return ""
