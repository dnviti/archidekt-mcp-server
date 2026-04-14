from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import RuntimeSettings
from .request_gate import ArchidektRequestGate


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
            if response.status_code != 429 or attempt + 1 >= max_attempts:
                return response

            retry_delay_seconds = self._archidekt_retry_delay_seconds(response, attempt)
            LOGGER.warning(
                "Archidekt returned 429 for %s %s; retrying in %.3f seconds (attempt %d/%d)",
                method,
                url,
                retry_delay_seconds,
                attempt + 1,
                max_attempts - 1,
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
        return float(
            min(
                self.settings.archidekt_retry_base_delay_seconds * (2**attempt),
                8.0,
            )
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
