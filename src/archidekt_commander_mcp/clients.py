from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urlencode

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
from .models import (
    ArchidektAccount,
    ArchidektCardReference,
    ArchidektCardSearchFilters,
    AuthenticatedAccount,
    CardSearchFilters,
    CollectionCardUpsert,
    CollectionCardRecord,
    CollectionLocator,
    CollectionSnapshot,
    PersonalDeckCardMutation,
    PersonalDeckCreateInput,
    PersonalDeckSummary,
    PersonalDeckUpdateInput,
)


NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(?P<payload>.*?)</script>',
    re.DOTALL,
)
COLLECTION_LINK_PATTERN = re.compile(r"/collection/v2/(\d+)")
LOGGER = logging.getLogger("archidekt_commander_mcp.clients")


def _auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"JWT {token}"}


def _json_headers(token: str | None) -> dict[str, str]:
    headers = _auth_headers(token)
    headers["Accept"] = "application/json"
    headers["Content-Type"] = "application/json"
    return headers


def build_archidekt_exact_name_filters(
    exact_names: list[str],
    *,
    game: int,
    page: int = 1,
    edition_code: str | None = None,
    include_tokens: bool = False,
    include_digital: bool = False,
    all_editions: bool = False,
) -> ArchidektCardSearchFilters:
    return ArchidektCardSearchFilters(
        exact_name=exact_names,
        game=game,
        page=page,
        edition_code=edition_code,
        include_tokens=include_tokens,
        include_digital=include_digital,
        all_editions=all_editions,
    )


