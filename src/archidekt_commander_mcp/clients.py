from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import redis.asyncio as redis_async
from redis.exceptions import RedisError

from .config import RuntimeSettings
from .filtering import (
    build_type_line,
    compare_color_sets,
    normalize_color_symbols,
    scryfall_price_key,
)
from .models import CardSearchFilters, CollectionCardRecord, CollectionLocator, CollectionSnapshot


NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(?P<payload>.*?)</script>',
    re.DOTALL,
)
COLLECTION_LINK_PATTERN = re.compile(r"/collection/v2/(\d+)")
LOGGER = logging.getLogger("archidekt_commander_mcp.clients")


class ArchidektPublicCollectionClient:
    def __init__(self, http_client: httpx.AsyncClient, settings: RuntimeSettings) -> None:
        self.http_client = http_client
        self.settings = settings

    async def resolve_collection_id(self, collection: CollectionLocator) -> int:
        if collection.static_collection_id is not None:
            LOGGER.info(
                "Using requested Archidekt collection id %s",
                collection.static_collection_id,
            )
            return collection.static_collection_id

        if not collection.username:
            raise RuntimeError("No valid Archidekt locator was provided.")

        LOGGER.info(
            "Resolving public Archidekt collection id from username '%s'",
            collection.username,
        )
        response = await self.http_client.get(
            f"{self.settings.normalized_archidekt_base_url}/u/{collection.username}"
        )
        response.raise_for_status()

        match = COLLECTION_LINK_PATTERN.search(response.text)
        if not match:
            raise RuntimeError(
                "Unable to resolve a public collection from the Archidekt profile page."
            )
        LOGGER.info("Resolved Archidekt collection id %s from profile", match.group(1))
        return int(match.group(1))

    async def fetch_snapshot(self, collection: CollectionLocator) -> CollectionSnapshot:
        collection_id = await self.resolve_collection_id(collection)
        LOGGER.info(
            "Starting Archidekt collection sync for locator=%s game=%s",
            collection.display_locator,
            collection.game,
        )
        first_page = await self._fetch_collection_page(collection_id, game=collection.game, page=1)
        page_props = first_page["pageProps"]

        total_pages = int(page_props["totalPages"])
        all_records = self._extract_records(first_page)
        LOGGER.info(
            "Fetched Archidekt collection page 1/%s with %s records in page 1",
            total_pages,
            len(all_records),
        )
        for page_number in range(2, total_pages + 1):
            page_payload = await self._fetch_collection_page(
                collection_id,
                game=collection.game,
                page=page_number,
            )
            page_records = self._extract_records(page_payload)
            all_records.extend(page_records)
            LOGGER.info(
                "Fetched Archidekt collection page %s/%s with %s records",
                page_number,
                total_pages,
                len(page_records),
            )

        owner = page_props.get("owner") or {}
        redux = (page_props.get("redux") or {}).get("collectionV2") or {}

        snapshot = CollectionSnapshot(
            collection_id=collection_id,
            owner_id=owner.get("id"),
            owner_username=owner.get("username"),
            game=int(page_props.get("game") or collection.game),
            page_size=int(redux.get("preferredPageSize") or 100),
            total_pages=total_pages,
            total_records=int(page_props["count"]),
            fetched_at=datetime.now(UTC),
            source_url=f"{self.settings.normalized_archidekt_base_url}/collection/v2/{collection_id}",
            records=all_records,
        )
        LOGGER.info(
            "Completed Archidekt collection sync: owner=%s total_records=%s unique_records_loaded=%s",
            snapshot.owner_username,
            snapshot.total_records,
            len(snapshot.records),
        )
        return snapshot

    async def _fetch_collection_page(self, collection_id: int, game: int, page: int) -> dict[str, Any]:
        LOGGER.info(
            "Requesting Archidekt collection page collection_id=%s page=%s game=%s",
            collection_id,
            page,
            game,
        )
        response = await self.http_client.get(
            f"{self.settings.normalized_archidekt_base_url}/collection/v2/{collection_id}",
            params={"game": game, "page": page},
        )
        response.raise_for_status()

        payload_match = NEXT_DATA_PATTERN.search(response.text)
        if not payload_match:
            raise RuntimeError("The __NEXT_DATA__ payload was not found on the collection page.")

        payload = json.loads(payload_match.group("payload"))
        page_props = (payload.get("props") or {}).get("pageProps") or {}
        if page_props.get("__N_REDIRECT"):
            raise RuntimeError("Archidekt returned a server-side redirect for the collection.")
        return {"pageProps": page_props}

    def _extract_records(self, page_payload: dict[str, Any]) -> list[CollectionCardRecord]:
        page_props = page_payload["pageProps"]
        redux = (page_props.get("redux") or {}).get("collectionV2") or {}
        collection_cards = redux.get("collectionCards") or {}
        ordered_ids = redux.get("serverCollectionData") or []

        results: list[CollectionCardRecord] = []
        for record_id in ordered_ids:
            raw_record = collection_cards.get(str(record_id)) or collection_cards.get(record_id)
            if not raw_record:
                continue

            card = raw_record.get("card") or {}
            prices = card.get("prices") or {}
            legalities = card.get("legalities") or {}
            casting_cost = card.get("castingCost") or []

            supertypes = tuple(str(item) for item in (card.get("superTypes") or []) if item)
            types = tuple(str(item) for item in (card.get("types") or []) if item)
            subtypes = tuple(str(item) for item in (card.get("subTypes") or []) if item)
            mana_cost = (
                "".join(f"{{{symbol}}}" for symbol in casting_cost)
                if isinstance(casting_cost, list)
                else None
            )

            results.append(
                CollectionCardRecord(
                    record_id=int(raw_record["id"]),
                    created_at=_parse_datetime(raw_record.get("createdAt")),
                    updated_at=_parse_datetime(raw_record.get("modifiedAt")),
                    quantity=int(raw_record.get("quantity") or 0),
                    foil=bool(raw_record.get("foil")),
                    modifier=raw_record.get("modifier"),
                    tags=tuple(str(item) for item in (raw_record.get("tags") or []) if item),
                    condition_code=_safe_int(raw_record.get("condition")),
                    language_code=_safe_int(raw_record.get("language")),
                    name=str(card.get("name") or ""),
                    display_name=card.get("displayName"),
                    oracle_text=str(card.get("text") or ""),
                    mana_cost=mana_cost,
                    cmc=_safe_float(card.get("cmc")),
                    colors=normalize_color_symbols(card.get("colors") or []),
                    color_identity=normalize_color_symbols(card.get("colorIdentity") or []),
                    supertypes=supertypes,
                    types=types,
                    subtypes=subtypes,
                    type_line=build_type_line(supertypes, types, subtypes),
                    keywords=tuple(str(item) for item in (card.get("keywords") or []) if item),
                    rarity=(str(card.get("rarity")).casefold() if card.get("rarity") else None),
                    set_code=(str(card.get("setCode")).casefold() if card.get("setCode") else None),
                    set_name=card.get("set"),
                    commander_legal=_normalize_legality(legalities.get("commander")),
                    oracle_id=card.get("oracleCardUid"),
                    printing_id=card.get("uid"),
                    edhrec_rank=_safe_int(card.get("edhrecRank")),
                    image_uri=card.get("imgurl") or None,
                    prices={key: _safe_float(value) for key, value in prices.items()},
                )
            )

        return results


