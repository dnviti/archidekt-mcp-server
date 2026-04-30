from __future__ import annotations

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import redis.asyncio as redis_async
from redis.exceptions import RedisError

from ..schemas.accounts import ArchidektAccount, AuthenticatedAccount
from ..schemas.cards import ArchidektCardReference
from ..schemas.collections import CollectionCardUpsert
from ..schemas.decks import PersonalDeckCardMutation, PersonalDeckCreateInput, PersonalDeckSummary, PersonalDeckUpdateInput
from ..schemas.search import ArchidektCardSearchFilters
from .http_base import _ArchidektHttpClientBase, _auth_headers, _json_headers
from .request_gate import ArchidektRequestGate
from .serialization import (
    _compact_text,
    _dedupe_personal_decks,
    _deserialize_archidekt_card_reference,
    _ensure_mapping,
    _normalize_next_url,
    _parse_datetime,
    _safe_float,
    _safe_int,
    _serialize_archidekt_card_reference,
)


LOGGER = logging.getLogger("archidekt_commander_mcp.clients")


class ArchidektAuthenticatedClient(_ArchidektHttpClientBase):
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings,
        request_gate: ArchidektRequestGate | None = None,
        redis_client: redis_async.Redis | None = None,
        renew_account: Callable[[AuthenticatedAccount], Awaitable[AuthenticatedAccount | None]] | None = None,
    ) -> None:
        super().__init__(http_client, settings, request_gate=request_gate)
        self.redis = redis_client
        self.renew_account = renew_account
        self._exact_name_search_cache: dict[
            str,
            tuple[
                datetime,
                tuple[list[ArchidektCardReference], int | None, bool | None],
            ],
        ] = {}

    async def login(self, account: ArchidektAccount) -> AuthenticatedAccount:
        if account.token and account.password is None:
            return AuthenticatedAccount(
                token=account.token,
            )

        payload: dict[str, Any] = {"password": account.password}
        if account.email:
            payload["email"] = account.email
        else:
            payload["username"] = account.username

        LOGGER.info("Logging into Archidekt with %s", account.display_identity)
        response = await self._request_archidekt(
            "POST",
            f"{self.settings.normalized_archidekt_base_url}/api/rest-auth/login/",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()

        response_payload = response.json()
        user = response_payload.get("user") or {}
        token = response_payload.get("token")

        if not token:
            raise RuntimeError("Archidekt login did not return an access token.")

        return AuthenticatedAccount(
            token=str(token),
            username=_compact_text(user.get("username")),
            user_id=_safe_int(user.get("id")),
        )

    async def resolve_account(self, account: ArchidektAccount) -> AuthenticatedAccount:
        if not account.token:
            return await self.login(account)

        recent_decks = await self._fetch_curated_self(account.token)
        inferred_username = recent_decks[0].owner_username if recent_decks else None
        inferred_user_id = recent_decks[0].owner_id if recent_decks else None
        return AuthenticatedAccount(
            token=account.token,
            username=inferred_username,
            user_id=inferred_user_id,
        )

    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        resolved = await self._coerce_account(account)

        recent_decks: list[PersonalDeckSummary] = []
        if resolved.username is None or resolved.user_id is None:
            resolved, recent_decks = await self._fetch_curated_self_for_account(resolved)
            resolved = resolved.model_copy(
                update={
                    "username": resolved.username or (recent_decks[0].owner_username if recent_decks else None),
                    "user_id": (
                        resolved.user_id
                        if resolved.user_id is not None
                        else (recent_decks[0].owner_id if recent_decks else None)
                    ),
                }
            )

        if not resolved.username:
            return resolved, recent_decks

        next_url: str | None = (
            f"{self.settings.normalized_archidekt_base_url}/api/decks/v3/?"
            + urlencode(
                {
                    "ownerUsername": resolved.username,
                    "showAll": "true",
                    "page": 1,
                    "pageSize": page_size,
                }
            )
        )
        decks: list[PersonalDeckSummary] = []
        while next_url:
            resolved, response = await self._request_authenticated(
                resolved,
                "GET",
                next_url,
                headers_factory=_auth_headers,
            )
            response.raise_for_status()
            payload = response.json()
            decks.extend(
                self._map_personal_deck_summary(item) for item in (payload.get("results") or [])
            )
            next_url = _normalize_next_url(
                payload.get("next"),
                self.settings.normalized_archidekt_base_url,
            )

        if not decks and recent_decks:
            decks = recent_decks

        return resolved, _dedupe_personal_decks(decks)

    async def _request_authenticated(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        method: str,
        url: str,
        *,
        headers_factory: Callable[[str | None], dict[str, str]],
        **kwargs: Any,
    ) -> tuple[AuthenticatedAccount, httpx.Response]:
        resolved = await self._coerce_account(account)
        response = await self._request_archidekt(
            method,
            url,
            headers=headers_factory(resolved.token),
            **kwargs,
        )
        if response.status_code not in {401, 403} or self.renew_account is None:
            return resolved, response

        renewed = await self.renew_account(resolved)
        if renewed is None:
            return resolved, response

        resolved.token = renewed.token
        resolved.username = renewed.username or resolved.username
        resolved.user_id = renewed.user_id if renewed.user_id is not None else resolved.user_id
        resolved.auth_session_id = renewed.auth_session_id or resolved.auth_session_id
        retry_response = await self._request_archidekt(
            method,
            url,
            headers=headers_factory(resolved.token),
            **kwargs,
        )
        return resolved, retry_response

    async def search_cards(
        self,
        filters: ArchidektCardSearchFilters,
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None]:
        exact_names = list(dict.fromkeys(filters.exact_name))
        if len(exact_names) <= 1:
            requested_exact_name = exact_names[0] if exact_names else None
            return await self._search_cards_once(filters, requested_exact_name=requested_exact_name)

        searches = await asyncio.gather(
            *[
                self._search_cards_once(filters, requested_exact_name=exact_name)
                for exact_name in exact_names
            ]
        )
        combined_results: list[ArchidektCardReference] = []
        total_matches_parts: list[int] = []
        has_more_flags: list[bool | None] = []

        for mapped, total_matches, has_more in searches:
            combined_results.extend(mapped)
            if total_matches is not None:
                total_matches_parts.append(total_matches)
            has_more_flags.append(has_more)

        combined_total_matches = (
            sum(total_matches_parts) if len(total_matches_parts) == len(searches) else None
        )
        if any(flag is True for flag in has_more_flags):
            combined_has_more: bool | None = True
        elif all(flag is False for flag in has_more_flags):
            combined_has_more = False
        else:
            combined_has_more = None
        return combined_results, combined_total_matches, combined_has_more

    async def _search_cards_once(
        self,
        filters: ArchidektCardSearchFilters,
        requested_exact_name: str | None = None,
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None]:
        cache_key = self._exact_name_cache_key(filters, requested_exact_name)
        if cache_key is not None:
            cached = self._load_exact_name_cache(cache_key)
            if cached is not None:
                return cached
            redis_cached = await self._load_exact_name_cache_from_redis(cache_key)
            if redis_cached is not None:
                self._store_exact_name_cache(cache_key, redis_cached)
                return redis_cached
        params = self._card_search_params(filters, requested_exact_name=requested_exact_name)
        response = await self._request_archidekt(
            "GET",
            f"{self.settings.normalized_archidekt_base_url}/api/cards/v2/",
            params=params,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Archidekt card search returned an invalid payload.")

        results = payload.get("results") or []
        mapped = [
            self._map_archidekt_card_reference(item, requested_exact_name=requested_exact_name)
            for item in results
            if isinstance(item, dict)
        ]
        total_matches = _safe_int(payload.get("count"))
        has_more = bool(payload.get("next")) if payload.get("next") is not None else None
        result = (mapped, total_matches, has_more)
        if cache_key is not None:
            self._store_exact_name_cache(cache_key, result)
            await self._store_exact_name_cache_in_redis(cache_key, result)
        return result

    def _exact_name_cache_key(
        self,
        filters: ArchidektCardSearchFilters,
        requested_exact_name: str | None,
    ) -> str | None:
        if self.settings.archidekt_exact_name_cache_ttl_seconds <= 0:
            return None
        if not requested_exact_name or filters.query:
            return None
        return f"{requested_exact_name.casefold()}:{filters.game}:{filters.page}:{filters.edition_code or ''}:{filters.include_tokens}:{filters.include_digital}:{filters.all_editions}"

    def _load_exact_name_cache(
        self, cache_key: str
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None] | None:
        entry = self._exact_name_search_cache.get(cache_key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= datetime.now(UTC):
            self._exact_name_search_cache.pop(cache_key, None)
            return None
        return value

    def _store_exact_name_cache(
        self,
        cache_key: str,
        value: tuple[list[ArchidektCardReference], int | None, bool | None],
    ) -> None:
        ttl = timedelta(seconds=self.settings.archidekt_exact_name_cache_ttl_seconds)
        self._exact_name_search_cache[cache_key] = (datetime.now(UTC) + ttl, value)

    def _exact_name_redis_key(self, cache_key: str) -> str:
        prefix = self.settings.redis_key_prefix.strip(":") or "archidekt-commander"
        return f"{prefix}:catalog:exact-name:{cache_key}"

    async def _load_exact_name_cache_from_redis(
        self, cache_key: str
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None] | None:
        if self.redis is None:
            return None
        redis_key = self._exact_name_redis_key(cache_key)
        try:
            data = await self.redis.get(redis_key)
        except RedisError as error:
            LOGGER.warning(
                "Redis exact-name cache read failed for %s; proceeding without cache: %s",
                cache_key,
                error,
            )
            return None
        if not data:
            return None
        try:
            payload = json.loads(data)
            results = [
                _deserialize_archidekt_card_reference(item)
                for item in payload.get("results") or []
            ]
            total_matches = _safe_int(payload.get("total_matches"))
            has_more = payload.get("has_more")
            if has_more is not None:
                has_more = bool(has_more)
            return results, total_matches, has_more
        except Exception as error:
            LOGGER.warning(
                "Failed to decode Redis exact-name cache for %s: %s",
                cache_key,
                error,
            )
            try:
                await self.redis.delete(redis_key)
            except RedisError:
                pass
            return None

    async def _store_exact_name_cache_in_redis(
        self,
        cache_key: str,
        value: tuple[list[ArchidektCardReference], int | None, bool | None],
    ) -> None:
        if self.redis is None:
            return
        redis_key = self._exact_name_redis_key(cache_key)
        results, total_matches, has_more = value
        payload = {
            "results": [_serialize_archidekt_card_reference(ref) for ref in results],
            "total_matches": total_matches,
            "has_more": has_more,
        }
        try:
            await self.redis.set(
                redis_key,
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
                ex=self.settings.archidekt_exact_name_cache_ttl_seconds,
            )
        except RedisError as error:
            LOGGER.warning(
                "Redis exact-name cache write failed for %s; continuing without persisted cache: %s",
                cache_key,
                error,
            )

    async def fetch_deck_cards(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        include_deleted_flag = "1" if include_deleted else "0"
        _, response = await self._request_authenticated(
            account,
            "GET",
            f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/v2/cards/",
            params={"includeDeleted": include_deleted_flag},
            headers_factory=_auth_headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Archidekt deck cards endpoint returned an invalid payload.")
        return payload

    async def create_deck(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck: PersonalDeckCreateInput,
    ) -> tuple[dict[str, Any], PersonalDeckSummary | None]:
        _, response = await self._request_authenticated(
            account,
            "POST",
            f"{self.settings.normalized_archidekt_base_url}/api/decks/v2/",
            json=self._deck_create_payload(deck),
            headers_factory=_json_headers,
        )
        response.raise_for_status()
        payload = _ensure_mapping(response.json(), "Archidekt deck create")
        return payload, self._coerce_personal_deck_summary(payload)

    async def update_deck(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
        deck: PersonalDeckUpdateInput,
    ) -> tuple[dict[str, Any], PersonalDeckSummary | None]:
        _, response = await self._request_authenticated(
            account,
            "PATCH",
            f"{self.settings.normalized_archidekt_base_url}/api/decks/v2/{deck_id}/",
            json=self._deck_update_payload(deck),
            headers_factory=_json_headers,
        )
        response.raise_for_status()
        payload = _ensure_mapping(response.json(), "Archidekt deck update")
        return payload, self._coerce_personal_deck_summary(payload)

    async def delete_deck(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
    ) -> dict[str, Any]:
        _, response = await self._request_authenticated(
            account,
            "DELETE",
            f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/v2/",
            headers_factory=_json_headers,
        )
        response.raise_for_status()
        return _ensure_mapping(response.json(), "Archidekt deck delete")

    async def modify_deck_cards(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
    ) -> dict[str, Any]:
        payload_cards = [
            self._deck_card_mutation_payload(card, index)
            for index, card in enumerate(cards, start=1)
        ]
        endpoint = f"{self.settings.normalized_archidekt_base_url}/api/decks/{deck_id}/modifyCards/v2/"
        resolved, response = await self._request_authenticated(
            account,
            "PATCH",
            endpoint,
            json={"cards": payload_cards},
            headers_factory=_json_headers,
        )
        headers = _json_headers(resolved.token)
        if response.status_code < 400:
            return _ensure_mapping(response.json(), "Archidekt deck card modification")

        batch_error = self._remote_error_payload(response)
        if response.status_code != 400 or len(payload_cards) <= 1:
            raise RuntimeError(
                self._format_remote_error(
                    "Archidekt deck card modification",
                    response,
                )
            )

        successful_mutations: list[dict[str, Any]] = []
        failed_mutations: list[dict[str, Any]] = []
        for payload_card in payload_cards:
            single_response = await self._request_archidekt(
                "PATCH",
                endpoint,
                json={"cards": [payload_card]},
                headers=headers,
            )
            if single_response.status_code < 400:
                successful_mutations.append(
                    {
                        "request": payload_card,
                        "response": _ensure_mapping(
                            single_response.json(),
                            "Archidekt deck card modification",
                        ),
                    }
                )
                continue

            failed_mutations.append(
                {
                    "request": payload_card,
                    "status_code": single_response.status_code,
                    "error": self._remote_error_payload(single_response),
                }
            )

        if not successful_mutations:
            raise RuntimeError(
                "Archidekt rejected the deck card mutation batch and every per-card retry failed. "
                f"Batch error: {batch_error}. "
                f"Per-card errors: {json.dumps(failed_mutations, ensure_ascii=True, separators=(',', ':'))}"
            )

        return {
            "batch_error": batch_error,
            "successful_mutations": successful_mutations,
            "failed_mutations": failed_mutations,
            "successful_count": len(successful_mutations),
            "failed_count": len(failed_mutations),
        }

    async def upsert_collection_entry(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        entry: CollectionCardUpsert,
    ) -> dict[str, Any]:
        if entry.record_id is None:
            method = "POST"
            endpoint = f"{self.settings.normalized_archidekt_base_url}/api/collection/v2/"
        else:
            method = "PATCH"
            endpoint = (
                f"{self.settings.normalized_archidekt_base_url}/api/collection/v2/{entry.record_id}/"
            )

        _, response = await self._request_authenticated(
            account,
            method,
            endpoint,
            json=self._collection_upsert_payload(entry),
            headers_factory=_json_headers,
        )
        response.raise_for_status()
        return _ensure_mapping(response.json(), "Archidekt collection upsert")

    async def delete_collection_entries(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        record_ids: list[int],
    ) -> dict[str, Any]:
        _, response = await self._request_authenticated(
            account,
            "DELETE",
            f"{self.settings.normalized_archidekt_base_url}/api/collection/bulk/",
            content=json.dumps({"ids": [int(record_id) for record_id in record_ids]}),
            headers_factory=_json_headers,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            return payload
        return {"deleted_ids": [int(record_id) for record_id in record_ids]}

    async def _fetch_curated_self(self, token: str) -> list[PersonalDeckSummary]:
        response = await self._request_archidekt(
            "GET",
            f"{self.settings.normalized_archidekt_base_url}/api/decks/curated/self/",
            headers=_auth_headers(token),
        )
        response.raise_for_status()
        return self._map_curated_self_payload(response.json())

    async def _fetch_curated_self_for_account(
        self,
        account: AuthenticatedAccount,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        resolved, response = await self._request_authenticated(
            account,
            "GET",
            f"{self.settings.normalized_archidekt_base_url}/api/decks/curated/self/",
            headers_factory=_auth_headers,
        )
        response.raise_for_status()
        return resolved, self._map_curated_self_payload(response.json())

    def _map_curated_self_payload(self, payload: Any) -> list[PersonalDeckSummary]:
        if isinstance(payload, dict):
            raw_results = payload.get("results") or payload.get("decks") or []
        elif isinstance(payload, list):
            raw_results = payload
        else:
            raw_results = []

        return [self._map_personal_deck_summary(item) for item in raw_results]

    async def _coerce_account(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
    ) -> AuthenticatedAccount:
        if isinstance(account, AuthenticatedAccount):
            return account
        return await self.resolve_account(account)

    def _map_personal_deck_summary(self, payload: dict[str, Any]) -> PersonalDeckSummary:
        owner = payload.get("owner") or {}
        colors = payload.get("colors") or {}
        return PersonalDeckSummary(
            id=int(payload["id"]),
            name=str(payload.get("name") or ""),
            size=_safe_int(payload.get("size")),
            deck_format=_safe_int(payload.get("deckFormat")),
            edh_bracket=_safe_int(payload.get("edhBracket")),
            private=bool(payload.get("private")),
            unlisted=bool(payload.get("unlisted")),
            theorycrafted=bool(payload.get("theorycrafted")),
            game=_safe_int(payload.get("game")),
            tags=[str(tag) for tag in (payload.get("tags") or []) if tag],
            parent_folder_id=_safe_int(payload.get("parentFolderId")),
            has_primer=bool(payload.get("hasPrimer")),
            created_at=_parse_datetime(payload.get("createdAt")),
            updated_at=_parse_datetime(payload.get("updatedAt")),
            featured=payload.get("featured") or None,
            custom_featured=payload.get("customFeatured") or None,
            owner_id=_safe_int(owner.get("id")),
            owner_username=_compact_text(owner.get("username")),
            colors={
                str(key): int(value)
                for key, value in colors.items()
                if value not in {None, ""} and _safe_int(value) is not None
            },
        )

    def _card_search_params(
        self,
        filters: ArchidektCardSearchFilters,
        requested_exact_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": filters.page, "game": filters.game}
        if requested_exact_name:
            params["name"] = requested_exact_name
            params["exact"] = ""
        elif filters.query:
            params["nameSearch"] = filters.query
        if filters.edition_code:
            params["edition"] = filters.edition_code
        if filters.include_tokens:
            params["includeTokens"] = ""
        if filters.include_digital:
            params["includeDigital"] = ""
        if not filters.all_editions:
            params["unique"] = ""
        return params

    def _map_archidekt_card_reference(
        self,
        payload: dict[str, Any],
        requested_exact_name: str | None = None,
    ) -> ArchidektCardReference:
        oracle_card = payload.get("oracleCard") or {}
        edition = payload.get("edition") or {}
        prices = payload.get("prices") or {}
        return ArchidektCardReference(
            card_id=int(payload["id"]),
            requested_exact_name=requested_exact_name,
            uid=_compact_text(payload.get("uid")),
            oracle_card_id=_safe_int(oracle_card.get("id")),
            oracle_id=_compact_text(oracle_card.get("uid")),
            name=str(oracle_card.get("name") or payload.get("name") or ""),
            display_name=_compact_text(payload.get("displayName")),
            mana_cost=_compact_text(oracle_card.get("manaCost")),
            cmc=_safe_float(oracle_card.get("cmc")),
            oracle_text=_compact_text(oracle_card.get("text")),
            colors=[str(item) for item in (oracle_card.get("colors") or []) if item],
            color_identity=[str(item) for item in (oracle_card.get("colorIdentity") or []) if item],
            supertypes=[str(item) for item in (oracle_card.get("superTypes") or []) if item],
            types=[str(item) for item in (oracle_card.get("types") or []) if item],
            subtypes=[str(item) for item in (oracle_card.get("subTypes") or []) if item],
            set_code=_compact_text(edition.get("editioncode")),
            set_name=_compact_text(edition.get("editionname")),
            rarity=_compact_text(payload.get("rarity")),
            released_at=_parse_datetime(payload.get("releasedAt")),
            prices={str(key): _safe_float(value) for key, value in prices.items()},
            owned=_safe_int(payload.get("owned")),
            default_category=_compact_text(oracle_card.get("defaultCategory")),
        )

    def _coerce_personal_deck_summary(self, payload: dict[str, Any]) -> PersonalDeckSummary | None:
        candidate = payload
        if isinstance(payload.get("deck"), dict):
            candidate = payload["deck"]
        elif isinstance(payload.get("result"), dict):
            candidate = payload["result"]

        if not isinstance(candidate, dict) or candidate.get("id") in {None, ""}:
            return None

        try:
            return self._map_personal_deck_summary(candidate)
        except Exception:
            return None

    def _deck_create_payload(self, deck: PersonalDeckCreateInput) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": deck.name,
            "deckFormat": deck.deck_format,
            "edhBracket": deck.edh_bracket,
            "description": deck.description or "",
            "featured": deck.featured,
            "playmat": deck.playmat,
            "copyId": deck.copy_id,
            "private": deck.private,
            "unlisted": deck.unlisted,
            "theorycrafted": deck.theorycrafted,
            "game": deck.game,
            "parent_folder": deck.parent_folder_id,
            "cardPackage": deck.card_package,
            "extras": deck.extras,
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _deck_update_payload(self, deck: PersonalDeckUpdateInput) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": deck.name,
            "deckFormat": deck.deck_format,
            "edhBracket": deck.edh_bracket,
            "description": deck.description,
            "featured": deck.featured,
            "playmat": deck.playmat,
            "copyId": deck.copy_id,
            "private": deck.private,
            "unlisted": deck.unlisted,
            "theorycrafted": deck.theorycrafted,
            "game": deck.game,
            "parent_folder": deck.parent_folder_id,
            "cardPackage": deck.card_package,
            "extras": deck.extras,
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _deck_card_mutation_payload(
        self,
        card: PersonalDeckCardMutation,
        index: int,
    ) -> dict[str, Any]:
        patch_id = (
            card.patch_id
            or f"mcp-{index}-{card.card_id or card.custom_card_id or card.deck_relation_id or 'card'}"
        )
        normalized_action = card.action
        normalized_categories = list(card.categories)
        modifications = {
            key: value
            for key, value in {
                "quantity": card.modifications.quantity,
                "modifier": card.modifications.modifier,
                "customCmc": card.modifications.custom_cmc,
                "companion": card.modifications.companion,
                "flippedDefault": card.modifications.flipped_default,
                "label": card.modifications.label,
            }.items()
            if value is not None
        }
        if card.action == "modify" and card.modifications.quantity == 0:
            normalized_action = "remove"
            normalized_categories = []
            modifications = {}
        payload: dict[str, Any] = {
            "action": normalized_action,
            "patchId": patch_id,
        }
        if normalized_categories:
            payload["categories"] = normalized_categories
        if modifications:
            payload["modifications"] = modifications
        if card.card_id is not None:
            payload["cardid"] = card.card_id
        if card.custom_card_id is not None:
            payload["customCardId"] = card.custom_card_id
        if card.deck_relation_id is not None:
            payload["deckRelationId"] = card.deck_relation_id
        return payload

    def _remote_error_payload(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        except Exception:
            return (response.text or "").strip() or f"HTTP {response.status_code}"

    def _format_remote_error(self, context: str, response: httpx.Response) -> str:
        return (
            f"{context} failed with HTTP {response.status_code}: "
            f"{self._remote_error_payload(response)}"
        )

    def _collection_upsert_payload(self, entry: CollectionCardUpsert) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "game": entry.game,
            "id": entry.record_id,
            "quantity": entry.quantity,
            "card": entry.card_id,
            "modifier": entry.modifier,
            "language": entry.language,
            "condition": entry.condition,
            "tags": entry.tags,
            "purchasePrice": entry.purchase_price,
        }
        return {key: value for key, value in payload.items() if value is not None}