class ArchidektRequestGate:
    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        time_source: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = float(window_seconds)
        self._time_source = time_source or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._request_started_at: deque[float] = deque()

    @classmethod
    def from_settings(
        cls,
        settings: RuntimeSettings,
        *,
        time_source: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> ArchidektRequestGate:
        return cls(
            max_requests=settings.archidekt_rate_limit_max_requests,
            window_seconds=settings.archidekt_rate_limit_window_seconds,
            time_source=time_source,
            sleep=sleep,
        )

    async def wait_for_slot(self) -> None:
        while True:
            async with self._lock:
                now = self._time_source()
                self._evict_expired(now)
                if len(self._request_started_at) < self.max_requests:
                    self._request_started_at.append(now)
                    return
                wait_seconds = max(
                    (self._request_started_at[0] + self.window_seconds) - now,
                    0.0,
                )

            if wait_seconds > 0:
                await self._sleep(wait_seconds)
            else:
                await asyncio.sleep(0)

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._request_started_at and self._request_started_at[0] <= cutoff:
            self._request_started_at.popleft()


class _ArchidektHttpClientBase:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: RuntimeSettings,
        request_gate: ArchidektRequestGate | None = None,
    ) -> None:
        self.http_client = http_client
        self.settings = settings
        self.request_gate = request_gate or ArchidektRequestGate.from_settings(settings)
        self._retry_sleep = self.request_gate._sleep

    async def _request_archidekt(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        max_attempts = self.settings.archidekt_retry_max_attempts
        attempt = 0

        while True:
            await self.request_gate.wait_for_slot()
            response = await self.http_client.request(method, url, **kwargs)
            if response.status_code != 429 or attempt >= max_attempts:
                return response

            retry_delay_seconds = self._archidekt_retry_delay_seconds(response, attempt)
            LOGGER.warning(
                "Archidekt returned 429 for %s %s; retrying in %.3f seconds (attempt %s/%s)",
                method,
                url,
                retry_delay_seconds,
                attempt + 1,
                max_attempts,
            )
            await self._retry_sleep(retry_delay_seconds)
            attempt += 1

    def _archidekt_retry_delay_seconds(
        self,
        response: httpx.Response,
        attempt: int,
    ) -> float:
        retry_after_seconds = self._parse_retry_after_seconds(response)
        if retry_after_seconds is not None:
            return retry_after_seconds
        return min(
            self.settings.archidekt_retry_base_delay_seconds * (2**attempt),
            8.0,
        )

    def _parse_retry_after_seconds(self, response: httpx.Response) -> float | None:
        headers = getattr(response, "headers", None)
        if not headers:
            return None

        raw_retry_after = headers.get("Retry-After")
        if not isinstance(raw_retry_after, str):
            return None

        try:
            retry_after_seconds = float(raw_retry_after.strip())
        except (TypeError, ValueError):
            return None

        if retry_after_seconds < 0:
            return None
        return retry_after_seconds


class ArchidektPublicCollectionClient(_ArchidektHttpClientBase):
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: RuntimeSettings,
        request_gate: ArchidektRequestGate | None = None,
    ) -> None:
        super().__init__(http_client, settings, request_gate=request_gate)

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

    async def fetch_snapshot(
        self,
        collection: CollectionLocator,
        auth_token: str | None = None,
    ) -> CollectionSnapshot:
        collection_id = await self.resolve_collection_id(collection)
        LOGGER.info(
            "Starting Archidekt collection sync for locator=%s game=%s",
            collection.display_locator,
            collection.game,
        )
        first_page = await self._fetch_collection_page(
            collection_id,
            game=collection.game,
            page=1,
            auth_token=auth_token,
        )
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
                auth_token=auth_token,
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

    async def _fetch_collection_page(
        self,
        collection_id: int,
        game: int,
        page: int,
        auth_token: str | None = None,
    ) -> dict[str, Any]:
        LOGGER.info(
            "Requesting Archidekt collection page collection_id=%s page=%s game=%s",
            collection_id,
            page,
            game,
        )
        response = await self.http_client.get(
            f"{self.settings.normalized_archidekt_base_url}/collection/v2/{collection_id}",
            params={"game": game, "page": page},
            headers=_auth_headers(auth_token),
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
                    card_id=_safe_int(card.get("id")),
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

    async def invalidate_snapshot(self, collection: CollectionLocator) -> None:
        await self._delete_key(self._redis_key(collection.cache_key))


class ArchidektAuthenticatedClient:
    def __init__(self, http_client: httpx.AsyncClient, settings: RuntimeSettings) -> None:
        self.http_client = http_client
        self.settings = settings

    async def login(self, account: ArchidektAccount) -> AuthenticatedAccount:
        if account.token and account.password is None:
            return AuthenticatedAccount(
                token=account.token,
                username=account.username,
                user_id=account.user_id,
            )

        payload: dict[str, Any] = {"password": account.password}
        if account.email:
            payload["email"] = account.email
        else:
            payload["username"] = account.username

        LOGGER.info("Logging into Archidekt with %s", account.display_identity)
        response = await self.http_client.post(
            f"{self.settings.normalized_archidekt_base_url}/api/rest-auth/login/",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()

        response_payload = response.json()
        user = response_payload.get("user") or {}
        token = response_payload.get("token")

        if not token:
            raise RuntimeError("Archidekt login did not return an access token.")

        return AuthenticatedAccount(
            token=str(token),
            username=_compact_text(user.get("username")),
            user_id=_safe_int(user.get("id")),
        )

    async def resolve_account(self, account: ArchidektAccount) -> AuthenticatedAccount:
        if not account.token:
            return await self.login(account)

        if account.username or account.user_id is not None:
            return AuthenticatedAccount(
                token=account.token,
                username=account.username,
                user_id=account.user_id,
            )

        recent_decks = await self._fetch_curated_self(account.token)
        inferred_username = recent_decks[0].owner_username if recent_decks else None
        inferred_user_id = recent_decks[0].owner_id if recent_decks else None
        return AuthenticatedAccount(
            token=account.token,
            username=inferred_username,
            user_id=inferred_user_id,
        )

    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        resolved = await self._coerce_account(account)

        recent_decks = await self._fetch_curated_self(resolved.token)
        if resolved.username is None and recent_decks:
            resolved = resolved.model_copy(
                update={
                    "username": recent_decks[0].owner_username,
                    "user_id": recent_decks[0].owner_id,
                }
            )

        if not resolved.username:
            return resolved, recent_decks

        next_url = (
            f"{self.settings.normalized_archidekt_base_url}/api/decks/v3/?"
            + urlencode(
                {
                    "ownerUsername": resolved.username,
                    "showAll": "true",
                    "page": 1,
                    "pageSize": page_size,
                }
            )
        )
        decks: list[PersonalDeckSummary] = []
        while next_url:
            response = await self.http_client.get(next_url, headers=_auth_headers(resolved.token))
            response.raise_for_status()
            payload = response.json()
            decks.extend(
                self._map_personal_deck_summary(item) for item in (payload.get("results") or [])
            )
            next_url = _normalize_next_url(
                payload.get("next"),
                self.settings.normalized_archidekt_base_url,
            )

        if not decks and recent_decks:
            decks = recent_decks

        return resolved, _dedupe_personal_decks(decks)

    async def search_cards(
        self,
        filters: ArchidektCardSearchFilters,
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None]:
        exact_names = list(dict.fromkeys(filters.exact_name))
        if len(exact_names) <= 1:
            requested_exact_name = exact_names[0] if exact_names else None
            return await self._search_cards_once(filters, requested_exact_name=requested_exact_name)

        searches = await asyncio.gather(
            *[
                self._search_cards_once(filters, requested_exact_name=exact_name)
                for exact_name in exact_names
            ]
        )
        combined_results: list[ArchidektCardReference] = []
        total_matches_parts: list[int] = []
        has_more_flags: list[bool | None] = []

        for mapped, total_matches, has_more in searches:
            combined_results.extend(mapped)
            if total_matches is not None:
                total_matches_parts.append(total_matches)
            has_more_flags.append(has_more)

        combined_total_matches = (
            sum(total_matches_parts) if len(total_matches_parts) == len(searches) else None
        )
        if any(flag is True for flag in has_more_flags):
            combined_has_more: bool | None = True
        elif all(flag is False for flag in has_more_flags):
            combined_has_more = False
        else:
            combined_has_more = None
        return combined_results, combined_total_matches, combined_has_more

    async def _search_cards_once(
        self,
        filters: ArchidektCardSearchFilters,
        requested_exact_name: str | None = None,
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None]:
        params = self._card_search_params(filters, requested_exact_name=requested_exact_name)
        response = await self.http_client.get(
            f"{self.settings.normalized_archidekt_base_url}/api/cards/v2/",
            params=params,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Archidekt card search returned an invalid payload.")

        results = payload.get("results") or []
        mapped = [
            self._map_archidekt_card_reference(item, requested_exact_name=requested_exact_name)
            for item in results
            if isinstance(item, dict)
        ]
        total_matches = _safe_int(payload.get("count"))
        has_more = bool(payload.get("next")) if payload.get("next") is not None else None
        return mapped, total_matches, has_more

    async def fetch_deck_cards(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        resolved = await self._coerce_account(account)
        include_deleted_flag = "1" if include_deleted else "0"
        response = await self.http_client.get(
            f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/v2/cards/",
            params={"includeDeleted": include_deleted_flag},
            headers=_auth_headers(resolved.token),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Archidekt deck cards endpoint returned an invalid payload.")
        return payload

    async def create_deck(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck: PersonalDeckCreateInput,
    ) -> tuple[dict[str, Any], PersonalDeckSummary | None]:
        resolved = await self._coerce_account(account)
        response = await self.http_client.post(
            f"{self.settings.normalized_archidekt_base_url}/api/decks/v2/",
            json=self._deck_create_payload(deck),
            headers=_json_headers(resolved.token),
        )
        response.raise_for_status()
        payload = _ensure_mapping(response.json(), "Archidekt deck create")
        return payload, self._coerce_personal_deck_summary(payload)

    async def update_deck(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
        deck: PersonalDeckUpdateInput,
    ) -> tuple[dict[str, Any], PersonalDeckSummary | None]:
        resolved = await self._coerce_account(account)
        response = await self.http_client.patch(
            f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/update/",
            json=self._deck_update_payload(deck),
            headers=_json_headers(resolved.token),
        )
        response.raise_for_status()
        payload = _ensure_mapping(response.json(), "Archidekt deck update")
        return payload, self._coerce_personal_deck_summary(payload)

    async def delete_deck(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
    ) -> None:
        resolved = await self._coerce_account(account)
        response = await self.http_client.delete(
            f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/",
            headers=_auth_headers(resolved.token),
        )
        response.raise_for_status()

    async def modify_deck_cards(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
    ) -> dict[str, Any]:
        resolved = await self._coerce_account(account)
        payload_cards = [
            self._deck_card_mutation_payload(card, index)
            for index, card in enumerate(cards, start=1)
        ]
        endpoint = f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/modifyCards/v2/"
        headers = _json_headers(resolved.token)
        response = await self.http_client.patch(
            endpoint,
            json={"cards": payload_cards},
            headers=headers,
        )
        if response.status_code < 400:
            return _ensure_mapping(response.json(), "Archidekt deck card modification")

        batch_error = self._remote_error_payload(response)
        if response.status_code != 400 or len(payload_cards) <= 1:
            raise RuntimeError(
                self._format_remote_error(
                    "Archidekt deck card modification",
                    response,
                )
            )

        successful_mutations: list[dict[str, Any]] = []
        failed_mutations: list[dict[str, Any]] = []
        for payload_card in payload_cards:
            single_response = await self.http_client.patch(
                endpoint,
                json={"cards": [payload_card]},
                headers=headers,
            )
            if single_response.status_code < 400:
                successful_mutations.append(
                    {
                        "request": payload_card,
                        "response": _ensure_mapping(
                            single_response.json(),
                            "Archidekt deck card modification",
                        ),
                    }
                )
                continue

            failed_mutations.append(
                {
                    "request": payload_card,
                    "status_code": single_response.status_code,
                    "error": self._remote_error_payload(single_response),
                }
            )

        if not successful_mutations:
            raise RuntimeError(
                "Archidekt rejected the deck card mutation batch and every per-card retry failed. "
                f"Batch error: {batch_error}. "
                f"Per-card errors: {json.dumps(failed_mutations, ensure_ascii=True, separators=(',', ':'))}"
            )

        return {
            "batch_error": batch_error,
            "successful_mutations": successful_mutations,
            "failed_mutations": failed_mutations,
            "successful_count": len(successful_mutations),
            "failed_count": len(failed_mutations),
        }

    async def upsert_collection_entry(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        entry: CollectionCardUpsert,
    ) -> dict[str, Any]:
        resolved = await self._coerce_account(account)
        if entry.record_id is None:
            method = self.http_client.post
            endpoint = f"{self.settings.normalized_archidekt_base_url}/api/collection/v2/"
        else:
            method = self.http_client.patch
            endpoint = (
                f"{self.settings.normalized_archidekt_base_url}/api/collection/v2/{entry.record_id}/"
            )

        response = await method(
            endpoint,
            json=self._collection_upsert_payload(entry),
            headers=_json_headers(resolved.token),
        )
        response.raise_for_status()
        return _ensure_mapping(response.json(), "Archidekt collection upsert")

    async def delete_collection_entries(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        record_ids: list[int],
    ) -> dict[str, Any]:
        resolved = await self._coerce_account(account)
        response = await self.http_client.request(
            "DELETE",
            f"{self.settings.normalized_archidekt_base_url}/api/collection/bulk/",
            content=json.dumps({"ids": [int(record_id) for record_id in record_ids]}),
            headers=_json_headers(resolved.token),
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            return payload
        return {"deleted_ids": [int(record_id) for record_id in record_ids]}

    async def _fetch_curated_self(self, token: str) -> list[PersonalDeckSummary]:
        response = await self.http_client.get(
            f"{self.settings.normalized_archidekt_base_url}/api/decks/curated/self/",
            headers=_auth_headers(token),
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            raw_results = payload.get("results") or payload.get("decks") or []
        elif isinstance(payload, list):
            raw_results = payload
        else:
            raw_results = []

        return [self._map_personal_deck_summary(item) for item in raw_results]

    async def _coerce_account(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
    ) -> AuthenticatedAccount:
        if isinstance(account, AuthenticatedAccount):
            return account
        return await self.resolve_account(account)

    def _map_personal_deck_summary(self, payload: dict[str, Any]) -> PersonalDeckSummary:
        owner = payload.get("owner") or {}
        colors = payload.get("colors") or {}
        return PersonalDeckSummary(
            id=int(payload["id"]),
            name=str(payload.get("name") or ""),
            size=_safe_int(payload.get("size")),
            deck_format=_safe_int(payload.get("deckFormat")),
            edh_bracket=_safe_int(payload.get("edhBracket")),
            private=bool(payload.get("private")),
            unlisted=bool(payload.get("unlisted")),
            theorycrafted=bool(payload.get("theorycrafted")),
            game=_safe_int(payload.get("game")),
            tags=[str(tag) for tag in (payload.get("tags") or []) if tag],
            parent_folder_id=_safe_int(payload.get("parentFolderId")),
            has_primer=bool(payload.get("hasPrimer")),
            created_at=_parse_datetime(payload.get("createdAt")),
            updated_at=_parse_datetime(payload.get("updatedAt")),
            featured=payload.get("featured") or None,
            custom_featured=payload.get("customFeatured") or None,
            owner_id=_safe_int(owner.get("id")),
            owner_username=_compact_text(owner.get("username")),
            colors={
                str(key): int(value)
                for key, value in colors.items()
                if value not in {None, ""} and _safe_int(value) is not None
            },
        )

    def _card_search_params(
        self,
        filters: ArchidektCardSearchFilters,
        requested_exact_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": filters.page, "game": filters.game}
        if requested_exact_name:
            params["name"] = requested_exact_name
            params["exact"] = ""
        elif filters.query:
            params["nameSearch"] = filters.query
        if filters.edition_code:
            params["edition"] = filters.edition_code
        if filters.include_tokens:
            params["includeTokens"] = ""
        if filters.include_digital:
            params["includeDigital"] = ""
        if not filters.all_editions:
            params["unique"] = ""
        return params

    def _map_archidekt_card_reference(
        self,
        payload: dict[str, Any],
        requested_exact_name: str | None = None,
    ) -> ArchidektCardReference:
        oracle_card = payload.get("oracleCard") or {}
        edition = payload.get("edition") or {}
        prices = payload.get("prices") or {}
        return ArchidektCardReference(
            card_id=int(payload["id"]),
            requested_exact_name=requested_exact_name,
            uid=_compact_text(payload.get("uid")),
            oracle_card_id=_safe_int(oracle_card.get("id")),
            oracle_id=_compact_text(oracle_card.get("uid")),
            name=str(oracle_card.get("name") or payload.get("name") or ""),
            display_name=_compact_text(payload.get("displayName")),
            mana_cost=_compact_text(oracle_card.get("manaCost")),
            cmc=_safe_float(oracle_card.get("cmc")),
            oracle_text=_compact_text(oracle_card.get("text")),
            colors=[str(item) for item in (oracle_card.get("colors") or []) if item],
            color_identity=[str(item) for item in (oracle_card.get("colorIdentity") or []) if item],
            supertypes=[str(item) for item in (oracle_card.get("superTypes") or []) if item],
            types=[str(item) for item in (oracle_card.get("types") or []) if item],
            subtypes=[str(item) for item in (oracle_card.get("subTypes") or []) if item],
            set_code=_compact_text(edition.get("editioncode")),
            set_name=_compact_text(edition.get("editionname")),
            rarity=_compact_text(payload.get("rarity")),
            released_at=_parse_datetime(payload.get("releasedAt")),
            prices={str(key): _safe_float(value) for key, value in prices.items()},
            owned=_safe_int(payload.get("owned")),
            default_category=_compact_text(oracle_card.get("defaultCategory")),
        )

    def _coerce_personal_deck_summary(self, payload: dict[str, Any]) -> PersonalDeckSummary | None:
        candidate = payload
        if isinstance(payload.get("deck"), dict):
            candidate = payload["deck"]
        elif isinstance(payload.get("result"), dict):
            candidate = payload["result"]

        if not isinstance(candidate, dict) or candidate.get("id") in {None, ""}:
            return None

        try:
            return self._map_personal_deck_summary(candidate)
        except Exception:
            return None

    def _deck_create_payload(self, deck: PersonalDeckCreateInput) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": deck.name,
            "deckFormat": deck.deck_format,
            "edhBracket": deck.edh_bracket,
            "description": deck.description or "",
            "featured": deck.featured,
            "playmat": deck.playmat,
            "copyId": deck.copy_id,
            "private": deck.private,
            "unlisted": deck.unlisted,
            "theorycrafted": deck.theorycrafted,
            "game": deck.game,
            "parent_folder": deck.parent_folder_id,
            "cardPackage": deck.card_package,
            "extras": deck.extras,
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _deck_update_payload(self, deck: PersonalDeckUpdateInput) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": deck.name,
            "deckFormat": deck.deck_format,
            "edhBracket": deck.edh_bracket,
            "description": deck.description,
            "featured": deck.featured,
            "playmat": deck.playmat,
            "copyId": deck.copy_id,
            "private": deck.private,
            "unlisted": deck.unlisted,
            "theorycrafted": deck.theorycrafted,
            "game": deck.game,
            "parent_folder": deck.parent_folder_id,
            "cardPackage": deck.card_package,
            "extras": deck.extras,
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _deck_card_mutation_payload(
        self,
        card: PersonalDeckCardMutation,
        index: int,
    ) -> dict[str, Any]:
        patch_id = (
            card.patch_id
            or f"mcp-{index}-{card.card_id or card.custom_card_id or card.deck_relation_id or 'card'}"
        )
        normalized_action = card.action
        normalized_categories = list(card.categories)
        modifications = {
            key: value
            for key, value in {
                "quantity": card.modifications.quantity,
                "modifier": card.modifications.modifier,
                "customCmc": card.modifications.custom_cmc,
                "companion": card.modifications.companion,
                "flippedDefault": card.modifications.flipped_default,
                "label": card.modifications.label,
            }.items()
            if value is not None
        }
        if card.action == "modify" and card.modifications.quantity == 0:
            normalized_action = "remove"
            normalized_categories = []
            modifications = {}
        payload: dict[str, Any] = {
            "action": normalized_action,
            "patchId": patch_id,
        }
        if normalized_categories:
            payload["categories"] = normalized_categories
        if modifications:
            payload["modifications"] = modifications
        if card.card_id is not None:
            payload["cardid"] = card.card_id
        if card.custom_card_id is not None:
            payload["customCardId"] = card.custom_card_id
        if card.deck_relation_id is not None:
            payload["deckRelationId"] = card.deck_relation_id
        return payload

    def _remote_error_payload(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        except Exception:
            return (response.text or "").strip() or f"HTTP {response.status_code}"

    def _format_remote_error(self, context: str, response: httpx.Response) -> str:
        return (
            f"{context} failed with HTTP {response.status_code}: "
            f"{self._remote_error_payload(response)}"
        )

    def _collection_upsert_payload(self, entry: CollectionCardUpsert) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game": entry.game,
            "id": entry.record_id,
            "quantity": entry.quantity,
            "card": entry.card_id,
            "modifier": entry.modifier,
            "language": entry.language,
            "condition": entry.condition,
            "tags": entry.tags,
            "purchasePrice": entry.purchase_price,
        }
        return {key: value for key, value in payload.items() if value is not None}


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


def _compact_text(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    return compact or None


def _ensure_mapping(payload: Any, context: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"{context} endpoint returned an invalid payload.")


def _normalize_next_url(raw_url: Any, base_url: str) -> str | None:
    if not raw_url:
        return None
    value = str(raw_url).replace("http://", "https://", 1)
    if value.startswith("/"):
        return base_url + value
    return value


def _dedupe_personal_decks(decks: list[PersonalDeckSummary]) -> list[PersonalDeckSummary]:
    deduped: dict[int, PersonalDeckSummary] = {}
    default_timestamp = datetime(1970, 1, 1, tzinfo=UTC).timestamp()
    for deck in decks:
        deduped[deck.id] = deck
    return sorted(
        deduped.values(),
        key=lambda item: (
            -(item.updated_at.timestamp() if item.updated_at else default_timestamp),
            item.name.casefold(),
        ),
    )


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
        card_id=_safe_int(payload.get("card_id")),
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


def serialize_collection_snapshot(snapshot: CollectionSnapshot) -> dict[str, Any]:
    return _serialize_snapshot(snapshot)


def deserialize_collection_snapshot(payload: dict[str, Any]) -> CollectionSnapshot:
    return _deserialize_snapshot(payload)