class CollectionCache:
    def __init__(
        self,
        client: ArchidektPublicCollectionClient,
        redis_client: redis_async.Redis,
        ttl_seconds: int,
        key_prefix: str = "archidekt-commander",
    ) -> None:
        self.client = client
        self.redis = redis_client
        self.ttl = timedelta(seconds=ttl_seconds)
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix.strip(":") or "archidekt-commander"

    async def get_snapshot(
        self,
        collection: CollectionLocator,
        force_refresh: bool = False,
    ) -> CollectionSnapshot:
        cache_key = collection.cache_key
        if not force_refresh:
            cached_snapshot = await self._load_cached_snapshot(cache_key)
            if cached_snapshot is not None:
                return cached_snapshot

        if force_refresh:
            LOGGER.info("Forced refresh requested for collection cache key=%s", cache_key)
        else:
            LOGGER.info("Collection cache miss or expired for key=%s; refreshing snapshot", cache_key)

        snapshot = await self.client.fetch_snapshot(collection)
        await self._persist_snapshot(cache_key, snapshot)
        LOGGER.info(
            "Collection cache refreshed for %s; next refresh after %s",
            cache_key,
            (datetime.now(UTC) + self.ttl).isoformat(),
        )
        return snapshot

    def _redis_key(self, cache_key: str) -> str:
        return f"{self.key_prefix}:collection:{cache_key}"

    async def _load_cached_snapshot(
        self,
        cache_key: str,
    ) -> CollectionSnapshot | None:
        redis_key = self._redis_key(cache_key)

        try:
            payload = await self.redis.get(redis_key)
        except RedisError as error:
            LOGGER.warning(
                "Redis cache read failed for %s; proceeding without cache: %s",
                cache_key,
                error,
            )
            return None

        try:
            if not payload:
                return None

            wrapper = json.loads(payload)
            snapshot_payload = wrapper.get("snapshot")
            saved_at = _parse_datetime(wrapper.get("saved_at"))
            if not isinstance(snapshot_payload, dict):
                raise ValueError("incomplete cache payload")
            snapshot = _deserialize_snapshot(snapshot_payload)
            ttl = await self._ttl(redis_key)
            if ttl is not None:
                LOGGER.info(
                    "Using Redis collection snapshot for %s; expires in %ss",
                    cache_key,
                    ttl,
                )
            elif saved_at is not None:
                LOGGER.info(
                    "Using Redis collection snapshot for %s; saved at %s",
                    cache_key,
                    saved_at.isoformat(),
                )
            else:
                LOGGER.info("Using Redis collection snapshot for %s", cache_key)
            return snapshot
        except RedisError as error:
            LOGGER.warning(
                "Redis cache metadata read failed for %s; proceeding without cache: %s",
                cache_key,
                error,
            )
            return None
        except Exception as error:
            LOGGER.warning(
                "Failed to decode Redis cache for %s: %s",
                cache_key,
                error,
            )
            await self._delete_key(redis_key)
            return None

    async def _persist_snapshot(
        self,
        cache_key: str,
        snapshot: CollectionSnapshot,
    ) -> None:
        redis_key = self._redis_key(cache_key)
        payload = {
            "cache_key": cache_key,
            "saved_at": datetime.now(UTC).isoformat(),
            "snapshot": _serialize_snapshot(snapshot),
        }
        try:
            await self.redis.set(
                redis_key,
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                ex=self.ttl_seconds,
            )
        except RedisError as error:
            LOGGER.warning(
                "Redis cache write failed for %s; continuing without persisted cache: %s",
                cache_key,
                error,
            )

    async def _ttl(self, redis_key: str) -> int | None:
        ttl = await self.redis.ttl(redis_key)
        if ttl >= 0:
            return ttl
        if ttl == -1:
            return None
        return None

    async def _delete_key(self, redis_key: str) -> None:
        try:
            await self.redis.delete(redis_key)
        except RedisError as error:
            LOGGER.warning("Failed to delete invalid Redis cache key %s: %s", redis_key, error)


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
        try:
            price_value = float(prices.get(price_field))
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


