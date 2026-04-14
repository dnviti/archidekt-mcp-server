# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
import redis.asyncio as redis_async

from ..config import RuntimeSettings
from ..filtering import (
    aggregate_owned_results,
    build_type_line,
    paginate_results,
    record_matches_filters,
    sort_card_results,
)
from ..integrations.authenticated import ArchidektAuthenticatedClient
from ..integrations.collection_cache import CollectionCache
from ..integrations.public_collection import ArchidektPublicCollectionClient
from ..integrations.scryfall import ScryfallClient, card_matches_scryfall_filters, scryfall_price_key
from ..integrations.serialization import deserialize_collection_snapshot, serialize_collection_snapshot
from ..schemas.accounts import ArchidektAccount, ArchidektLoginResponse, AuthenticatedAccount, CollectionLocator
from ..schemas.cards import ArchidektCardSearchResponse, CardResult
from ..schemas.collections import (
    CollectionCardDelete,
    CollectionCardUpsert,
    CollectionCardUpsertResult,
    CollectionMutationResponse,
    CollectionOverview,
)
from ..schemas.decks import (
    PersonalDeckCardMutation,
    PersonalDeckCardRecord,
    PersonalDeckCardsResponse,
    PersonalDeckCreateInput,
    PersonalDeckMutationResponse,
    PersonalDeckSummary,
    PersonalDeckUpdateInput,
    PersonalDecksResponse,
)
from ..schemas.search import ArchidektCardSearchFilters, CardSearchFilters, SearchResponse
from .account_resolution import (
    _coerce_account as _coerce_account_impl,
    _ensure_account_identity as _ensure_account_identity_impl,
    _resolve_optional_account as _resolve_optional_account_impl,
    describe_collection_locator,
)
from .deck_usage import (
    AuthenticatedDeckListSnapshot,
    PersonalDeckUsageSnapshot,
    _apply_personal_deck_usage as _apply_personal_deck_usage_impl,
    _get_personal_deck_usage_snapshot as _get_personal_deck_usage_snapshot_impl,
)
from .serialization import (
    _extract_deck_id,
    _extract_face_image,
    _parse_datetime,
    _safe_float,
    _safe_int,
)
from .snapshot_cache import (
    _account_collection_locators as _account_collection_locators_impl,
    _collection_write_marker_key as _collection_write_marker_key_impl,
    _consume_recent_collection_write as _consume_recent_collection_write_impl,
    _deduplicate_personal_decks as _deduplicate_personal_decks_impl,
    _delete_private_redis_key as _delete_private_redis_key_impl,
    _get_authenticated_deck_list as _get_authenticated_deck_list_impl,
    _invalidate_authenticated_deck_list_cache as _invalidate_authenticated_deck_list_cache_impl,
    _invalidate_collection_caches as _invalidate_collection_caches_impl,
    _invalidate_personal_deck_caches as _invalidate_personal_deck_caches_impl,
    _invalidate_personal_deck_usage_cache as _invalidate_personal_deck_usage_cache_impl,
    _is_self_collection_locator as _is_self_collection_locator_impl,
    _load_authenticated_deck_list_from_redis as _load_authenticated_deck_list_from_redis_impl,
    _load_private_cache as _load_private_cache_impl,
    _load_private_memory_cache as _load_private_memory_cache_impl,
    _load_private_redis_cache as _load_private_redis_cache_impl,
    _lock_for_key as _lock_for_key_impl,
    _mark_recent_collection_write as _mark_recent_collection_write_impl,
    _private_account_cache_key as _private_account_cache_key_impl,
    _private_authenticated_deck_list_cache_key as _private_authenticated_deck_list_cache_key_impl,
    _private_redis_key as _private_redis_key_impl,
    _private_snapshot_cache_key as _private_snapshot_cache_key_impl,
    _private_usage_cache_key as _private_usage_cache_key_impl,
    _private_redis_ttl as _private_redis_ttl_impl,
    _store_authenticated_deck_list_in_redis as _store_authenticated_deck_list_in_redis_impl,
    _store_private_cache as _store_private_cache_impl,
    _store_private_memory_cache as _store_private_memory_cache_impl,
    _store_private_redis_cache as _store_private_redis_cache_impl,
)


