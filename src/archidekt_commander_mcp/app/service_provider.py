from __future__ import annotations

import asyncio
import logging

from ..config import RuntimeSettings
from ..services.deckbuilding import DeckbuildingService


class DeckbuildingServiceProvider:
    def __init__(self, runtime: RuntimeSettings, logger: logging.Logger) -> None:
        self._runtime = runtime
        self._logger = logger
        self._service: DeckbuildingService | None = None
        self._lock: asyncio.Lock | None = None

    async def get(self) -> DeckbuildingService:
        if self._service is not None:
            return self._service

        if self._lock is None:
            self._lock = asyncio.Lock()

        async with self._lock:
            if self._service is None:
                self._logger.info("Creating DeckbuildingService")
                self._service = DeckbuildingService(self._runtime)
        return self._service

    async def close(self) -> None:
        if self._service is None:
            return
        await self._service.aclose()
        self._service = None
