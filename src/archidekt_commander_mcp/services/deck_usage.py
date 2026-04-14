# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ..schemas.accounts import AuthenticatedAccount
from ..schemas.cards import CardResult
from ..schemas.decks import PersonalDeckCardUsage, PersonalDeckSummary
from .serialization import _parse_datetime, _safe_int

if TYPE_CHECKING:
    from .deckbuilding import DeckbuildingService

LOGGER = logging.getLogger("archidekt_commander_mcp.server")


@dataclass(slots=True)
class PersonalDeckUsageSnapshot:
    account: AuthenticatedAccount
    decks: list[PersonalDeckSummary]
    usage_by_oracle_id: dict[str, list[PersonalDeckCardUsage]]
    usage_by_name: dict[str, list[PersonalDeckCardUsage]]
    fetched_at: datetime


@dataclass(slots=True)
class AuthenticatedDeckListSnapshot:
    account: AuthenticatedAccount
    decks: list[PersonalDeckSummary]
    fetched_at: datetime


def _normalize_lookup_value(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    if not compact:
        return None
    return compact.casefold()


def _usage_sort_key(usage: PersonalDeckCardUsage) -> tuple[float, str]:
    timestamp = usage.updated_at.timestamp() if usage.updated_at else 0.0
    return (-timestamp, usage.deck_name.casefold())


def _serialize_personal_deck_usage_snapshot(
    snapshot: PersonalDeckUsageSnapshot,
) -> dict[str, Any]:
    return {
        "account": snapshot.account.model_dump(mode="json"),
        "decks": [deck.model_dump(mode="json") for deck in snapshot.decks],
        "usage_by_oracle_id": {
            key: [usage.model_dump(mode="json") for usage in usages]
            for key, usages in snapshot.usage_by_oracle_id.items()
        },
        "usage_by_name": {
            key: [usage.model_dump(mode="json") for usage in usages]
            for key, usages in snapshot.usage_by_name.items()
        },
        "fetched_at": snapshot.fetched_at.isoformat(),
    }


def _deserialize_personal_deck_usage_snapshot(
    payload: dict[str, Any],
) -> PersonalDeckUsageSnapshot:
    return PersonalDeckUsageSnapshot(
        account=AuthenticatedAccount.model_validate(payload.get("account") or {}),
        decks=[
            PersonalDeckSummary.model_validate(deck_payload)
            for deck_payload in (payload.get("decks") or [])
        ],
        usage_by_oracle_id={
            str(key): [
                PersonalDeckCardUsage.model_validate(usage_payload)
                for usage_payload in (usages or [])
            ]
            for key, usages in (payload.get("usage_by_oracle_id") or {}).items()
        },
        usage_by_name={
            str(key): [
                PersonalDeckCardUsage.model_validate(usage_payload)
                for usage_payload in (usages or [])
            ]
            for key, usages in (payload.get("usage_by_name") or {}).items()
        },
        fetched_at=_parse_datetime(payload.get("fetched_at")) or datetime.now(UTC),
    )


async def _get_personal_deck_usage_snapshot(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
    force_refresh: bool = False,
) -> PersonalDeckUsageSnapshot:
    cache_key = service._private_usage_cache_key(account)
    async with service._lock_for_key(cache_key):
        if not force_refresh:
            cached_snapshot = await service._load_private_cache(
                service._personal_deck_usage_cache,
                "personal-decks",
                cache_key,
                _deserialize_personal_deck_usage_snapshot,
            )
            if cached_snapshot is not None:
                return cast(PersonalDeckUsageSnapshot, cached_snapshot)

        resolved_account, decks = await service.auth_client.list_personal_decks(account)
        usage_by_oracle_id: dict[str, list[PersonalDeckCardUsage]] = {}
        usage_by_name: dict[str, list[PersonalDeckCardUsage]] = {}

        semaphore = asyncio.Semaphore(6)

        async def fetch_one(deck: PersonalDeckSummary) -> tuple[PersonalDeckSummary, dict[str, Any]]:
            async with semaphore:
                payload = await service.auth_client.fetch_deck_cards(
                    resolved_account,
                    deck.id,
                    include_deleted=False,
                )
            return deck, payload

        results = await asyncio.gather(
            *(fetch_one(deck) for deck in decks),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, BaseException):
                LOGGER.warning("Failed to fetch one personal deck while building usage index: %s", result)
                continue

            deck, payload = result
            cards = payload.get("cards") or []
            deck_usage_by_oracle: dict[str, PersonalDeckCardUsage] = {}
            deck_usage_by_name: dict[str, PersonalDeckCardUsage] = {}

            for raw_record in cards:
                if raw_record.get("deletedAt"):
                    continue

                quantity = _safe_int(raw_record.get("quantity")) or 0
                if quantity <= 0:
                    continue

                card_payload = raw_record.get("card") or {}
                oracle_card = card_payload.get("oracleCard") or {}
                oracle_id = _normalize_lookup_value(oracle_card.get("uid"))
                fallback_name = _normalize_lookup_value(
                    oracle_card.get("name")
                    or card_payload.get("displayName")
                    or card_payload.get("name")
                )
                categories = sorted(
                    {str(category) for category in (raw_record.get("categories") or []) if category}
                )

                if oracle_id:
                    entry = deck_usage_by_oracle.get(oracle_id)
                    if entry is None:
                        entry = PersonalDeckCardUsage(
                            deck_id=deck.id,
                            deck_name=deck.name,
                            quantity=0,
                            categories=[],
                            private=deck.private,
                            unlisted=deck.unlisted,
                            theorycrafted=deck.theorycrafted,
                            updated_at=deck.updated_at,
                        )
                        deck_usage_by_oracle[oracle_id] = entry
                    entry.quantity += quantity
                    entry.categories = sorted(set(entry.categories) | set(categories))

                if fallback_name:
                    entry = deck_usage_by_name.get(fallback_name)
                    if entry is None:
                        entry = PersonalDeckCardUsage(
                            deck_id=deck.id,
                            deck_name=deck.name,
                            quantity=0,
                            categories=[],
                            private=deck.private,
                            unlisted=deck.unlisted,
                            theorycrafted=deck.theorycrafted,
                            updated_at=deck.updated_at,
                        )
                        deck_usage_by_name[fallback_name] = entry
                    entry.quantity += quantity
                    entry.categories = sorted(set(entry.categories) | set(categories))

            for oracle_id, usage in deck_usage_by_oracle.items():
                usage_by_oracle_id.setdefault(oracle_id, []).append(usage)
            for name_key, usage in deck_usage_by_name.items():
                usage_by_name.setdefault(name_key, []).append(usage)

        for usages in usage_by_oracle_id.values():
            usages.sort(key=_usage_sort_key)
        for usages in usage_by_name.values():
            usages.sort(key=_usage_sort_key)

        snapshot = PersonalDeckUsageSnapshot(
            account=resolved_account,
            decks=decks,
            usage_by_oracle_id=usage_by_oracle_id,
            usage_by_name=usage_by_name,
            fetched_at=datetime.now(UTC),
        )
        await service._store_private_cache(
            service._personal_deck_usage_cache,
            "personal-decks",
            cache_key,
            snapshot,
            _serialize_personal_deck_usage_snapshot,
        )
        return snapshot


def _apply_personal_deck_usage(
    results: list[CardResult],
    usage_snapshot: PersonalDeckUsageSnapshot,
) -> None:
    for result in results:
        usages = []
        if result.oracle_id:
            usages = usage_snapshot.usage_by_oracle_id.get(
                _normalize_lookup_value(result.oracle_id) or "",
                [],
            )
        if not usages:
            usages = usage_snapshot.usage_by_name.get(
                _normalize_lookup_value(result.name) or "",
                [],
            )
        if not usages:
            continue

        result.personal_deck_usage = [
            usage.model_copy(deep=True) for usage in usages
        ]
        result.personal_deck_count = len(usages)
        result.personal_deck_total_quantity = sum(usage.quantity for usage in usages)