LOGGER = logging.getLogger("archidekt_commander_mcp.server")


def _normalize_lookup_value(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    if not compact:
        return None
    return compact.casefold()


def _compact_optional_text(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    return compact or None


def _coerce_optional_bool(*values: Any) -> bool | None:
    for value in values:
        if value is None:
            continue
        return bool(value)
    return None


class DeckbuildingService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": settings.user_agent},
        )
        self.redis_client = redis_async.from_url(settings.redis_url, decode_responses=True)
        self.archidekt_client = ArchidektPublicCollectionClient(self.http_client, settings)
        self.auth_client = ArchidektAuthenticatedClient(self.http_client, settings, redis_client=self.redis_client)
        self.scryfall_client = ScryfallClient(self.http_client, settings)
        self.cache = CollectionCache(
            self.archidekt_client,
            self.redis_client,
            settings.cache_ttl_seconds,
            settings.redis_key_prefix,
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._private_snapshot_cache: dict[str, tuple[datetime, Any]] = {}
        self._authenticated_deck_list_cache: dict[str, tuple[datetime, AuthenticatedDeckListSnapshot]] = {}
        self._personal_deck_usage_cache: dict[str, tuple[datetime, PersonalDeckUsageSnapshot]] = {}
        self._recent_collection_write_markers: dict[str, datetime] = {}

    def _lock_for_key(self, key: str) -> asyncio.Lock:
        return _lock_for_key_impl(self, key)

    def _private_account_cache_key(self, account: AuthenticatedAccount) -> str:
        return _private_account_cache_key_impl(self, account)

    def _private_snapshot_cache_key(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> str:
        return _private_snapshot_cache_key_impl(self, collection, account)

    def _private_usage_cache_key(self, account: AuthenticatedAccount) -> str:
        return _private_usage_cache_key_impl(self, account)

    def _private_authenticated_deck_list_cache_key(self, account: AuthenticatedAccount) -> str:
        return _private_authenticated_deck_list_cache_key_impl(self, account)

    def _deduplicate_personal_decks(self, decks: list[PersonalDeckSummary]) -> list[PersonalDeckSummary]:
        return _deduplicate_personal_decks_impl(self, decks)

    async def _get_authenticated_deck_list(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        return await _get_authenticated_deck_list_impl(self, account, force_refresh)

    async def _load_authenticated_deck_list_from_redis(
        self,
        cache_key: str,
    ) -> AuthenticatedDeckListSnapshot | None:
        return await _load_authenticated_deck_list_from_redis_impl(self, cache_key)

    async def _store_authenticated_deck_list_in_redis(
        self,
        cache_key: str,
        snapshot: AuthenticatedDeckListSnapshot,
    ) -> None:
        await _store_authenticated_deck_list_in_redis_impl(self, cache_key, snapshot)

    async def _invalidate_authenticated_deck_list_cache(self, account: AuthenticatedAccount) -> None:
        await _invalidate_authenticated_deck_list_cache_impl(self, account)

    def _collection_write_marker_key(self, account: AuthenticatedAccount, game: int) -> str:
        return _collection_write_marker_key_impl(self, account, game)

    def _private_redis_key(self, namespace: str, cache_key: str) -> str:
        return _private_redis_key_impl(self, namespace, cache_key)

    async def _ensure_account_identity(self, account: AuthenticatedAccount) -> AuthenticatedAccount:
        return await _ensure_account_identity_impl(self, account)

    def _load_private_memory_cache(self, cache: dict[str, tuple[datetime, Any]], key: str) -> Any | None:
        return _load_private_memory_cache_impl(self, cache, key)

    def _store_private_memory_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        key: str,
        value: Any,
    ) -> None:
        _store_private_memory_cache_impl(self, cache, key, value)

    async def _load_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        return await _load_private_cache_impl(self, cache, namespace, key, deserializer)

    async def _store_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await _store_private_cache_impl(self, cache, namespace, key, value, serializer)

    async def _load_private_redis_cache(
        self,
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        return await _load_private_redis_cache_impl(self, namespace, key, deserializer)

    async def _store_private_redis_cache(
        self,
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await _store_private_redis_cache_impl(self, namespace, key, value, serializer)

    async def _private_redis_ttl(self, redis_key: str) -> int | None:
        return await _private_redis_ttl_impl(self, redis_key)

    async def _delete_private_redis_key(self, redis_key: str) -> None:
        await _delete_private_redis_key_impl(self, redis_key)

    def _account_collection_locators(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> list[CollectionLocator]:
        return _account_collection_locators_impl(self, account, games)

    def _is_self_collection_locator(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        return _is_self_collection_locator_impl(self, collection, account)

    def _mark_recent_collection_write(
        self,
        account: AuthenticatedAccount,
        games: set[int],
    ) -> None:
        _mark_recent_collection_write_impl(self, account, games)

    def _consume_recent_collection_write(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        return _consume_recent_collection_write_impl(self, collection, account)

    async def _invalidate_personal_deck_usage_cache(self, account: AuthenticatedAccount) -> None:
        await _invalidate_personal_deck_usage_cache_impl(self, account)

    async def _invalidate_personal_deck_caches(self, account: AuthenticatedAccount) -> None:
        await _invalidate_personal_deck_caches_impl(self, account)

    async def _invalidate_collection_caches(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> None:
        await _invalidate_collection_caches_impl(self, account, games)

    async def get_snapshot(
        self,
        collection: CollectionLocator,
        force_refresh: bool = False,
        account: AuthenticatedAccount | ArchidektAccount | None = None,
    ):
        if account is None:
            async with self._lock_for_key(collection.cache_key):
                return await self.cache.get_snapshot(collection, force_refresh=force_refresh)

        resolved_account = await self._coerce_account(account)
        if resolved_account.username is None or resolved_account.user_id is None:
            resolved_account = await self._ensure_account_identity(resolved_account)
        if not force_refresh and self._consume_recent_collection_write(collection, resolved_account):
            force_refresh = True
            LOGGER.info(
                "Bypassing cached snapshot for %s after a recent authenticated collection write",
                collection.cache_key,
            )
        cache_key = self._private_snapshot_cache_key(collection, resolved_account)
        async with self._lock_for_key(cache_key):
            if not force_refresh:
                cached_snapshot = await self._load_private_cache(
                    self._private_snapshot_cache,
                    "collection",
                    cache_key,
                    deserialize_collection_snapshot,
                )
                if cached_snapshot is not None:
                    return cached_snapshot

            snapshot = await self.archidekt_client.fetch_snapshot(
                collection,
                auth_token=resolved_account.token,
            )
            await self._store_private_cache(
                self._private_snapshot_cache,
                "collection",
                cache_key,
                snapshot,
                serialize_collection_snapshot,
            )
            return snapshot

    async def login_archidekt(self, account: ArchidektAccount | None = None) -> ArchidektLoginResponse:
        resolved_account = await self._coerce_account(account)
        if resolved_account.user_id is None or resolved_account.username is None:
            resolved_account, decks = await self.auth_client.list_personal_decks(resolved_account)
        else:
            _, decks = await self.auth_client.list_personal_decks(resolved_account)

        if resolved_account.user_id is None:
            raise RuntimeError("Archidekt authentication succeeded but did not resolve a user id.")

        personal_decks: PersonalDecksResponse | None = None
        notes = [
            "Reuse the returned `account` object in later authenticated tool calls so you do not have to resend the password.",
            "The returned `collection.collection_id` is inferred from Archidekt's current frontend, which links My Collection to `/collection/v2/{user_id}/`.",
        ]
        personal_decks = self._build_personal_decks_response(resolved_account, decks)
        notes.append(
            "The login response includes the current personal deck list so the model can reason about existing decks before proposing or creating another one."
        )
        if account is None:
            notes.append(
                "This login was resolved from the current MCP auth session instead of requiring credentials in the tool payload."
            )

        return ArchidektLoginResponse(
            account=resolved_account,
            collection=CollectionLocator(collection_id=resolved_account.user_id),
            notes=notes,
            personal_decks=personal_decks,
        )

    async def list_personal_decks(self, account: ArchidektAccount | None = None) -> PersonalDecksResponse:
        resolved_account = await self._coerce_account(account)
        resolved_account, decks = await self.auth_client.list_personal_decks(resolved_account)
        return self._build_personal_decks_response(resolved_account, decks)

    def _build_personal_decks_response(
        self,
        resolved_account: AuthenticatedAccount,
        decks: list[PersonalDeckSummary],
    ) -> PersonalDecksResponse:
        owner_username = resolved_account.username or (decks[0].owner_username if decks else None)
        private_count = sum(1 for deck in decks if deck.private)
        unlisted_count = sum(1 for deck in decks if deck.unlisted)

        notes = [
            "Authenticated deck listing executed against the current Archidekt account.",
            "Use this together with `search_owned_cards` to spot cards already committed to other decks.",
        ]
        if private_count or unlisted_count:
            notes.append(
                f"Returned {private_count} private deck(s) and {unlisted_count} unlisted deck(s)."
            )

        return PersonalDecksResponse(
            owner_username=owner_username,
            total_decks=len(decks),
            fetched_at=datetime.now(UTC),
            notes=notes,
            decks=decks,
        )

    async def search_archidekt_cards(
        self,
        filters: ArchidektCardSearchFilters,
    ) -> ArchidektCardSearchResponse:
        results, total_matches, has_more = await self.auth_client.search_cards(filters)
        notes = [
            "Use `card_id` values from this response when creating deck entries or collection entries.",
            "For cards already in the user's collection, `search_owned_cards` may already expose matching `archidekt_card_ids`.",
        ]
        if filters.exact_name:
            notes.append(
                "Use `requested_exact_name` on each result to match returned printings back to the requested card names."
            )
            if len(filters.exact_name) > 1:
                notes.append(
                    "This response aggregates multiple exact-name Archidekt lookups into one batch for the model."
                )
                matched_names = {
                    (result.requested_exact_name or "").casefold()
                    for result in results
                    if result.requested_exact_name
                }
                missing_names = [
                    name for name in filters.exact_name if name.casefold() not in matched_names
                ]
                if missing_names:
                    notes.append(
                        "No exact Archidekt match was returned for: " + ", ".join(missing_names) + "."
                    )
        return ArchidektCardSearchResponse(
            page=filters.page,
            returned_count=len(results),
            total_matches=total_matches,
            has_more=has_more,
            notes=notes,
            results=results,
        )

    async def get_personal_deck_cards(
        self,
        deck_id: int,
        include_deleted: bool = False,
        account: AuthenticatedAccount | ArchidektAccount | None = None,
    ) -> PersonalDeckCardsResponse:
        resolved_account = await self._coerce_account(account)
        payload = await self.auth_client.fetch_deck_cards(
            resolved_account,
            deck_id,
            include_deleted=include_deleted,
        )
        raw_cards = payload.get("cards") or payload.get("results") or []
        mapped_cards = [
            self._map_personal_deck_card_record(item)
            for item in raw_cards
            if isinstance(item, dict) and (include_deleted or not item.get("deletedAt"))
        ]
        return PersonalDeckCardsResponse(
            deck_id=deck_id,
            include_deleted=include_deleted,
            fetched_at=datetime.now(UTC),
            total_cards=len(mapped_cards),
            notes=[
                "Use `deck_relation_id` for future modify/remove deck card operations.",
                "Use `archidekt_card_id` when adding another copy of a known Archidekt card to a deck.",
            ],
            cards=mapped_cards,
        )

    async def create_personal_deck(
        self,
        deck: PersonalDeckCreateInput,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._coerce_account(account)
        payload, summary = await self.auth_client.create_deck(resolved_account, deck)
        deck_id = (summary.id if summary else None) or _extract_deck_id(payload)
        if deck_id is None:
            raise RuntimeError("Archidekt deck create succeeded but did not return a deck id.")
        await self._invalidate_personal_deck_caches(resolved_account)
        return PersonalDeckMutationResponse(
            action="created",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=1,
            processed_at=datetime.now(UTC),
            notes=[
                "Personal deck usage cache invalidated for this account.",
                "Use `modify_personal_deck_cards` next if the deck should be populated immediately.",
            ],
            deck=summary,
            result=payload,
        )

    async def update_personal_deck(
        self,
        deck_id: int,
        deck: PersonalDeckUpdateInput,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._coerce_account(account)
        payload, summary = await self.auth_client.update_deck(resolved_account, deck_id, deck)
        await self._invalidate_personal_deck_caches(resolved_account)
        return PersonalDeckMutationResponse(
            action="updated",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=1,
            processed_at=datetime.now(UTC),
            notes=["Personal deck usage cache invalidated for this account."],
            deck=summary,
            result=payload,
        )

    async def delete_personal_deck(
        self,
        deck_id: int,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._coerce_account(account)
        await self.auth_client.delete_deck(resolved_account, deck_id)
        await self._invalidate_personal_deck_caches(resolved_account)
        return PersonalDeckMutationResponse(
            action="deleted",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=1,
            processed_at=datetime.now(UTC),
            notes=["Personal deck usage cache invalidated for this account."],
            result={"deleted": True},
        )

    async def modify_personal_deck_cards(
        self,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._coerce_account(account)
        cards, backfill_notes = await self._backfill_mutation_card_ids(
            deck_id=deck_id,
            cards=cards,
            account=resolved_account,
        )
        payload = await self.auth_client.modify_deck_cards(resolved_account, deck_id, cards)
        await self._invalidate_personal_deck_caches(resolved_account)
        successful_count = _safe_int(payload.get("successful_count")) if isinstance(payload, dict) else None
        failed_count = _safe_int(payload.get("failed_count")) if isinstance(payload, dict) else None
        affected_count = successful_count if successful_count is not None else len(cards)
        notes = [
            "Personal deck usage cache invalidated for this account.",
            "Re-run `get_personal_deck_cards` if you need fresh `deck_relation_id` values after this patch.",
            *backfill_notes,
        ]
        if failed_count:
            notes.append(
                f"Applied {affected_count} deck card mutation(s); {failed_count} mutation(s) were rejected by Archidekt. Inspect `result.failed_mutations` for the exact payloads and errors."
            )
        return PersonalDeckMutationResponse(
            action="modified-cards",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=affected_count,
            processed_at=datetime.now(UTC),
            notes=notes,
            result=payload,
        )

    async def _backfill_mutation_card_ids(
        self,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: AuthenticatedAccount,
    ) -> tuple[list[PersonalDeckCardMutation], list[str]]:
        cards_needing_backfill = [
            card
            for card in cards
            if card.deck_relation_id is not None
            and card.card_id is None
            and card.custom_card_id is None
        ]
        if not cards_needing_backfill:
            return cards, []

        deck_state = await self.get_personal_deck_cards(
            deck_id=deck_id,
            include_deleted=True,
            account=account,
        )
        card_ids_by_relation = {
            deck_card.deck_relation_id: deck_card.archidekt_card_id
            for deck_card in deck_state.cards
            if deck_card.deck_relation_id is not None and deck_card.archidekt_card_id is not None
        }
        backfilled_count = 0
        updated_cards: list[PersonalDeckCardMutation] = []
        for card in cards:
            if (
                card.deck_relation_id is not None
                and card.card_id is None
                and card.custom_card_id is None
            ):
                backfilled_card_id = card_ids_by_relation.get(card.deck_relation_id)
                if backfilled_card_id is not None:
                    card = card.model_copy(update={"card_id": backfilled_card_id})
                    backfilled_count += 1
            updated_cards.append(card)

        if not backfilled_count:
            return updated_cards, []
        return (
            updated_cards,
            [
                f"Backfilled Archidekt `card_id` values for {backfilled_count} deck mutation(s) using the current deck state."
            ],
        )

    async def upsert_collection_entries(
        self,
        entries: list[CollectionCardUpsert],
        account: ArchidektAccount | None = None,
    ) -> CollectionMutationResponse:
        resolved_account = await self._ensure_account_identity(await self._coerce_account(account))
        results: list[CollectionCardUpsertResult] = []
        affected_games: set[int] = set()
        for entry in entries:
            payload = await self.auth_client.upsert_collection_entry(resolved_account, entry)
            results.append(
                CollectionCardUpsertResult(
                    operation="updated" if entry.record_id is not None else "created",
                    record_id=_safe_int(payload.get("id") or payload.get("recordId")) or entry.record_id,
                    card_id=entry.card_id,
                    game=entry.game,
                    result=payload,
                )
            )
            affected_games.add(entry.game)

        await self._invalidate_collection_caches(resolved_account, affected_games)
        self._mark_recent_collection_write(resolved_account, affected_games)
        return CollectionMutationResponse(
            action="upsert",
            account_username=resolved_account.username,
            affected_count=len(results),
            processed_at=datetime.now(UTC),
            notes=[
                "Public and authenticated collection caches were invalidated for the affected game(s).",
                "The next authenticated read of the same self collection will bypass cached snapshots once to reduce stale reads after this write.",
                "Use `search_archidekt_cards` or `search_owned_cards` to source Archidekt `card_id` values for later writes.",
            ],
            results=results,
        )

    async def delete_collection_entries(
        self,
        entries: list[CollectionCardDelete],
        account: ArchidektAccount | None = None,
    ) -> CollectionMutationResponse:
        resolved_account = await self._ensure_account_identity(await self._coerce_account(account))
        record_ids = [entry.record_id for entry in entries]
        payload = await self.auth_client.delete_collection_entries(resolved_account, record_ids)
        affected_games = {entry.game for entry in entries if entry.game is not None} or {1, 2, 3}

        await self._invalidate_collection_caches(resolved_account, affected_games)
        self._mark_recent_collection_write(resolved_account, affected_games)
        return CollectionMutationResponse(
            action="delete",
            account_username=resolved_account.username,
            affected_count=len(entries),
            processed_at=datetime.now(UTC),
            notes=[
                "Public and authenticated collection caches were invalidated for the affected game(s).",
                "The next authenticated read of the same self collection will bypass cached snapshots once to reduce stale reads after this write.",
                "Use `search_owned_cards` to confirm that the deleted record ids no longer appear in the collection snapshot.",
            ],
            results=[
                CollectionCardUpsertResult(
                    operation="deleted",
                    record_id=entry.record_id,
                    game=entry.game,
                    result=payload,
                )
                for entry in entries
            ],
        )

    async def get_collection_overview(
        self,
        collection: CollectionLocator,
        account: ArchidektAccount | None = None,
    ) -> CollectionOverview:
        resolved_account = await self._resolve_optional_account(account)
        snapshot = await self.get_snapshot(collection, account=resolved_account)
        return CollectionOverview(
            collection_id=snapshot.collection_id,
            owner_id=snapshot.owner_id,
            owner_username=snapshot.owner_username,
            game=snapshot.game,
            total_records=snapshot.total_records,
            unique_oracle_cards=len(snapshot.owned_oracle_ids),
            total_owned_quantity=sum(record.quantity for record in snapshot.records),
            total_pages=snapshot.total_pages,
            page_size=snapshot.page_size,
            source_url=snapshot.source_url,
            fetched_at=snapshot.fetched_at,
        )

    async def search_owned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
        account: ArchidektAccount | None = None,
    ) -> SearchResponse:
        resolved_account = await self._resolve_optional_account(account)
        snapshot = await self.get_snapshot(collection, account=resolved_account)
        matching_records = [
            record for record in snapshot.records if record_matches_filters(record, filters)
        ]
        results = aggregate_owned_results(
            matching_records,
            filters,
            collection_id=snapshot.collection_id,
            base_url=self.settings.normalized_archidekt_base_url,
        )
        notes = [
            f"Collection snapshot fetched at {snapshot.fetched_at.isoformat()}",
            f"Collection locator: {describe_collection_locator(collection)}",
        ]
        if resolved_account is None:
            notes.append(
                "Deterministic search executed against the requested public Archidekt collection."
            )
        else:
            notes.append(
                "Deterministic search executed against the requested authenticated Archidekt collection."
            )
            usage_snapshot = await self._get_personal_deck_usage_snapshot(resolved_account)
            self._apply_personal_deck_usage(results, usage_snapshot)
            if any(result.personal_deck_count for result in results):
                notes.append(
                    "Some owned cards already appear in personal decks. Ask the user whether those cards may be reused before finalizing a new deck."
                )

        sorted_results = sort_card_results(results, filters)
        paged_results = paginate_results(sorted_results, filters.page, filters.limit)
        await self._ensure_archidekt_card_ids(paged_results, game=collection.game)
        total_matches = len(sorted_results)

        return SearchResponse(
            source="collection",
            ownership_scope="owned",
            applied_filters=filters.model_dump(mode="json"),
            page=filters.page,
            limit=filters.limit,
            returned_count=len(paged_results),
            total_matches=total_matches,
            has_more=filters.page * filters.limit < total_matches,
            notes=notes,
            results=paged_results,
        )

    async def search_unowned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
        account: ArchidektAccount | None = None,
    ) -> SearchResponse:
        resolved_account = await self._resolve_optional_account(account)
        snapshot = await self.get_snapshot(collection, account=resolved_account)
        raw_cards, query_used, has_more, notes = await self.scryfall_client.search_unowned_cards(
            filters=filters,
            owned_oracle_ids=snapshot.owned_oracle_ids,
            owned_names=snapshot.owned_names,
        )

        filtered_cards = [card for card in raw_cards if card_matches_scryfall_filters(card, filters)]
        mapped_results = [self._map_scryfall_card(card, filters) for card in filtered_cards]
        sorted_results = sort_card_results(mapped_results, filters)
        paged_results = paginate_results(sorted_results, filters.page, filters.limit)
        await self._ensure_archidekt_card_ids(paged_results, game=collection.game)

        return SearchResponse(
            source="scryfall",
            ownership_scope="unowned",
            applied_filters=filters.model_dump(mode="json"),
            query_used=query_used,
            page=filters.page,
            limit=filters.limit,
            returned_count=len(paged_results),
            total_matches=len(sorted_results) if not has_more else None,
            has_more=has_more,
            notes=notes
            + [
                f"Collection locator: {describe_collection_locator(collection)}",
                (
                    "Owned cards were excluded deterministically using the requested authenticated Archidekt collection."
                    if resolved_account is not None
                    else "Owned cards were excluded deterministically using the requested Archidekt collection."
                ),
            ],
            results=paged_results,
        )

    async def _ensure_archidekt_card_ids(
        self,
        results: list[CardResult],
        *,
        game: int,
    ) -> None:
        missing_results = [
            result
            for result in results
            if not result.archidekt_card_ids and result.name
        ]
        if not missing_results:
            return

        exact_names = list(dict.fromkeys(result.name for result in missing_results if result.name))
        if not exact_names:
            return

        catalog_results, _, _ = await self.auth_client.search_cards(
            ArchidektCardSearchFilters(
                exact_name=exact_names,
                game=game,
                include_tokens=True,
                all_editions=True,
            )
        )
        candidates_by_name: dict[str, list[Any]] = {}
        for candidate in catalog_results:
            lookup_name = (candidate.requested_exact_name or candidate.name).casefold()
            if lookup_name:
                candidates_by_name.setdefault(lookup_name, []).append(candidate)

        for result in missing_results:
            candidates = candidates_by_name.get(result.name.casefold(), [])
            if not candidates:
                continue

            matching_set_candidates = [
                candidate
                for candidate in candidates
                if result.set_code
                and candidate.set_code
                and candidate.set_code.casefold() == result.set_code.casefold()
            ]
            selected = matching_set_candidates or candidates
            result.archidekt_card_ids = sorted(
                {
                    candidate.card_id
                    for candidate in selected
                    if candidate.card_id is not None
                }
            )

    async def _resolve_optional_account(
        self,
        account: AuthenticatedAccount | ArchidektAccount | None,
    ) -> AuthenticatedAccount | None:
        return await _resolve_optional_account_impl(self, account)

    async def _coerce_account(
        self,
        account: AuthenticatedAccount | ArchidektAccount | None,
    ) -> AuthenticatedAccount:
        return await _coerce_account_impl(self, account)

    async def _get_personal_deck_usage_snapshot(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> PersonalDeckUsageSnapshot:
        return await _get_personal_deck_usage_snapshot_impl(self, account, force_refresh)

    def _apply_personal_deck_usage(
        self,
        results: list[CardResult],
        usage_snapshot: PersonalDeckUsageSnapshot,
    ) -> None:
        _apply_personal_deck_usage_impl(results, usage_snapshot)

    def _map_personal_deck_card_record(self, raw_record: dict[str, Any]) -> PersonalDeckCardRecord:
        modifications = raw_record.get("modifications") or {}
        card_payload = raw_record.get("card") or {}
        oracle_card = card_payload.get("oracleCard") or {}
        supertypes = [str(item) for item in (oracle_card.get("superTypes") or []) if item]
        types = [str(item) for item in (oracle_card.get("types") or []) if item]
        subtypes = [str(item) for item in (oracle_card.get("subTypes") or []) if item]
        modifier = _compact_optional_text(modifications.get("modifier"))
        if modifier is None:
            modifier = _compact_optional_text(raw_record.get("modifier"))
        custom_cmc = _safe_float(modifications.get("customCmc"))
        if custom_cmc is None:
            custom_cmc = _safe_float(raw_record.get("customCmc"))
        label = _compact_optional_text(modifications.get("label"))
        if label is None:
            label = _compact_optional_text(raw_record.get("label"))

        return PersonalDeckCardRecord(
            deck_relation_id=_safe_int(raw_record.get("deckRelationId") or raw_record.get("id")),
            quantity=_safe_int(raw_record.get("quantity")) or 0,
            categories=[
                str(category)
                for category in (raw_record.get("categories") or [])
                if category
            ],
            deleted_at=_parse_datetime(raw_record.get("deletedAt")),
            archidekt_card_id=_safe_int(raw_record.get("cardId") or card_payload.get("id")),
            uid=_compact_optional_text(card_payload.get("uid")),
            oracle_card_id=_safe_int(oracle_card.get("id")),
            oracle_id=_compact_optional_text(oracle_card.get("uid")),
            name=str(
                oracle_card.get("name")
                or card_payload.get("displayName")
                or card_payload.get("name")
                or ""
            ),
            display_name=_compact_optional_text(card_payload.get("displayName")),
            mana_cost=_compact_optional_text(oracle_card.get("manaCost")),
            cmc=_safe_float(oracle_card.get("cmc")),
            type_line=build_type_line(supertypes, types, subtypes),
            oracle_text=_compact_optional_text(oracle_card.get("text")),
            modifier=modifier,
            custom_cmc=custom_cmc,
            companion=_coerce_optional_bool(
                modifications.get("companion") if modifications else None,
                raw_record.get("companion"),
            ),
            flipped_default=_coerce_optional_bool(
                modifications.get("flippedDefault") if modifications else None,
                raw_record.get("flippedDefault"),
            ),
            label=label,
        )

    def _map_scryfall_card(self, card: dict[str, Any], filters: CardSearchFilters) -> CardResult:
        prices = card.get("prices") or {}
        price_field = scryfall_price_key(filters.price_source)
        unit_price = _safe_float(prices.get(price_field))
        image_uri = (
            ((card.get("image_uris") or {}).get("normal"))
            or ((card.get("image_uris") or {}).get("large"))
            or _extract_face_image(card.get("card_faces") or [])
        )

        return CardResult(
            source="scryfall",
            ownership_scope="unowned",
            name=str(card.get("name") or ""),
            mana_cost=card.get("mana_cost"),
            cmc=_safe_float(card.get("cmc")),
            type_line=card.get("type_line"),
            oracle_text=card.get("oracle_text"),
            colors=[str(value) for value in (card.get("colors") or [])],
            color_identity=[str(value) for value in (card.get("color_identity") or [])],
            keywords=[str(value) for value in (card.get("keywords") or []) if value],
            rarity=card.get("rarity"),
            set_code=card.get("set"),
            set_name=card.get("set_name"),
            finishes=list(card.get("finishes") or []),
            commander_legal=((card.get("legalities") or {}).get("commander") == "legal"),
            edhrec_rank=_safe_int(card.get("edhrec_rank")),
            unit_price=unit_price,
            price_source=price_field,
            oracle_id=card.get("oracle_id"),
            source_uri=card.get("scryfall_uri"),
            image_uri=image_uri,
        )

    async def aclose(self) -> None:
        await self.http_client.aclose()
        await self.redis_client.aclose()