def _color_query(prefix: str, colors: list[str], mode: str) -> str:
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


def _parse_datetime(raw_value: Any) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_legality(raw_value: Any) -> bool | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip().casefold()
    if value == "legal":
        return True
    if value in {"not_legal", "banned", "restricted"}:
        return False
    return None


def _safe_int(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _safe_float(raw_value: Any) -> float | None:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _serialize_snapshot(snapshot: CollectionSnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload["fetched_at"] = snapshot.fetched_at.isoformat()
    payload["records"] = [_serialize_record(record) for record in snapshot.records]
    return payload


def _serialize_record(record: CollectionCardRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["created_at"] = record.created_at.isoformat() if record.created_at else None
    payload["updated_at"] = record.updated_at.isoformat() if record.updated_at else None
    payload["tags"] = list(record.tags)
    payload["colors"] = list(record.colors)
    payload["color_identity"] = list(record.color_identity)
    payload["supertypes"] = list(record.supertypes)
    payload["types"] = list(record.types)
    payload["subtypes"] = list(record.subtypes)
    payload["keywords"] = list(record.keywords)
    return payload


def _deserialize_snapshot(payload: dict[str, Any]) -> CollectionSnapshot:
    return CollectionSnapshot(
        collection_id=int(payload["collection_id"]),
        owner_id=_safe_int(payload.get("owner_id")),
        owner_username=payload.get("owner_username"),
        game=int(payload["game"]),
        page_size=int(payload["page_size"]),
        total_pages=int(payload["total_pages"]),
        total_records=int(payload["total_records"]),
        fetched_at=_require_datetime(payload.get("fetched_at")),
        source_url=str(payload["source_url"]),
        records=[_deserialize_record(record_payload) for record_payload in payload.get("records", [])],
    )


def _deserialize_record(payload: dict[str, Any]) -> CollectionCardRecord:
    return CollectionCardRecord(
        record_id=int(payload["record_id"]),
        created_at=_parse_datetime(payload.get("created_at")),
        updated_at=_parse_datetime(payload.get("updated_at")),
        quantity=int(payload["quantity"]),
        foil=bool(payload["foil"]),
        modifier=payload.get("modifier"),
        tags=tuple(str(item) for item in payload.get("tags", [])),
        condition_code=_safe_int(payload.get("condition_code")),
        language_code=_safe_int(payload.get("language_code")),
        name=str(payload.get("name") or ""),
        display_name=payload.get("display_name"),
        oracle_text=str(payload.get("oracle_text") or ""),
        mana_cost=payload.get("mana_cost"),
        cmc=_safe_float(payload.get("cmc")),
        colors=tuple(str(item) for item in payload.get("colors", [])),
        color_identity=tuple(str(item) for item in payload.get("color_identity", [])),
        supertypes=tuple(str(item) for item in payload.get("supertypes", [])),
        types=tuple(str(item) for item in payload.get("types", [])),
        subtypes=tuple(str(item) for item in payload.get("subtypes", [])),
        type_line=str(payload.get("type_line") or ""),
        keywords=tuple(str(item) for item in payload.get("keywords", [])),
        rarity=payload.get("rarity"),
        set_code=payload.get("set_code"),
        set_name=payload.get("set_name"),
        commander_legal=payload.get("commander_legal"),
        oracle_id=payload.get("oracle_id"),
        printing_id=payload.get("printing_id"),
        edhrec_rank=_safe_int(payload.get("edhrec_rank")),
        image_uri=payload.get("image_uri"),
        prices={str(key): _safe_float(value) for key, value in (payload.get("prices") or {}).items()},
    )


def _require_datetime(raw_value: Any) -> datetime:
    parsed = _parse_datetime(raw_value)
    if parsed is None:
        raise ValueError("invalid cached timestamp")
    return parsed
