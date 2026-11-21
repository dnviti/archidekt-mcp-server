from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

# In-process caches only serve as a fallback next to Redis. Entries expire lazily on
# read, so without pruning a long-lived replica would keep every user's snapshot forever.
MEMORY_CACHE_PRUNE_THRESHOLD = 256


def prune_expired_entries(cache: dict[str, tuple[datetime, Any]]) -> None:
    if len(cache) < MEMORY_CACHE_PRUNE_THRESHOLD:
        return
    now = datetime.now(UTC)
    for key in [key for key, (expires_at, _) in cache.items() if expires_at <= now]:
        del cache[key]


def prune_idle_locks(locks: dict[str, asyncio.Lock]) -> None:
    if len(locks) < MEMORY_CACHE_PRUNE_THRESHOLD:
        return
    # Only drop locks that are free and have nobody queued; asyncio cannot interleave
    # between the setdefault() in callers and their `async with`, so this is safe.
    for key in [
        key
        for key, lock in locks.items()
        if not lock.locked() and not getattr(lock, "_waiters", None)
    ]:
        del locks[key]
