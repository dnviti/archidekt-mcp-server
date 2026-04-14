from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import httpx
import redis.asyncio as redis_async
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ValidationError
from redis.exceptions import RedisError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

if __package__ in {None, ""}:
    import sys

    package_root = Path(__file__).resolve().parents[1]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

    from archidekt_commander_mcp.clients import (
        ArchidektAuthenticatedClient,
        ArchidektPublicCollectionClient,
        CollectionCache,
        ScryfallClient,
        card_matches_scryfall_filters,
        deserialize_collection_snapshot,
        serialize_collection_snapshot,
        scryfall_price_key,
    )
    from archidekt_commander_mcp.config import RuntimeSettings
    from archidekt_commander_mcp.filtering import (
        aggregate_owned_results,
        build_type_line,
        paginate_results,
        record_matches_filters,
        sort_card_results,
    )
    from archidekt_commander_mcp.models import (
        ArchidektAccount,
        ArchidektCardSearchFilters,
        ArchidektCardSearchRequest,
        ArchidektCardSearchResponse,
        ArchidektLoginRequest,
        ArchidektLoginResponse,
        AuthenticatedAccount,
        CardResult,
        CardSearchFilters,
        CollectionCardDelete,
        CollectionCardUpsert,
        CollectionDeleteRequest,
        CollectionLocator,
        CollectionMutationResponse,
        CollectionOverview,
        CollectionOverviewRequest,
        CollectionUpsertRequest,
        CollectionSearchRequest,
        PersonalDeckCardMutation,
        PersonalDeckCardsMutationRequest,
        PersonalDeckCardRecord,
        PersonalDeckCardUsage,
        PersonalDeckCardsRequest,
        PersonalDeckCardsResponse,
        PersonalDeckCreateInput,
        PersonalDeckCreateRequest,
        PersonalDeckDeleteRequest,
        PersonalDeckMutationResponse,
        PersonalDecksRequest,
        PersonalDecksResponse,
        PersonalDeckSummary,
        PersonalDeckUpdateInput,
        PersonalDeckUpdateRequest,
        SearchResponse,
    )
    from archidekt_commander_mcp.mcp_auth import (
        AUTH_SCOPE,
        RedisArchidektOAuthProvider,
        account_from_access_token,
        render_archidekt_authorize_page,
    )
    from archidekt_commander_mcp.webui import render_home_page
else:
    from .clients import (
        ArchidektAuthenticatedClient,
        ArchidektPublicCollectionClient,
        CollectionCache,
        ScryfallClient,
        card_matches_scryfall_filters,
        deserialize_collection_snapshot,
        serialize_collection_snapshot,
        scryfall_price_key,
    )
    from .config import RuntimeSettings
    from .filtering import (
        aggregate_owned_results,
        build_type_line,
        paginate_results,
        record_matches_filters,
        sort_card_results,
    )
    from .models import (
        ArchidektAccount,
        ArchidektCardSearchFilters,
        ArchidektCardSearchRequest,
        ArchidektCardSearchResponse,
        ArchidektLoginRequest,
        ArchidektLoginResponse,
        AuthenticatedAccount,
        CardResult,
        CardSearchFilters,
        CollectionCardDelete,
        CollectionCardUpsert,
        CollectionDeleteRequest,
        CollectionLocator,
        CollectionMutationResponse,
        CollectionOverview,
        CollectionOverviewRequest,
        CollectionUpsertRequest,
        CollectionSearchRequest,
        PersonalDeckCardMutation,
        PersonalDeckCardsMutationRequest,
        PersonalDeckCardRecord,
        PersonalDeckCardUsage,
        PersonalDeckCardsRequest,
        PersonalDeckCardsResponse,
        PersonalDeckCreateInput,
        PersonalDeckCreateRequest,
        PersonalDeckDeleteRequest,
        PersonalDeckMutationResponse,
        PersonalDecksRequest,
        PersonalDecksResponse,
        PersonalDeckSummary,
        PersonalDeckUpdateInput,
        PersonalDeckUpdateRequest,
        SearchResponse,
    )
    from .mcp_auth import (
        AUTH_SCOPE,
        RedisArchidektOAuthProvider,
        account_from_access_token,
        render_archidekt_authorize_page,
    )
    from .webui import render_home_page


LOGGER = logging.getLogger("archidekt_commander_mcp.server")
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
ModelT = TypeVar("ModelT", bound=BaseModel)


