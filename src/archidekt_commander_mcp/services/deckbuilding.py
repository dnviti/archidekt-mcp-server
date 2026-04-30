# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
import redis.asyncio as redis_async

from ..auth.provider import RedisArchidektOAuthProvider
from ..config import RuntimeSettings
from ..integrations.authenticated import ArchidektAuthenticatedClient
from ..integrations.collection_cache import CollectionCache
from ..integrations.public_collection import ArchidektPublicCollectionClient
from ..integrations.scryfall import ScryfallClient
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
)
from .account_identity import ArchidektAccountIdentity
from .authenticated_cache import AuthenticatedCache
from .card_search import CardSearchWorkflow
from .deck_usage import (
    AuthenticatedDeckListSnapshot,
    PersonalDeckUsageSnapshot,
    _apply_personal_deck_usage as _apply_personal_deck_usage_impl,
)
from .personal_decks import PersonalDeckWorkflow
from .serialization import _safe_int

LOGGER = logging.getLogger("archidekt_commander_mcp.server")


class DeckbuildingService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": settings.user_agent},
        )
        self.redis_client = redis_async.from_url(settings.redis_url, decode_responses=True)
        self.oauth_provider = (
            RedisArchidektOAuthProvider(
                self.redis_client,
                key_prefix=settings.redis_key_prefix,
                issuer_url=settings.normalized_public_base_url or "",
                auth_code_ttl_seconds=settings.auth_code_ttl_seconds,
                access_token_ttl_seconds=settings.auth_access_token_ttl_seconds,
                refresh_token_ttl_seconds=settings.auth_refresh_token_ttl_seconds,
            )
            if settings.auth_enabled
            else None
        )
        self.archidekt_client = ArchidektPublicCollectionClient(self.http_client, settings)
        self.auth_client = ArchidektAuthenticatedClient(
            self.http_client,
            settings,
            redis_client=self.redis_client,
        )
        self.account_identity = ArchidektAccountIdentity(
            auth_client=lambda: self.auth_client,
            oauth_provider=lambda: self.oauth_provider,
            authenticated_deck_list_loader=lambda account: self._get_authenticated_deck_list(account),
            logger=LOGGER,
        )
        self.auth_client.renew_account = self.account_identity.renew_archidekt_account
        self.scryfall_client = ScryfallClient(self.http_client, settings)
        self.cache = CollectionCache(
            self.archidekt_client,
            self.redis_client,
            settings.cache_ttl_seconds,
            settings.redis_key_prefix,
        )
        self.authenticated_cache = AuthenticatedCache(
            settings=settings,
            redis_client=lambda: self.redis_client,
            auth_client=lambda: self.auth_client,
            collection_cache=lambda: self.cache,
        )
        self.personal_decks = PersonalDeckWorkflow(self)
        self.card_search = CardSearchWorkflow(self)

    @property
    def _locks(self) -> dict[str, asyncio.Lock]:
        return self.authenticated_cache._locks

    @property
    def _private_snapshot_cache(self) -> dict[str, tuple[datetime, Any]]:
        return self.authenticated_cache._private_snapshot_cache

    @property
    def _authenticated_deck_list_cache(
        self,
    ) -> dict[str, tuple[datetime, AuthenticatedDeckListSnapshot]]:
        return self.authenticated_cache._authenticated_deck_list_cache

    @property
    def _authenticated_deck_list_cache_index(self) -> dict[str, set[str]]:
        return self.authenticated_cache._authenticated_deck_list_cache_index

    @property
    def _personal_deck_cache_refresh_markers(self) -> dict[str, datetime]:
        return self.authenticated_cache._personal_deck_cache_refresh_markers

    @property
    def _personal_deck_usage_cache(self) -> dict[str, tuple[datetime, PersonalDeckUsageSnapshot]]:
        return self.authenticated_cache._personal_deck_usage_cache

    @property
    def _recent_collection_write_markers(self) -> dict[str, datetime]:
        return self.authenticated_cache._recent_collection_write_markers

    async def _renew_archidekt_account(
        self,
        account: AuthenticatedAccount,
    ) -> AuthenticatedAccount | None:
        return await self.account_identity.renew_archidekt_account(account)

    async def _renew_after_archidekt_auth_failure(
        self,
        account: AuthenticatedAccount,
        error: Exception,
    ) -> AuthenticatedAccount | None:
        return await self.account_identity.renew_after_archidekt_auth_failure(account, error)

    def _is_archidekt_auth_failure(self, error: Exception) -> bool:
        return self.account_identity.is_archidekt_auth_failure(error)

    def _lock_for_key(self, key: str) -> asyncio.Lock:
        return self.authenticated_cache.lock_for_key(key)

    def _private_account_cache_key(self, account: AuthenticatedAccount) -> str:
        return self.authenticated_cache.private_account_cache_key(account)

    def _private_snapshot_cache_key(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> str:
        return self.authenticated_cache.private_snapshot_cache_key(collection, account)

    def _private_usage_cache_key(self, account: AuthenticatedAccount) -> str:
        return self.authenticated_cache.private_usage_cache_key(account)

    def _private_authenticated_deck_list_cache_key(self, account: AuthenticatedAccount) -> str:
        return self.authenticated_cache.private_authenticated_deck_list_cache_key(account)

    def _deduplicate_personal_decks(self, decks: list[PersonalDeckSummary]) -> list[PersonalDeckSummary]:
        return self.authenticated_cache.deduplicate_personal_decks(decks)

    def _mark_personal_deck_cache_refresh(
        self,
        account: AuthenticatedAccount,
        family: str = "all",
        deck_list_cache_key: str | None = None,
    ) -> None:
        self.authenticated_cache.mark_personal_deck_cache_refresh(account, family, deck_list_cache_key)

    def _has_personal_deck_cache_refresh_marker(
        self,
        account: AuthenticatedAccount,
        family: str,
        deck_list_cache_key: str | None = None,
    ) -> bool:
        return self.authenticated_cache.has_personal_deck_cache_refresh_marker(account, family, deck_list_cache_key)

    def _clear_personal_deck_cache_refresh(
        self,
        account: AuthenticatedAccount,
        family: str,
        deck_list_cache_key: str | None = None,
    ) -> None:
        self.authenticated_cache.clear_personal_deck_cache_refresh(account, family, deck_list_cache_key)

    async def _get_authenticated_deck_list(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        return await self.authenticated_cache.get_authenticated_deck_list(account, force_refresh)

    async def _load_authenticated_deck_list_from_redis(
        self,
        cache_key: str,
    ) -> tuple[AuthenticatedDeckListSnapshot | None, bool]:
        return await self.authenticated_cache.load_authenticated_deck_list_from_redis(cache_key)

    async def _store_authenticated_deck_list_in_redis(
        self,
        cache_key: str,
        snapshot: AuthenticatedDeckListSnapshot,
    ) -> None:
        await self.authenticated_cache.store_authenticated_deck_list_in_redis(cache_key, snapshot)

    async def _invalidate_authenticated_deck_list_cache(self, account: AuthenticatedAccount) -> None:
        await self.authenticated_cache.invalidate_authenticated_deck_list_cache(account)

    def _collection_write_marker_key(self, account: AuthenticatedAccount, game: int) -> str:
        return self.authenticated_cache.collection_write_marker_key(account, game)

    def _private_redis_key(self, namespace: str, cache_key: str) -> str:
        return self.authenticated_cache.private_redis_key(namespace, cache_key)

    async def _ensure_account_identity(self, account: AuthenticatedAccount) -> AuthenticatedAccount:
        return await _ensure_account_identity_impl(self, account)

    def _load_private_memory_cache(self, cache: dict[str, tuple[datetime, Any]], key: str) -> Any | None:
        return self.authenticated_cache.load_private_memory_cache(cache, key)

    def _store_private_memory_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        key: str,
        value: Any,
    ) -> None:
        self.authenticated_cache.store_private_memory_cache(cache, key, value)

    async def _load_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        return await self.authenticated_cache.load_private_cache(cache, namespace, key, deserializer)

    async def _store_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await self.authenticated_cache.store_private_cache(cache, namespace, key, value, serializer)

    async def _load_private_redis_cache(
        self,
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> tuple[Any | None, bool]:
        return await self.authenticated_cache.load_private_redis_cache(namespace, key, deserializer)

    async def _store_private_redis_cache(
        self,
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        await self.authenticated_cache.store_private_redis_cache(namespace, key, value, serializer)

    async def _delete_private_redis_key(self, redis_key: str) -> None:
        await self.authenticated_cache.delete_private_redis_key(redis_key)

    def _account_collection_locators(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> list[CollectionLocator]:
        return self.authenticated_cache.account_collection_locators(account, games)

    def _is_self_collection_locator(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        return self.authenticated_cache.is_self_collection_locator(collection, account)

    async def _mark_recent_collection_write(
        self,
        account: AuthenticatedAccount,
        games: set[int],
    ) -> None:
        await self.authenticated_cache.mark_recent_collection_write(account, games)

    async def _consume_recent_collection_write(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        return await self.authenticated_cache.consume_recent_collection_write(collection, account)

    async def _invalidate_personal_deck_usage_cache(self, account: AuthenticatedAccount) -> None:
        await self.authenticated_cache.invalidate_personal_deck_usage_cache(account)

    async def _invalidate_personal_deck_caches(self, account: AuthenticatedAccount) -> None:
        await self.authenticated_cache.invalidate_personal_deck_caches(account)

    async def _invalidate_collection_caches(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> None:
        await self.authenticated_cache.invalidate_collection_caches(account, games)

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
        skip_cache_store = False
        if not force_refresh and await self._consume_recent_collection_write(collection, resolved_account):
            force_refresh = True
            skip_cache_store = True
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

            try:
                snapshot = await self.archidekt_client.fetch_snapshot(
                    collection,
                    auth_token=resolved_account.token,
                )
            except Exception as error:
                renewed_account = await self._renew_after_archidekt_auth_failure(
                    resolved_account,
                    error,
                )
                if renewed_account is None:
                    raise
                resolved_account = renewed_account
                snapshot = await self.archidekt_client.fetch_snapshot(
                    collection,
                    auth_token=resolved_account.token,
                )
            if not skip_cache_store:
                await self._store_private_cache(
                    self._private_snapshot_cache,
                    "collection",
                    cache_key,
                    snapshot,
                    serialize_collection_snapshot,
                )
            else:
                LOGGER.info(
                    "Skipping cached snapshot store for %s while a recent authenticated write marker is active",
                    collection.cache_key,
                )
            return snapshot

    async def login_archidekt(self, account: ArchidektAccount | None = None) -> ArchidektLoginResponse:
        return await self.personal_decks.login_archidekt(account)

    async def list_personal_decks(self, account: ArchidektAccount | None = None) -> PersonalDecksResponse:
        return await self.personal_decks.list_personal_decks(account)

    def _build_personal_decks_response(
        self,
        resolved_account: AuthenticatedAccount,
        decks: list[PersonalDeckSummary],
    ) -> PersonalDecksResponse:
        return self.personal_decks.build_personal_decks_response(resolved_account, decks)

    async def search_archidekt_cards(
        self,
        filters: ArchidektCardSearchFilters,
    ) -> ArchidektCardSearchResponse:
        return await self.card_search.search_archidekt_cards(filters)

    async def get_personal_deck_cards(
        self,
        deck_id: int,
        include_deleted: bool = False,
        account: AuthenticatedAccount | ArchidektAccount | None = None,
    ) -> PersonalDeckCardsResponse:
        return await self.personal_decks.get_personal_deck_cards(deck_id, include_deleted, account)

    async def create_personal_deck(
        self,
        deck: PersonalDeckCreateInput,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        return await self.personal_decks.create_personal_deck(deck, account)

    async def update_personal_deck(
        self,
        deck_id: int,
        deck: PersonalDeckUpdateInput,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        return await self.personal_decks.update_personal_deck(deck_id, deck, account)

    async def delete_personal_deck(
        self,
        deck_id: int,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        return await self.personal_decks.delete_personal_deck(deck_id, account)

    async def modify_personal_deck_cards(
        self,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        return await self.personal_decks.modify_personal_deck_cards(deck_id, cards, account)

    async def _backfill_mutation_card_ids(
        self,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: AuthenticatedAccount,
    ) -> tuple[list[PersonalDeckCardMutation], list[str]]:
        return await self.personal_decks.backfill_mutation_card_ids(deck_id, cards, account)

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
        await self._mark_recent_collection_write(resolved_account, affected_games)
        return CollectionMutationResponse(
            action="upsert",
            account_username=resolved_account.username,
            affected_count=len(results),
            processed_at=datetime.now(UTC),
            notes=[
                "Public and authenticated collection caches were invalidated for the affected game(s).",
                "Authenticated reads of the same self collection will bypass cached snapshots briefly after this write to reduce stale reads.",
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
        await self._mark_recent_collection_write(resolved_account, affected_games)
        return CollectionMutationResponse(
            action="delete",
            account_username=resolved_account.username,
            affected_count=len(entries),
            processed_at=datetime.now(UTC),
            notes=[
                "Public and authenticated collection caches were invalidated for the affected game(s).",
                "Authenticated reads of the same self collection will bypass cached snapshots briefly after this write to reduce stale reads.",
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
        return await self.card_search.get_collection_overview(collection, account)

    async def search_owned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
        account: ArchidektAccount | None = None,
    ) -> SearchResponse:
        return await self.card_search.search_owned_cards(collection, filters, account)

    async def search_unowned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
        account: ArchidektAccount | None = None,
    ) -> SearchResponse:
        return await self.card_search.search_unowned_cards(collection, filters, account)

    async def _ensure_archidekt_card_ids(
        self,
        results: list[CardResult],
        *,
        game: int,
    ) -> None:
        await self.card_search.ensure_archidekt_card_ids(results, game=game)

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
        return await self.authenticated_cache.get_personal_deck_usage_snapshot(account, force_refresh)

    def _apply_personal_deck_usage(
        self,
        results: list[CardResult],
        usage_snapshot: PersonalDeckUsageSnapshot,
    ) -> None:
        _apply_personal_deck_usage_impl(results, usage_snapshot)

    def _map_personal_deck_card_record(self, raw_record: dict[str, Any]) -> PersonalDeckCardRecord:
        return self.personal_decks.map_personal_deck_card_record(raw_record)

    def _map_scryfall_card(self, card: dict[str, Any], filters: CardSearchFilters) -> CardResult:
        return self.card_search.map_scryfall_card(card, filters)

    async def aclose(self) -> None:
        await self.http_client.aclose()
        await self.redis_client.aclose()
