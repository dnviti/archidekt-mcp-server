from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

from ..config import RuntimeSettings


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