def configure_logging(level_name: str) -> str:
    normalized_level = level_name.strip().upper() if level_name else "INFO"
    if normalized_level not in VALID_LOG_LEVELS:
        normalized_level = "INFO"

    level = getattr(logging, normalized_level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    return normalized_level


def describe_collection_locator(collection: CollectionLocator) -> str:
    return collection.display_locator


def describe_account(account: ArchidektAccount | AuthenticatedAccount | None) -> str:
    if account is None:
        return "none"
    if isinstance(account, AuthenticatedAccount):
        if account.username:
            return f"username={account.username}"
        if account.user_id is not None:
            return f"user_id={account.user_id}"
        return "token-provided"
    return account.display_identity


def account_from_auth_context() -> AuthenticatedAccount | None:
    return account_from_access_token(get_access_token())


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
        return self._locks.setdefault(key, asyncio.Lock())

    def _private_account_cache_key(self, account: AuthenticatedAccount) -> str:
        if account.user_id is not None:
            return f"user:{account.user_id}"
        if account.username:
            return f"username:{account.username.casefold()}"
        digest = hashlib.sha256(account.token.encode("utf-8")).hexdigest()
        return f"token:{digest[:16]}"

    def _private_snapshot_cache_key(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> str:
        return f"private-collection:{collection.cache_key}:{self._private_account_cache_key(account)}"

    def _private_usage_cache_key(self, account: AuthenticatedAccount) -> str:
        return f"private-decks:{self._private_account_cache_key(account)}"

    def _private_authenticated_deck_list_cache_key(self, account: AuthenticatedAccount) -> str:
        return f"authenticated-deck-list:{self._private_account_cache_key(account)}"

    def _deduplicate_personal_decks(
        self,
        decks: list[PersonalDeckSummary],
    ) -> list[PersonalDeckSummary]:
        deduplicated: list[PersonalDeckSummary] = []
        seen_ids: set[int] = set()
        for deck in decks:
            if deck.id in seen_ids:
                continue
            seen_ids.add(deck.id)
            deduplicated.append(deck)
        return deduplicated

    async def _get_authenticated_deck_list(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        cache_key = self._private_authenticated_deck_list_cache_key(account)
        async with self._lock_for_key(cache_key):
            if not force_refresh:
                cached = self._load_private_memory_cache(
                    self._authenticated_deck_list_cache, cache_key
                )
                if cached is not None:
                    return cached.account, cached.decks
                cached_redis = await self._load_authenticated_deck_list_from_redis(cache_key)
                if cached_redis is not None:
                    self._authenticated_deck_list_cache[cache_key] = (
                        datetime.now(UTC) + timedelta(seconds=self.settings.personal_deck_cache_ttl_seconds),
                        cached_redis,
                    )
                    return cached_redis.account, cached_redis.decks

            resolved_account, decks = await self.auth_client.list_personal_decks(account)
            snapshot = AuthenticatedDeckListSnapshot(
                account=resolved_account,
                decks=self._deduplicate_personal_decks(decks),
                fetched_at=datetime.now(UTC),
            )
            ttl = timedelta(seconds=self.settings.personal_deck_cache_ttl_seconds)
            self._authenticated_deck_list_cache[cache_key] = (datetime.now(UTC) + ttl, snapshot)
            await self._store_authenticated_deck_list_in_redis(cache_key, snapshot)
            return snapshot.account, snapshot.decks

    async def _load_authenticated_deck_list_from_redis(
        self, cache_key: str
    ) -> AuthenticatedDeckListSnapshot | None:
        try:
            redis_key = self._private_redis_key("authenticated-deck-list", cache_key)
            data = await self.redis_client.get(redis_key)
            if not data:
                return None
            obj = json.loads(data)
            return AuthenticatedDeckListSnapshot(
                account=AuthenticatedAccount.model_validate(obj["account"]),
                decks=[PersonalDeckSummary.model_validate(d) for d in obj["decks"]],
                fetched_at=datetime.fromisoformat(obj["fetched_at"]),
            )
        except Exception:
            return None

    async def _store_authenticated_deck_list_in_redis(
        self, cache_key: str, snapshot: AuthenticatedDeckListSnapshot
    ) -> None:
        try:
            redis_key = self._private_redis_key("authenticated-deck-list", cache_key)
            data = json.dumps({
                "account": snapshot.account.model_dump(mode="json"),
                "decks": [d.model_dump(mode="json") for d in snapshot.decks],
                "fetched_at": snapshot.fetched_at.isoformat(),
            })
            await self.redis_client.set(
                redis_key, data, ex=self.settings.personal_deck_cache_ttl_seconds
            )
        except Exception:
            pass

    async def _invalidate_authenticated_deck_list_cache(
        self, account: AuthenticatedAccount
    ) -> None:
        cache_key = self._private_authenticated_deck_list_cache_key(account)
        self._authenticated_deck_list_cache.pop(cache_key, None)
        try:
            redis_key = self._private_redis_key("authenticated-deck-list", cache_key)
            await self.redis_client.delete(redis_key)
        except Exception:
            pass

    def _collection_write_marker_key(self, account: AuthenticatedAccount, game: int) -> str:
        return f"{self._private_account_cache_key(account)}:game:{game}"

    def _private_redis_key(self, namespace: str, cache_key: str) -> str:
        return f"{self.settings.redis_key_prefix}:private:{namespace}:{cache_key}"

    async def _ensure_account_identity(self, account: AuthenticatedAccount) -> AuthenticatedAccount:
        if account.username and account.user_id is not None:
            return account
        resolved_account, _ = await self.auth_client.list_personal_decks(account)
        return resolved_account

    def _load_private_memory_cache(self, cache: dict[str, tuple[datetime, Any]], key: str) -> Any | None:
        entry = cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= datetime.now(UTC):
            cache.pop(key, None)
            return None
        return value

    def _store_private_memory_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        key: str,
        value: Any,
    ) -> None:
        ttl_seconds = self.settings.personal_deck_cache_ttl_seconds
        if ttl_seconds <= 0:
            return
        cache[key] = (datetime.now(UTC) + timedelta(seconds=ttl_seconds), value)

    async def _load_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        cached_value = await self._load_private_redis_cache(namespace, key, deserializer)
        if cached_value is not None:
            self._store_private_memory_cache(cache, key, cached_value)
            return cached_value
        return self._load_private_memory_cache(cache, key)

    async def _store_private_cache(
        self,
        cache: dict[str, tuple[datetime, Any]],
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        self._store_private_memory_cache(cache, key, value)
        await self._store_private_redis_cache(namespace, key, value, serializer)

    async def _load_private_redis_cache(
        self,
        namespace: str,
        key: str,
        deserializer: Callable[[dict[str, Any]], Any],
    ) -> Any | None:
        ttl_seconds = self.settings.personal_deck_cache_ttl_seconds
        if ttl_seconds <= 0:
            return None

        redis_key = self._private_redis_key(namespace, key)
        try:
            payload = await self.redis_client.get(redis_key)
        except RedisError as error:
            LOGGER.warning(
                "Redis private cache read failed for %s:%s; proceeding without cache: %s",
                namespace,
                key,
                error,
            )
            return None

        try:
            if not payload:
                return None

            wrapper = json.loads(payload)
            cached_payload = wrapper.get("payload")
            if not isinstance(cached_payload, dict):
                raise ValueError("incomplete private cache payload")

            value = deserializer(cached_payload)
            ttl = await self._private_redis_ttl(redis_key)
            if ttl is not None:
                LOGGER.info(
                    "Using Redis private cache for %s:%s; expires in %ss",
                    namespace,
                    key,
                    ttl,
                )
            else:
                LOGGER.info("Using Redis private cache for %s:%s", namespace, key)
            return value
        except RedisError as error:
            LOGGER.warning(
                "Redis private cache metadata read failed for %s:%s; proceeding without cache: %s",
                namespace,
                key,
                error,
            )
            return None
        except Exception as error:
            LOGGER.warning(
                "Failed to decode Redis private cache for %s:%s: %s",
                namespace,
                key,
                error,
            )
            await self._delete_private_redis_key(redis_key)
            return None

    async def _store_private_redis_cache(
        self,
        namespace: str,
        key: str,
        value: Any,
        serializer: Callable[[Any], dict[str, Any]],
    ) -> None:
        ttl_seconds = self.settings.personal_deck_cache_ttl_seconds
        if ttl_seconds <= 0:
            return

        redis_key = self._private_redis_key(namespace, key)
        payload = {
            "saved_at": datetime.now(UTC).isoformat(),
            "payload": serializer(value),
        }
        try:
            await self.redis_client.set(
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

    async def _private_redis_ttl(self, redis_key: str) -> int | None:
        ttl = await self.redis_client.ttl(redis_key)
        if ttl >= 0:
            return ttl
        if ttl == -1:
            return None
        return None

    async def _delete_private_redis_key(self, redis_key: str) -> None:
        try:
            await self.redis_client.delete(redis_key)
        except RedisError as error:
            LOGGER.warning("Failed to delete invalid Redis private cache key %s: %s", redis_key, error)

    def _account_collection_locators(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> list[CollectionLocator]:
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
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        valid_cache_keys = {
            locator.cache_key
            for locator in self._account_collection_locators(account, games={collection.game})
        }
        return collection.cache_key in valid_cache_keys

    def _mark_recent_collection_write(
        self,
        account: AuthenticatedAccount,
        games: set[int],
    ) -> None:
        marked_at = datetime.now(UTC)
        for game in games:
            self._recent_collection_write_markers[
                self._collection_write_marker_key(account, game)
            ] = marked_at

    def _consume_recent_collection_write(
        self,
        collection: CollectionLocator,
        account: AuthenticatedAccount,
    ) -> bool:
        if not self._is_self_collection_locator(collection, account):
            return False
        marker_key = self._collection_write_marker_key(account, collection.game)
        marked_at = self._recent_collection_write_markers.get(marker_key)
        if marked_at is None:
            return False
        if (datetime.now(UTC) - marked_at) > timedelta(minutes=2):
            self._recent_collection_write_markers.pop(marker_key, None)
            return False
        self._recent_collection_write_markers.pop(marker_key, None)
        return True

    async def _invalidate_personal_deck_usage_cache(self, account: AuthenticatedAccount) -> None:
        cache_key = self._private_usage_cache_key(account)
        self._personal_deck_usage_cache.pop(cache_key, None)
        await self._delete_private_redis_key(self._private_redis_key("personal-decks", cache_key))

    async def _invalidate_personal_deck_caches(self, account: AuthenticatedAccount) -> None:
        await self._invalidate_authenticated_deck_list_cache(account)
        await self._invalidate_personal_deck_usage_cache(account)

    async def _invalidate_collection_caches(
        self,
        account: AuthenticatedAccount,
        games: set[int] | None = None,
    ) -> None:
        for locator in self._account_collection_locators(account, games):
            private_key = self._private_snapshot_cache_key(locator, account)
            self._private_snapshot_cache.pop(private_key, None)
            await self._delete_private_redis_key(self._private_redis_key("collection", private_key))
            await self.cache.invalidate_snapshot(locator)

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
        account: ArchidektAccount | None = None,
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
        results = []
        affected_games: set[int] = set()
        for entry in entries:
            payload = await self.auth_client.upsert_collection_entry(resolved_account, entry)
            results.append(
                {
                    "operation": "updated" if entry.record_id is not None else "created",
                    "record_id": _safe_int(payload.get("id") or payload.get("recordId")) or entry.record_id,
                    "card_id": entry.card_id,
                    "game": entry.game,
                    "result": payload,
                }
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
                {
                    "operation": "deleted",
                    "record_id": entry.record_id,
                    "game": entry.game,
                    "result": payload,
                }
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
        if account is None:
            return account_from_auth_context()
        if isinstance(account, AuthenticatedAccount):
            return account
        return await self.auth_client.resolve_account(account)

    async def _coerce_account(
        self,
        account: AuthenticatedAccount | ArchidektAccount | None,
    ) -> AuthenticatedAccount:
        if account is None:
            context_account = account_from_auth_context()
            if context_account is None:
                raise RuntimeError(
                    "Authenticated Archidekt access requires either an `account` payload or an MCP-authenticated session."
                )
            return context_account
        if isinstance(account, AuthenticatedAccount):
            return account
        return await self.auth_client.resolve_account(account)

    async def _get_personal_deck_usage_snapshot(
        self,
        account: AuthenticatedAccount,
        force_refresh: bool = False,
    ) -> PersonalDeckUsageSnapshot:
        cache_key = self._private_usage_cache_key(account)
        async with self._lock_for_key(cache_key):
            if not force_refresh:
                cached_snapshot = await self._load_private_cache(
                    self._personal_deck_usage_cache,
                    "personal-decks",
                    cache_key,
                    _deserialize_personal_deck_usage_snapshot,
                )
                if cached_snapshot is not None:
                    return cached_snapshot

            resolved_account, decks = await self.auth_client.list_personal_decks(account)
            usage_by_oracle_id: dict[str, list[PersonalDeckCardUsage]] = {}
            usage_by_name: dict[str, list[PersonalDeckCardUsage]] = {}

            semaphore = asyncio.Semaphore(6)

            async def fetch_one(deck: PersonalDeckSummary) -> tuple[PersonalDeckSummary, dict[str, Any]]:
                async with semaphore:
                    payload = await self.auth_client.fetch_deck_cards(
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
                if isinstance(result, Exception):
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
            await self._store_private_cache(
                self._personal_deck_usage_cache,
                "personal-decks",
                cache_key,
                snapshot,
                _serialize_personal_deck_usage_snapshot,
            )
            return snapshot

    def _apply_personal_deck_usage(
        self,
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


def build_arg_parser() -> argparse.ArgumentParser:
    env_settings = RuntimeSettings()
    parser = argparse.ArgumentParser(description="Archidekt Commander MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=env_settings.transport,
        help="MCP transport to use. Default: streamable-http.",
    )
    parser.add_argument(
        "--host",
        default=env_settings.host,
        help="Bind host for the Web UI / HTTP MCP server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_settings.port,
        help="Bind port for the Web UI / HTTP MCP server.",
    )
    parser.add_argument(
        "--log-level",
        default=env_settings.log_level,
        help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=env_settings.cache_ttl_seconds,
        help="Redis TTL in seconds for collection snapshots.",
    )
    parser.add_argument(
        "--personal-deck-cache-ttl-seconds",
        type=int,
        default=env_settings.personal_deck_cache_ttl_seconds,
        help="In-memory TTL in seconds for authenticated collection and personal deck usage snapshots.",
    )
    parser.add_argument(
        "--redis-url",
        default=env_settings.redis_url,
        help="Redis connection URL for the shared collection cache.",
    )
    parser.add_argument(
        "--redis-key-prefix",
        default=env_settings.redis_key_prefix,
        help="Prefix used for Redis keys created by this server.",
    )
    parser.add_argument(
        "--http-timeout-seconds",
        type=float,
        default=env_settings.http_timeout_seconds,
        help="HTTP timeout for Archidekt and Scryfall requests.",
    )
    parser.add_argument(
        "--max-search-results",
        type=int,
        default=env_settings.max_search_results,
        help="Maximum number of results returned per search page.",
    )
    parser.add_argument(
        "--scryfall-max-pages",
        type=int,
        default=env_settings.scryfall_max_pages,
        help="Maximum number of Scryfall pages scanned for unowned searches.",
    )
    parser.add_argument(
        "--user-agent",
        default=env_settings.user_agent,
        help="User-Agent sent to Archidekt and Scryfall.",
    )
    parser.add_argument(
        "--streamable-http-path",
        default=env_settings.streamable_http_path,
        help="HTTP path used by the streamable-http MCP transport.",
    )
    return parser


def build_runtime_settings_from_args(args: argparse.Namespace) -> RuntimeSettings:
    return RuntimeSettings(
        transport=args.transport,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        cache_ttl_seconds=args.cache_ttl_seconds,
        personal_deck_cache_ttl_seconds=args.personal_deck_cache_ttl_seconds,
        redis_url=args.redis_url,
        redis_key_prefix=args.redis_key_prefix,
        http_timeout_seconds=args.http_timeout_seconds,
        max_search_results=args.max_search_results,
        scryfall_max_pages=args.scryfall_max_pages,
        user_agent=args.user_agent,
        streamable_http_path=args.streamable_http_path,
    )


def main() -> None:
    args = build_arg_parser().parse_args()
    runtime = build_runtime_settings_from_args(args)
    configure_logging(runtime.log_level)

    if runtime.transport == "streamable-http":
        LOGGER.info(
            "Serving Web UI at http://%s:%s/ and MCP at http://%s:%s%s",
            runtime.host,
            runtime.port,
            runtime.host,
            runtime.port,
            runtime.streamable_http_path,
        )
    elif runtime.transport == "sse":
        LOGGER.info("Serving SSE MCP transport on http://%s:%s/", runtime.host, runtime.port)
    else:
        LOGGER.info("Serving stdio MCP transport")

    server = create_server(runtime)
    server.run(transport=runtime.transport)


def _extract_face_image(card_faces: list[dict[str, Any]]) -> str | None:
    for face in card_faces:
        image_uris = face.get("image_uris") or {}
        if image_uris.get("normal"):
            return image_uris["normal"]
        if image_uris.get("large"):
            return image_uris["large"]
    return None


def _parse_datetime(raw_value: Any) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_float(raw_value: Any) -> float | None:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _safe_int(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


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


def _extract_deck_id(payload: dict[str, Any]) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("id"),
        (payload.get("deck") or {}).get("id") if isinstance(payload.get("deck"), dict) else None,
        (payload.get("result") or {}).get("id") if isinstance(payload.get("result"), dict) else None,
    ]
    for candidate in candidates:
        parsed = _safe_int(candidate)
        if parsed is not None:
            return parsed
    return None


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


if __package__ in {None, ""}:
    from archidekt_commander_mcp.app_factory import create_server
else:
    from .app_factory import create_server


app = create_server()
mcp = app


if __name__ == "__main__":
    main()
