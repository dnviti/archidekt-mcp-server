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

RECENT_COLLECTION_WRITE_MARKER_TTL_SECONDS = 120
PERSONAL_DECK_CACHE_REFRESH_MARKER_TTL_SECONDS = 120


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
    del service
    digest = hashlib.sha256(account.token.encode("utf-8")).hexdigest()
    return f"authenticated-deck-list:token:{digest[:16]}"


def _authenticated_deck_list_account_scope_key(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> str | None:
    del service
    if account.user_id is not None:
        return f"user:{account.user_id}"
    if account.username:
        return f"username:{account.username.casefold()}"
    return None


def _track_authenticated_deck_list_cache_key(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    cache_key: str,
) -> None:
    scope_key = _authenticated_deck_list_account_scope_key(service, account)
    if scope_key is None:
        return
    service._authenticated_deck_list_cache_index.setdefault(scope_key, set()).add(cache_key)


def _personal_deck_cache_refresh_marker_keys(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    family: str,
    deck_list_cache_key: str | None = None,
) -> set[str]:
    keys: set[str]
    if family == "deck-list":
        keys = {f"deck-list:{deck_list_cache_key or _private_authenticated_deck_list_cache_key(service, account)}"}
    elif family == "usage":
        keys = {f"usage:{_private_usage_cache_key(service, account)}"}
    else:
        keys = _personal_deck_cache_refresh_marker_keys(service, account, "deck-list", deck_list_cache_key)
        keys.update(_personal_deck_cache_refresh_marker_keys(service, account, "usage", deck_list_cache_key))
        return keys
    scope_key = _authenticated_deck_list_account_scope_key(service, account)
    if scope_key is not None:
        keys.add(f"scope:{family}:{scope_key}")
    return keys


def _mark_personal_deck_cache_refresh(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    family: str = "all",
    deck_list_cache_key: str | None = None,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(seconds=PERSONAL_DECK_CACHE_REFRESH_MARKER_TTL_SECONDS)
    for marker_key in _personal_deck_cache_refresh_marker_keys(service, account, family, deck_list_cache_key):
        service._personal_deck_cache_refresh_markers[marker_key] = expires_at


def _has_personal_deck_cache_refresh_marker(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    family: str,
    deck_list_cache_key: str | None = None,
) -> bool:
    now = datetime.now(UTC)
    has_marker = False
    for marker_key in _personal_deck_cache_refresh_marker_keys(service, account, family, deck_list_cache_key):
        expires_at = service._personal_deck_cache_refresh_markers.get(marker_key)
        if expires_at is None:
            continue
        if expires_at <= now:
            service._personal_deck_cache_refresh_markers.pop(marker_key, None)
            continue
        has_marker = True
    return has_marker


def _clear_personal_deck_cache_refresh(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    family: str,
    deck_list_cache_key: str | None = None,
) -> None:
    for marker_key in _personal_deck_cache_refresh_marker_keys(service, account, family, deck_list_cache_key):
        service._personal_deck_cache_refresh_markers.pop(marker_key, None)


def _authenticated_deck_list_index_redis_key(
    service: DeckbuildingService,
    scope_key: str,
) -> str:
    return _private_redis_key(service, "authenticated-deck-list-index-v2", scope_key)


def _legacy_authenticated_deck_list_index_redis_key(
    service: DeckbuildingService,
    scope_key: str,
) -> str:
    return _private_redis_key(service, "authenticated-deck-list-index", scope_key)


async def _load_legacy_authenticated_deck_list_index(
    service: DeckbuildingService,
    scope_key: str,
) -> set[str]:
    legacy_index_key = _legacy_authenticated_deck_list_index_redis_key(service, scope_key)
    try:
        existing_payload = await service.redis_client.get(legacy_index_key)
    except RedisError as error:
        LOGGER.warning(
            "Failed to read legacy authenticated deck-list cache index for %s: %s",
            scope_key,
            error,
        )
        return set()
    if not existing_payload:
        return set()
    try:
        return {
            str(key)
            for key in json.loads(existing_payload)
            if isinstance(key, str) and key
        }
    except Exception as error:
        LOGGER.warning(
            "Failed to decode legacy authenticated deck-list cache index for %s: %s",
            scope_key,
            error,
        )
        await _delete_private_redis_key(service, legacy_index_key)
        return set()


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
        if not force_refresh and _has_personal_deck_cache_refresh_marker(service, account, "deck-list", cache_key):
            force_refresh = True
        if not force_refresh:
            cached_redis, use_memory_fallback = await _load_authenticated_deck_list_from_redis(
                service,
                cache_key,
            )
            if cached_redis is not None:
                ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
                if ttl_seconds > 0:
                    service._authenticated_deck_list_cache[cache_key] = (
                        datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                        cached_redis,
                    )
                    _track_authenticated_deck_list_cache_key(service, cached_redis.account, cache_key)
                return cached_redis.account, cached_redis.decks
            if use_memory_fallback:
                cached = _load_private_memory_cache(
                    service,
                    service._authenticated_deck_list_cache,
                    cache_key,
                )
                if cached is not None:
                    _track_authenticated_deck_list_cache_key(service, cached.account, cache_key)
                    return cached.account, cached.decks

        resolved_account, decks = await service.auth_client.list_personal_decks(account)
        snapshot = AuthenticatedDeckListSnapshot(
            account=resolved_account,
            decks=_deduplicate_personal_decks(service, decks),
            fetched_at=datetime.now(UTC),
        )
        ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
        if ttl_seconds > 0:
            ttl = timedelta(seconds=ttl_seconds)
            service._authenticated_deck_list_cache[cache_key] = (datetime.now(UTC) + ttl, snapshot)
            _track_authenticated_deck_list_cache_key(service, snapshot.account, cache_key)
            await _store_authenticated_deck_list_in_redis(service, cache_key, snapshot)
        _clear_personal_deck_cache_refresh(service, snapshot.account, "deck-list", cache_key)
        return snapshot.account, snapshot.decks


async def _load_authenticated_deck_list_from_redis(
    service: DeckbuildingService,
    cache_key: str,
) -> tuple[AuthenticatedDeckListSnapshot | None, bool]:
    ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
    if ttl_seconds <= 0:
        return None, False

    redis_key = _private_redis_key(service, "authenticated-deck-list", cache_key)
    try:
        data = await service.redis_client.get(redis_key)
    except RedisError as error:
        LOGGER.warning(
            "Redis authenticated deck-list cache read failed for %s; proceeding with memory fallback: %s",
            cache_key,
            error,
        )
        return None, True

    if not data:
        return None, False

    try:
        obj = json.loads(data)
        return (
            AuthenticatedDeckListSnapshot(
                account=AuthenticatedAccount.model_validate(obj["account"]),
                decks=[PersonalDeckSummary.model_validate(d) for d in obj["decks"]],
                fetched_at=datetime.fromisoformat(obj["fetched_at"]),
            ),
            False,
        )
    except Exception as error:
        LOGGER.warning(
            "Failed to decode Redis authenticated deck-list cache for %s: %s",
            cache_key,
            error,
        )
        await _delete_private_redis_key(service, redis_key)
        return None, False


async def _store_authenticated_deck_list_in_redis(
    service: DeckbuildingService,
    cache_key: str,
    snapshot: AuthenticatedDeckListSnapshot,
) -> None:
    ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
    if ttl_seconds <= 0:
        return

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
            ex=ttl_seconds,
        )
        scope_key = _authenticated_deck_list_account_scope_key(service, snapshot.account)
        if scope_key is not None:
            redis_index_key = _authenticated_deck_list_index_redis_key(service, scope_key)
            await service.redis_client.execute_command("SADD", redis_index_key, cache_key)
            await service.redis_client.execute_command("EXPIRE", redis_index_key, ttl_seconds)
    except RedisError as error:
        LOGGER.warning(
            "Failed to persist authenticated deck-list cache for %s: %s",
            cache_key,
            error,
        )


async def _invalidate_authenticated_deck_list_cache(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> None:
    cache_keys = {_private_authenticated_deck_list_cache_key(service, account)}
    scope_key = _authenticated_deck_list_account_scope_key(service, account)
    if scope_key is not None:
        cache_keys.update(service._authenticated_deck_list_cache_index.pop(scope_key, set()))
    for cache_key in cache_keys:
        service._authenticated_deck_list_cache.pop(cache_key, None)
    try:
        redis_index_keys: list[str] = []
        if scope_key is not None:
            redis_index_key = _authenticated_deck_list_index_redis_key(service, scope_key)
            redis_index_keys.append(redis_index_key)
            raw_members = await service.redis_client.execute_command("SMEMBERS", redis_index_key)
            if isinstance(raw_members, (set, list, tuple)):
                cache_keys.update(
                    str(key)
                    for key in raw_members
                    if isinstance(key, str) and key
                )
            legacy_cache_keys = await _load_legacy_authenticated_deck_list_index(service, scope_key)
            if legacy_cache_keys:
                cache_keys.update(legacy_cache_keys)
                redis_index_keys.append(_legacy_authenticated_deck_list_index_redis_key(service, scope_key))
        for cache_key in cache_keys:
            service._authenticated_deck_list_cache.pop(cache_key, None)
        redis_keys = [
            _private_redis_key(service, "authenticated-deck-list", cache_key)
            for cache_key in cache_keys
        ]
        redis_keys.extend(redis_index_keys)
        if redis_keys:
            await service.redis_client.delete(*redis_keys)
    except RedisError as error:
        LOGGER.warning(
            "Failed to invalidate authenticated deck-list cache for %s: %s",
            scope_key or "token-scoped entry",
            error,
        )


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
    cached_value, use_memory_fallback = await _load_private_redis_cache(
        service,
        namespace,
        key,
        deserializer,
    )
    if cached_value is not None:
        _store_private_memory_cache(service, cache, key, cached_value)
        return cached_value
    if use_memory_fallback:
        return _load_private_memory_cache(service, cache, key)
    return None


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
) -> tuple[Any | None, bool]:
    ttl_seconds = service.settings.personal_deck_cache_ttl_seconds
    if ttl_seconds <= 0:
        return None, False

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
        return None, True

    try:
        if not payload:
            return None, False

        wrapper = json.loads(payload)
        cached_payload = wrapper.get("payload")
        if not isinstance(cached_payload, dict):
            raise ValueError("incomplete private cache payload")

        value = deserializer(cached_payload)
        LOGGER.info("Using Redis private cache for %s:%s", namespace, key)
        return value, False
    except RedisError as error:
        LOGGER.warning(
            "Redis private cache metadata read failed for %s:%s; proceeding without cache: %s",
            namespace,
            key,
            error,
        )
        return None, True
    except Exception as error:
        LOGGER.warning(
            "Failed to decode Redis private cache for %s:%s: %s",
            namespace,
            key,
            error,
        )
        await _delete_private_redis_key(service, redis_key)
        return None, False


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


async def _mark_recent_collection_write(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    games: set[int],
) -> None:
    marked_at = datetime.now(UTC)
    for game in games:
        marker_key = _collection_write_marker_key(service, account, game)
        service._recent_collection_write_markers[marker_key] = marked_at
        try:
            await service.redis_client.set(
                _private_redis_key(service, "recent-collection-write", marker_key),
                marked_at.isoformat(),
                ex=RECENT_COLLECTION_WRITE_MARKER_TTL_SECONDS,
            )
        except RedisError as error:
            LOGGER.warning(
                "Failed to persist recent collection write marker for %s: %s",
                marker_key,
                error,
            )


async def _consume_recent_collection_write(
    service: DeckbuildingService,
    collection: CollectionLocator,
    account: AuthenticatedAccount,
) -> bool:
    if not _is_self_collection_locator(service, collection, account):
        return False
    marker_key = _collection_write_marker_key(service, account, collection.game)
    marked_at = service._recent_collection_write_markers.get(marker_key)
    local_recent = False
    if marked_at is not None and (datetime.now(UTC) - marked_at) > timedelta(
        seconds=RECENT_COLLECTION_WRITE_MARKER_TTL_SECONDS
    ):
        service._recent_collection_write_markers.pop(marker_key, None)
    elif marked_at is not None:
        local_recent = True
    try:
        redis_marker = await service.redis_client.get(
            _private_redis_key(service, "recent-collection-write", marker_key)
        )
    except RedisError as error:
        LOGGER.warning(
            "Failed to read recent collection write marker for %s: %s",
            marker_key,
            error,
        )
        return local_recent
    return local_recent or redis_marker is not None


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
    _mark_personal_deck_cache_refresh(service, account, "all")
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
