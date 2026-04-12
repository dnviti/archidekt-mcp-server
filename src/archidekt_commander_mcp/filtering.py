from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import CardResult, CardSearchFilters, CollectionCardRecord


_COLOR_WORD_MAP = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
}

_SCRYFALL_PRICE_MAP = {
    "tcg": "usd",
    "ck": "usd",
    "cm": "eur",
    "scg": "usd",
    "mp": "usd",
    "ct": "eur",
    "usd": "usd",
    "eur": "eur",
    "tix": "tix",
}

_RARITY_SORT_ORDER = {
    "common": 0,
    "uncommon": 1,
    "rare": 2,
    "mythic": 3,
    "special": 4,
    "bonus": 5,
}


def normalize_color_symbols(raw_values: Iterable[str] | None) -> tuple[str, ...]:
    if not raw_values:
        return ()

    normalized: list[str] = []
    for raw_value in raw_values:
        value = str(raw_value).strip()
        if not value:
            continue
        if len(value) == 1:
            symbol = value.upper()
        else:
            symbol = _COLOR_WORD_MAP.get(value.casefold(), "")
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    return tuple(normalized)


def build_type_line(
    supertypes: Iterable[str] | None,
    types: Iterable[str] | None,
    subtypes: Iterable[str] | None,
) -> str:
    left = " ".join([part for part in [*(supertypes or []), *(types or [])] if part])
    right = " ".join([part for part in (subtypes or []) if part])
    if left and right:
        return f"{left} - {right}"
    return left or right


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def collection_price_key(price_source: str, foil: bool) -> str:
    if price_source in {"usd", "eur", "tix"}:
        price_source = "tcg"
    if foil:
        return {
            "tcg": "tcgFoil",
            "ck": "ckFoil",
            "cm": "cmFoil",
            "scg": "scgFoil",
            "mp": "mpFoil",
            "ct": "ctFoil",
        }.get(price_source, "tcgFoil")
    return {
        "tcg": "tcg",
        "ck": "ck",
        "cm": "cm",
        "scg": "scg",
        "mp": "mp",
        "ct": "ct",
    }.get(price_source, "tcg")


def get_collection_unit_price(record: CollectionCardRecord, price_source: str) -> float | None:
    key = collection_price_key(price_source, record.foil)
    raw_value = record.prices.get(key)
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def scryfall_price_key(price_source: str) -> str:
    return _SCRYFALL_PRICE_MAP.get(price_source, "usd")


def compare_color_sets(card_values: Iterable[str], filter_values: Iterable[str], mode: str) -> bool:
    if mode == "ignore":
        return True

    card_set = set(card_values)
    filter_set = set(filter_values)

    if mode == "subset":
        return card_set.issubset(filter_set)
    if mode == "exact":
        return card_set == filter_set
    if mode == "overlap":
        return bool(card_set & filter_set)
    return True


def record_matches_filters(record: CollectionCardRecord, filters: CardSearchFilters) -> bool:
    name = normalize_text(record.display_name or record.name)
    oracle_text = normalize_text(record.oracle_text)
    type_line = normalize_text(record.type_line)
    tags = {normalize_text(tag) for tag in record.tags}
    keywords = {normalize_text(keyword) for keyword in record.keywords}
    rarity = (record.rarity or "").casefold()
    set_code = (record.set_code or "").casefold()
    subtypes = {normalize_text(subtype) for subtype in record.subtypes}
    supertypes = {normalize_text(supertype) for supertype in record.supertypes}
    types = {normalize_text(card_type) for card_type in record.types}

    if filters.exact_name:
        exact_names = {normalize_text(candidate) for candidate in filters.exact_name}
        if name not in exact_names and normalize_text(record.name) not in exact_names:
            return False

    if any(normalize_text(term) not in name for term in filters.name_terms_all):
        return False
    if any(normalize_text(term) not in oracle_text for term in filters.oracle_terms_all):
        return False
    if filters.oracle_terms_any and not any(
        normalize_text(term) in oracle_text for term in filters.oracle_terms_any
    ):
        return False
    if any(normalize_text(term) in oracle_text for term in filters.oracle_terms_exclude):
        return False

    if any(normalize_text(term) not in type_line for term in filters.type_includes):
        return False
    if any(normalize_text(term) in type_line for term in filters.type_excludes):
        return False
    if any(normalize_text(term) not in subtypes for term in filters.subtype_includes):
        return False
    if any(normalize_text(term) in subtypes for term in filters.subtype_excludes):
        return False
    if any(normalize_text(term) not in supertypes for term in filters.supertypes_includes):
        return False
    if any(normalize_text(term) in supertypes for term in filters.supertypes_excludes):
        return False
    if filters.keywords_any and not any(
        normalize_text(keyword) in keywords for keyword in filters.keywords_any
    ):
        return False

    if not compare_color_sets(record.colors, filters.colors, filters.colors_mode):
        return False
    if not compare_color_sets(record.color_identity, filters.color_identity, filters.color_identity_mode):
        return False

    if filters.cmc_min is not None and (record.cmc is None or record.cmc < filters.cmc_min):
        return False
    if filters.cmc_max is not None and (record.cmc is None or record.cmc > filters.cmc_max):
        return False
    if filters.mana_values and int(record.cmc or -1) not in set(filters.mana_values):
        return False
    if filters.commander_legal is not None and record.commander_legal != filters.commander_legal:
        return False
    if filters.rarities and rarity not in set(filters.rarities):
        return False
    if filters.set_codes and set_code not in set(filters.set_codes):
        return False

    if filters.finishes:
        finish = "foil" if record.foil else "normal"
        if finish not in set(filters.finishes):
            return False

    if filters.collection_tags_any and not (tags & {normalize_text(tag) for tag in filters.collection_tags_any}):
        return False
    if filters.min_quantity is not None and record.quantity < filters.min_quantity:
        return False
    if filters.max_quantity is not None and record.quantity > filters.max_quantity:
        return False

    if not filters.include_tokens and "token" in types:
        return False

    if filters.max_price is not None:
        unit_price = get_collection_unit_price(record, filters.price_source)
        if unit_price is None or unit_price > filters.max_price:
            return False

    return True


