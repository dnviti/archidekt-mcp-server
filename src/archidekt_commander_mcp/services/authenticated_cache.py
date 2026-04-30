from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import RuntimeSettings
from ..schemas.accounts import AuthenticatedAccount, CollectionLocator
from ..schemas.decks import PersonalDeckSummary
from .deck_usage import (
    AuthenticatedDeckListSnapshot,
    PersonalDeckUsageSnapshot,
    _get_personal_deck_usage_snapshot,
)
from .snapshot_cache import (
    _account_collection_locators,
    _clear_personal_deck_cache_refresh,
    _collection_write_marker_key,
    _consume_recent_collection_write,
    _deduplicate_personal_decks,
    _delete_private_redis_key,
    _get_authenticated_deck_list,
    _has_personal_deck_cache_refresh_marker,
    _invalidate_authenticated_deck_list_cache,
    _invalidate_collection_caches,
    _invalidate_personal_deck_caches,
    _invalidate_personal_deck_usage_cache,
    _is_self_collection_locator,
    _load_authenticated_deck_list_from_redis,
    _load_private_cache,
    _load_private_memory_cache,
    _load_private_redis_cache,
    _lock_for_key,
    _mark_personal_deck_cache_refresh,
    _mark_recent_collection_write,
    _private_account_cache_key,
    _private_authenticated_deck_list_cache_key,
    _private_redis_key,
    _private_snapshot_cache_key,
    _private_usage_cache_key,
    _store_authenticated_deck_list_in_redis,
    _store_private_cache,
    _store_private_memory_cache,
    _store_private_redis_cache,
)


