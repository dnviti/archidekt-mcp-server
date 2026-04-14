# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from redis.exceptions import RedisError

from ..schemas.accounts import AuthenticatedAccount, CollectionLocator
from ..schemas.decks import PersonalDeckSummary
from .deck_usage import AuthenticatedDeckListSnapshot

if TYPE_CHECKING:
    from .deckbuilding import DeckbuildingService

LOGGER = logging.getLogger("archidekt_commander_mcp.server")


def _lock_for_key(service: DeckbuildingService, key: str) -> asyncio.Lock:
    return service._locks.setdefault(key, asyncio.Lock())


def _private_account_cache_key(service: DeckbuildingService, account: AuthenticatedAccount) -> str:
    del service
    if account.user_id is not None:
        return f"user:{account.user_id}"
    if account.username:
        return f"username:{account.username.casefold()}"
    digest = hashlib.sha256(account.token.encode("utf-8")).hexdigest()
    return f"token:{digest[:16]}"


def _private_snapshot_cache_key(
    service: DeckbuildingService,
    collection: CollectionLocator,
    account: AuthenticatedAccount,
) -> str:
    return f"private-collection:{collection.cache_key}:{_private_account_cache_key(service, account)}"


def _private_usage_cache_key(service: DeckbuildingService, account: AuthenticatedAccount) -> str:
    return f"private-decks:{_private_account_cache_key(service, account)}"


