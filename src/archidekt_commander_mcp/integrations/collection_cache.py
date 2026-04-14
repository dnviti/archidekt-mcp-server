from __future__ import annotations

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import json
import logging
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis_async
from redis.exceptions import RedisError

from ..schemas.accounts import CollectionLocator
from ..schemas.collections import CollectionSnapshot
from .public_collection import ArchidektPublicCollectionClient
from .serialization import (
    _parse_datetime,
    deserialize_collection_snapshot,
    serialize_collection_snapshot,
)


LOGGER = logging.getLogger("archidekt_commander_mcp.clients")


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
            snapshot = deserialize_collection_snapshot(snapshot_payload)
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
            "snapshot": serialize_collection_snapshot(snapshot),
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
        if isinstance(ttl, int) and ttl >= 0:
            return ttl
        if isinstance(ttl, int) and ttl == -1:
            return None
        return None

    async def _delete_key(self, redis_key: str) -> None:
        try:
            await self.redis.delete(redis_key)
        except RedisError as error:
            LOGGER.warning("Failed to delete invalid Redis cache key %s: %s", redis_key, error)

    async def invalidate_snapshot(self, collection: CollectionLocator) -> None:
        await self._delete_key(self._redis_key(collection.cache_key))