def aggregate_owned_results(
    records: list[CollectionCardRecord],
    filters: CardSearchFilters,
    collection_id: int,
    base_url: str,
) -> list[CardResult]:
    if filters.unique_by == "printing":
        groups = {str(record.record_id): [record] for record in records}
    else:
        groups: dict[str, list[CollectionCardRecord]] = defaultdict(list)
        for record in records:
            groups[record.oracle_id or record.name.casefold()].append(record)

    results: list[CardResult] = []
    for grouped_records in groups.values():
        ordered = sorted(
            grouped_records,
            key=lambda item: (item.display_name or item.name, item.set_code or "", item.record_id),
        )
        representative = ordered[0]
        quantity = sum(item.quantity for item in ordered)

        priced_records: list[tuple[int, float]] = []
        for item in ordered:
            unit_price = get_collection_unit_price(item, filters.price_source)
            if unit_price is not None:
                priced_records.append((item.quantity, unit_price))

        total_value = sum(item_quantity * item_price for item_quantity, item_price in priced_records)
        priced_quantity = sum(item_quantity for item_quantity, _ in priced_records)
        weighted_unit_price = round(total_value / priced_quantity, 2) if priced_quantity else None
        added_at = min((item.created_at for item in ordered if item.created_at), default=None)
        updated_at = max((item.updated_at for item in ordered if item.updated_at), default=None)

        results.append(
            CardResult(
                source="collection",
                ownership_scope="owned",
                name=representative.display_name or representative.name,
                quantity=quantity,
                mana_cost=representative.mana_cost,
                cmc=representative.cmc,
                type_line=representative.type_line,
                oracle_text=representative.oracle_text,
                colors=list(representative.colors),
                color_identity=list(representative.color_identity),
                keywords=sorted({keyword for item in ordered for keyword in item.keywords}),
                rarity=representative.rarity,
                set_code=representative.set_code,
                set_name=representative.set_name,
                finishes=sorted({"foil" if item.foil else "normal" for item in ordered}),
                tags=sorted({tag for item in ordered for tag in item.tags}),
                commander_legal=representative.commander_legal,
                edhrec_rank=representative.edhrec_rank,
                unit_price=weighted_unit_price,
                total_value=round(total_value, 2) if priced_records else None,
                price_source=filters.price_source,
                added_at=added_at,
                updated_at=updated_at,
                oracle_id=representative.oracle_id,
                source_uri=f"{base_url}/collection/v2/{collection_id}",
                image_uri=representative.image_uri,
                archidekt_card_ids=sorted(
                    {item.card_id for item in ordered if item.card_id is not None}
                ),
                archidekt_record_ids=sorted(item.record_id for item in ordered),
            )
        )

    return results


def sort_card_results(results: list[CardResult], filters: CardSearchFilters) -> list[CardResult]:
    reverse = filters.sort_direction == "desc"

    if filters.sort_by == "name":
        return sorted(results, key=lambda result: result.name.casefold(), reverse=reverse)

    def sortable_value(result: CardResult) -> float | int | None:
        if filters.sort_by == "cmc":
            return result.cmc
        if filters.sort_by == "quantity":
            return result.quantity
        if filters.sort_by == "unit_price":
            return result.unit_price
        if filters.sort_by == "total_value":
            return result.total_value
        if filters.sort_by == "updated_at":
            return result.updated_at.timestamp() if result.updated_at else None
        if filters.sort_by == "added_at":
            return result.added_at.timestamp() if result.added_at else None
        if filters.sort_by == "edhrec_rank":
            return result.edhrec_rank
        if filters.sort_by == "rarity":
            return _RARITY_SORT_ORDER.get((result.rarity or "").casefold())
        return None

    present_results = [result for result in results if sortable_value(result) is not None]
    missing_results = [result for result in results if sortable_value(result) is None]

    # Keep alphabetical ordering stable inside equal-value groups while still honoring desc/asc.
    present_results = sorted(present_results, key=lambda result: result.name.casefold())
    present_results = sorted(present_results, key=lambda result: sortable_value(result), reverse=reverse)
    missing_results = sorted(missing_results, key=lambda result: result.name.casefold())
    return present_results + missing_results


def paginate_results(results: list[CardResult], page: int, limit: int) -> list[CardResult]:
    start = (page - 1) * limit
    end = start + limit
    return results[start:end]