def _private_authenticated_deck_list_cache_key(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> str:
    return f"authenticated-deck-list:{_private_account_cache_key(service, account)}"


def _deduplicate_personal_decks(
    service: DeckbuildingService,
    decks: list[PersonalDeckSummary],
) -> list[PersonalDeckSummary]:
    del service
    deduplicated: list[PersonalDeckSummary] = []
    seen_ids: set[int] = set()
    for deck in decks:
        if deck.id in seen_ids:
            continue
        seen_ids.add(deck.id)
        deduplicated.append(deck)
    return deduplicated


async def _get_authenticated_deck_list(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    force_refresh: bool = False,
) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
    cache_key = _private_authenticated_deck_list_cache_key(service, account)
    async with _lock_for_key(service, cache_key):
        if not force_refresh:
            cached = _load_private_memory_cache(
                service,
                service._authenticated_deck_list_cache,
                cache_key,
            )
            if cached is not None:
                return cached.account, cached.decks
            cached_redis = await _load_authenticated_deck_list_from_redis(service, cache_key)
            if cached_redis is not None:
                service._authenticated_deck_list_cache[cache_key] = (
                    datetime.now(UTC) + timedelta(seconds=service.settings.personal_deck_cache_ttl_seconds),
                    cached_redis,
                )
                return cached_redis.account, cached_redis.decks

        resolved_account, decks = await service.auth_client.list_personal_decks(account)
        snapshot = AuthenticatedDeckListSnapshot(
            account=resolved_account,
            decks=_deduplicate_personal_decks(service, decks),
            fetched_at=datetime.now(UTC),
        )
        ttl = timedelta(seconds=service.settings.personal_deck_cache_ttl_seconds)
        service._authenticated_deck_list_cache[cache_key] = (datetime.now(UTC) + ttl, snapshot)
        await _store_authenticated_deck_list_in_redis(service, cache_key, snapshot)
        return snapshot.account, snapshot.decks


async def _load_authenticated_deck_list_from_redis(
    service: DeckbuildingService,
    cache_key: str,
) -> AuthenticatedDeckListSnapshot | None:
    try:
        redis_key = _private_redis_key(service, "authenticated-deck-list", cache_key)
        data = await service.redis_client.get(redis_key)
        if not data:
            return None
        obj = json.loads(data)
        return AuthenticatedDeckListSnapshot(
            account=AuthenticatedAccount.model_validate(obj["account"]),
            decks=[PersonalDeckSummary.model_validate(d) for d in obj["decks"]],
            fetched_at=datetime.fromisoformat(obj["fetched_at"]),
        )
    except Exception:
        return None


async def _store_authenticated_deck_list_in_redis(
    service: DeckbuildingService,
    cache_key: str,
    snapshot: AuthenticatedDeckListSnapshot,
) -> None:
    try:
        redis_key = _private_redis_key(service, "authenticated-deck-list", cache_key)
        data = json.dumps({
            "account": snapshot.account.model_dump(mode="json"),
            "decks": [d.model_dump(mode="json") for d in snapshot.decks],
            "fetched_at": snapshot.fetched_at.isoformat(),
        })
        await service.redis_client.set(
            redis_key,
            data,
            ex=service.settings.personal_deck_cache_ttl_seconds,
        )
    except Exception:
        pass


async def _invalidate_authenticated_deck_list_cache(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> None:
    cache_key = _private_authenticated_deck_list_cache_key(service, account)
    service._authenticated_deck_list_cache.pop(cache_key, None)
    try:
        redis_key = _private_redis_key(service, "authenticated-deck-list", cache_key)
        await service.redis_client.delete(redis_key)
    except Exception:
        pass


def _collection_write_marker_key(service: DeckbuildingService, account: AuthenticatedAccount, game: int) -> str:
    return f"{_private_account_cache_key(service, account)}:game:{game}"


def _private_redis_key(service: DeckbuildingService, namespace: str, cache_key: str) -> str:
    return f"{service.settings.redis_key_prefix}:private:{namespace}:{cache_key}"


def _load_private_memory_cache(
    service: DeckbuildingService,
    cache: dict[str, tuple[datetime, Any]],
    key: str,
) -> Any | None:
    del service
    entry = cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if expires_at <= datetime.now(UTC):
        cache.pop(key, None)
        return None
    return value


def _store_private_memory_cache(
    service: DeckbuildingService,
    cache: dict[str, tuple[datetime, Any]],
    key: str,
    value: Any,
) -> None:
    ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
    if ttl_seconds <= 0:
        return
    cache[key] = (datetime.now(UTC) + timedelta(seconds=ttl_seconds), value)


async def _load_private_cache(
    service: DeckbuildingService,
    cache: dict[str, tuple[datetime, Any]],
    namespace: str,
    key: str,
    deserializer: Callable[[dict[str, Any]], Any],
) -> Any | None:
    cached_value = await _load_private_redis_cache(service, namespace, key, deserializer)
    if cached_value is not None:
        _store_private_memory_cache(service, cache, key, cached_value)
        return cached_value
    return _load_private_memory_cache(service, cache, key)


async def _store_private_cache(
    service: DeckbuildingService,
    cache: dict[str, tuple[datetime, Any]],
    namespace: str,
    key: str,
    value: Any,
    serializer: Callable[[Any], dict[str, Any]],
) -> None:
    _store_private_memory_cache(service, cache, key, value)
    await _store_private_redis_cache(service, namespace, key, value, serializer)


async def _load_private_redis_cache(
    service: DeckbuildingService,
    namespace: str,
    key: str,
    deserializer: Callable[[dict[str, Any]], Any],
) -> Any | None:
    ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
    if ttl_seconds <= 0:
        return None

    redis_key = _private_redis_key(service, namespace, key)
    try:
        payload = await service.redis_client.get(redis_key)
    except RedisError as error:
        LOGGER.warning(
            "Redis private cache read failed for %s:%s; proceeding without cache: %s",
            namespace,
            key,
            error,
        )
        return None

    try:
        if not payload:
            return None

        wrapper = json.loads(payload)
        cached_payload = wrapper.get("payload")
        if not isinstance(cached_payload, dict):
            raise ValueError("incomplete private cache payload")

        value = deserializer(cached_payload)
        ttl = await _private_redis_ttl(service, redis_key)
        if ttl is not None:
            LOGGER.info(
                "Using Redis private cache for %s:%s; expires in %ss",
                namespace,
                key,
                ttl,
            )
        else:
            LOGGER.info("Using Redis private cache for %s:%s", namespace, key)
        return value
    except RedisError as error:
        LOGGER.warning(
            "Redis private cache metadata read failed for %s:%s; proceeding without cache: %s",
            namespace,
            key,
            error,
        )
        return None
    except Exception as error:
        LOGGER.warning(
            "Failed to decode Redis private cache for %s:%s: %s",
            namespace,
            key,
            error,
        )
        await _delete_private_redis_key(service, redis_key)
        return None


async def _store_private_redis_cache(
    service: DeckbuildingService,
    namespace: str,
    key: str,
    value: Any,
    serializer: Callable[[Any], dict[str, Any]],
) -> None:
    ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
    if ttl_seconds <= 0:
        return

    redis_key = _private_redis_key(service, namespace, key)
    payload = {
        "saved_at": datetime.now(UTC).isoformat(),
        "payload": serializer(value),
    }
    try:
        await service.redis_client.set(
            redis_key,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            ex=ttl_seconds,
        )
    except RedisError as error:
        LOGGER.warning(
            "Redis private cache write failed for %s:%s; continuing without persisted cache: %s",
            namespace,
            key,
            error,
        )


async def _private_redis_ttl(service: DeckbuildingService, redis_key: str) -> int | None:
    ttl = await service.redis_client.ttl(redis_key)
    if isinstance(ttl, int) and ttl >= 0:
        return ttl
    if isinstance(ttl, int) and ttl == -1:
        return None
    return None


async def _delete_private_redis_key(service: DeckbuildingService, redis_key: str) -> None:
    try:
        await service.redis_client.delete(redis_key)
    except RedisError as error:
        LOGGER.warning("Failed to delete invalid Redis private cache key %s: %s", redis_key, error)


def _account_collection_locators(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    games: set[int] | None = None,
) -> list[CollectionLocator]:
    del service
    target_games = sorted(games or {1, 2, 3})
    locators: dict[str, CollectionLocator] = {}
    for game in target_games:
        if account.user_id is not None:
            locator = CollectionLocator(collection_id=account.user_id, game=game)
            locators[locator.cache_key] = locator
        if account.username:
            locator = CollectionLocator(username=account.username, game=game)
            locators[locator.cache_key] = locator
    return list(locators.values())


def _is_self_collection_locator(
    service: DeckbuildingService,
    collection: CollectionLocator,
    account: AuthenticatedAccount,
) -> bool:
    valid_cache_keys = {
        locator.cache_key
        for locator in _account_collection_locators(service, account, games={collection.game})
    }
    return collection.cache_key in valid_cache_keys


def _mark_recent_collection_write(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    games: set[int],
) -> None:
    marked_at = datetime.now(UTC)
    for game in games:
        service._recent_collection_write_markers[
            _collection_write_marker_key(service, account, game)
        ] = marked_at


def _consume_recent_collection_write(
    service: DeckbuildingService,
    collection: CollectionLocator,
    account: AuthenticatedAccount,
) -> bool:
    if not _is_self_collection_locator(service, collection, account):
        return False
    marker_key = _collection_write_marker_key(service, account, collection.game)
    marked_at = service._recent_collection_write_markers.get(marker_key)
    if marked_at is None:
        return False
    if (datetime.now(UTC) - marked_at) > timedelta(minutes=2):
        service._recent_collection_write_markers.pop(marker_key, None)
        return False
    service._recent_collection_write_markers.pop(marker_key, None)
    return True


async def _invalidate_personal_deck_usage_cache(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> None:
    cache_key = _private_usage_cache_key(service, account)
    service._personal_deck_usage_cache.pop(cache_key, None)
    await _delete_private_redis_key(service, _private_redis_key(service, "personal-decks", cache_key))


async def _invalidate_personal_deck_caches(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> None:
    await _invalidate_authenticated_deck_list_cache(service, account)
    await _invalidate_personal_deck_usage_cache(service, account)


async def _invalidate_collection_caches(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    games: set[int] | None = None,
) -> None:
    for locator in _account_collection_locators(service, account, games):
        private_key = _private_snapshot_cache_key(service, locator, account)
        service._private_snapshot_cache.pop(private_key, None)
        await _delete_private_redis_key(service, _private_redis_key(service, "collection", private_key))
        await service.cache.invalidate_snapshot(locator)