class AuthenticatedCache:
    def __init__(
        self,
        *,
        settings: RuntimeSettings,
        redis_client: Callable[[], Any],
        auth_client: Callable[[], Any],
        collection_cache: Callable[[], Any],
    ) -> None:
        self.settings = settings
        self._redis_client = redis_client
        self._auth_client = auth_client
        self._collection_cache = collection_cache
        self._locks: dict[str, asyncio.Lock] = {}
        self._private_snapshot_cache: dict[str, tuple[datetime, Any]] = {}
        self._authenticated_deck_list_cache: dict[str, tuple[datetime, AuthenticatedDeckListSnapshot]] = {}
        self._authenticated_deck_list_cache_index: dict[str, set[str]] = {}
        self._personal_deck_cache_refresh_markers: dict[str, datetime] = {}
        self._personal_deck_usage_cache: dict[str, tuple[datetime, PersonalDeckUsageSnapshot]] = {}
        self._recent_collection_write_markers: dict[str, datetime] = {}

    @property
    def auth_client(self) -> Any:
        return self._auth_client()

    @property
    def redis_client(self) -> Any:
        return self._redis_client()

    @property
    def cache(self) -> Any:
        return self._collection_cache()

    @property
    def _adapter(self) -> Any:
        return self

    def lock_for_key(self, key: str) -> asyncio.Lock:
        return _lock_for_key(self._adapter, key)

    def _lock_for_key(self, key: str) -> asyncio.Lock:
        return self.lock_for_key(key)

    def private_account_cache_key(self, account: AuthenticatedAccount) -> str:
        return _private_account_cache_key(self._adapter, account)

    def private_snapshot_cache_key(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> str:
        return _private_snapshot_cache_key(self._adapter, collection, account)

    def private_usage_cache_key(self, account: AuthenticatedAccount) -> str:
        return _private_usage_cache_key(self._adapter, account)

    def _private_usage_cache_key(self, account: AuthenticatedAccount) -> str:
        return self.private_usage_cache_key(account)

    def private_authenticated_deck_list_cache_key(self, account: AuthenticatedAccount) -> str:
        return _private_authenticated_deck_list_cache_key(self._adapter, account)

    def deduplicate_personal_decks(self, decks: list[PersonalDeckSummary]) -> list[PersonalDeckSummary]:
        return _deduplicate_personal_decks(self._adapter, decks)

    def mark_personal_deck_cache_refresh(
        self,
        account: AuthenticatedAccount,
        family: str = "all",
        deck_list_cache_key: str | None = None,
    ) -> None:
        _mark_personal_deck_cache_refresh(self._adapter, account, family, deck_list_cache_key)

    def has_personal_deck_cache_refresh_marker(
        self,
        account: AuthenticatedAccount,
        family: str,
        deck_list_cache_key: str | None = None,
    ) -> bool:
        return _has_personal_deck_cache_refresh_marker(self._adapter, account, family, deck_list_cache_key)

    def _has_personal_deck_cache_refresh_marker(
        self,
        account: AuthenticatedAccount,
        family: str,
        deck_list_cache_key: str | None = None,
    ) -> bool:
        return self.has_personal_deck_cache_refresh_marker(account, family, deck_list_cache_key)

    def clear_personal_deck_cache_refresh(
        self,
        account: AuthenticatedAccount,
        family: str,
        deck_list_cache_key: str | None = None,
    ) -> None:
        _clear_personal_deck_cache_refresh(self._adapter, account, family, deck_list_cache_key)

    def _clear_personal_deck_cache_refresh(
        self,
        account: AuthenticatedAccount,
        family: str,
        deck_list_cache_key: str | None = None,
    ) -> None:
        self.clear_personal_deck_cache_refresh(account, family, deck_list_cache_key)

    async def get_authenticated_deck_list(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        return await _get_authenticated_deck_list(self._adapter, account, force_refresh)

    async def _get_authenticated_deck_list(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        return await self.get_authenticated_deck_list(account, force_refresh)

    async def load_authenticated_deck_list_from_redis(
        self,
        cache_key: str,
    ) -> tuple[AuthenticatedDeckListSnapshot | None, bool]:
        return await _load_authenticated_deck_list_from_redis(self._adapter, cache_key)

    async def store_authenticated_deck_list_in_redis(
        self,
        cache_key: str,
        snapshot: AuthenticatedDeckListSnapshot,
    ) -> None:
        await _store_authenticated_deck_list_in_redis(self._adapter, cache_key, snapshot)

    async def invalidate_authenticated_deck_list_cache(self, account: AuthenticatedAccount) -> None:
        await _invalidate_authenticated_deck_list_cache(self._adapter, account)

    def collection_write_marker_key(self, account: AuthenticatedAccount, game: int) -> str:
        return _collection_write_marker_key(self._adapter, account, game)

    def private_redis_key(self, namespace: str, cache_key: str) -> str:
        return _private_redis_key(self._adapter, namespace, cache_key)

    def load_private_memory_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        key: str,
    ) -> Any | None:
        return _load_private_memory_cache(self._adapter, cache, key)

    def store_private_memory_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        key: str,
        value: Any,
    ) -> None:
        _store_private_memory_cache(self._adapter, cache, key, value)

    async def load_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        return await _load_private_cache(self._adapter, cache, namespace, key, deserializer)

    async def _load_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        return await self.load_private_cache(cache, namespace, key, deserializer)

    async def store_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await _store_private_cache(self._adapter, cache, namespace, key, value, serializer)

    async def _store_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await self.store_private_cache(cache, namespace, key, value, serializer)

    async def load_private_redis_cache(
        self,
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> tuple[Any | None, bool]:
        return await _load_private_redis_cache(self._adapter, namespace, key, deserializer)

    async def store_private_redis_cache(
        self,
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await _store_private_redis_cache(self._adapter, namespace, key, value, serializer)

    async def delete_private_redis_key(self, redis_key: str) -> None:
        await _delete_private_redis_key(self._adapter, redis_key)

    def account_collection_locators(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> list[CollectionLocator]:
        return _account_collection_locators(self._adapter, account, games)

    def is_self_collection_locator(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        return _is_self_collection_locator(self._adapter, collection, account)

    async def mark_recent_collection_write(
        self,
        account: AuthenticatedAccount,
        games: set[int],
    ) -> None:
        await _mark_recent_collection_write(self._adapter, account, games)

    async def consume_recent_collection_write(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        return await _consume_recent_collection_write(self._adapter, collection, account)

    async def invalidate_personal_deck_usage_cache(self, account: AuthenticatedAccount) -> None:
        await _invalidate_personal_deck_usage_cache(self._adapter, account)

    async def invalidate_personal_deck_caches(self, account: AuthenticatedAccount) -> None:
        await _invalidate_personal_deck_caches(self._adapter, account)

    async def invalidate_collection_caches(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> None:
        await _invalidate_collection_caches(self._adapter, account, games)

    async def get_personal_deck_usage_snapshot(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> PersonalDeckUsageSnapshot:
        return await _get_personal_deck_usage_snapshot(self._adapter, account, force_refresh)
