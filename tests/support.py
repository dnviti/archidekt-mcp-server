from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.exceptions import RedisError

from archidekt_commander_mcp.models import (
    ArchidektAccount,
    ArchidektCardReference,
    ArchidektCardSearchFilters,
    AuthenticatedAccount,
    CardSearchFilters,
    CollectionCardUpsert,
    CollectionLocator,
    CollectionSnapshot,
    PersonalDeckCreateInput,
    PersonalDeckSummary,
)

class FakeCollectionClient:
    def __init__(self, snapshot: CollectionSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.auth_tokens: list[str | None] = []

    async def fetch_snapshot(
        self,
        collection: CollectionLocator,
        auth_token: str | None = None,
    ) -> CollectionSnapshot:
        del collection
        self.calls += 1
        self.auth_tokens.append(auth_token)
        return self.snapshot


class FakeAuthMutationClient:
    def __init__(self) -> None:
        self.upsert_calls: list[CollectionCardUpsert] = []
        self.delete_calls: list[list[int]] = []
        self.modify_calls: list[list[object]] = []

    async def upsert_collection_entry(
        self,
        account: AuthenticatedAccount,
        entry: CollectionCardUpsert,
    ) -> dict[str, object]:
        del account
        self.upsert_calls.append(entry)
        return {"id": entry.record_id or 9001, "card": entry.card_id, "quantity": entry.quantity}

    async def delete_collection_entries(
        self,
        account: AuthenticatedAccount,
        record_ids: list[int],
    ) -> dict[str, object]:
        del account
        self.delete_calls.append(record_ids)
        return {"deleted_ids": record_ids}

    async def fetch_deck_cards(
        self,
        account: AuthenticatedAccount,
        deck_id: int,
        include_deleted: bool = False,
    ) -> dict[str, object]:
        del account
        del include_deleted
        return {
            "deckId": deck_id,
            "cards": [
                {
                    "id": 77,
                    "quantity": 1,
                    "categories": ["Ramp"],
                    "card": {
                        "id": 150824,
                        "uid": "870ec754-a76c-40ea-9b81-81b3dca1f62c",
                        "displayName": "Sol Ring",
                        "oracleCard": {
                            "id": 15342,
                            "uid": "6ad8011d-3471-4369-9d68-b264cc027487",
                            "name": "Sol Ring",
                            "manaCost": "{1}",
                            "cmc": 1,
                            "text": "{T}: Add {C}{C}.",
                            "superTypes": [],
                            "types": ["Artifact"],
                            "subTypes": [],
                        },
                    },
                }
            ],
        }

    async def modify_deck_cards(
        self,
        account: AuthenticatedAccount,
        deck_id: int,
        cards: list[object],
    ) -> dict[str, object]:
        del account
        del deck_id
        self.modify_calls.append(cards)
        return {"successful_count": len(cards), "failed_count": 0}

    async def create_deck(
        self,
        account: AuthenticatedAccount,
        deck: PersonalDeckCreateInput,
    ) -> tuple[dict[str, Any], PersonalDeckSummary | None]:
        summary = PersonalDeckSummary(
            id=99,
            name=deck.name,
            owner_username=account.username or "private-user",
            owner_id=account.user_id or 1,
        )
        return {"id": 99, "name": deck.name}, summary

    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        del page_size
        if isinstance(account, AuthenticatedAccount):
            resolved = account.model_copy(
                update={
                    "username": account.username or "private-user",
                    "user_id": account.user_id or 321,
                }
            )
        else:
            resolved = AuthenticatedAccount(
                token=account.token or "secret",
                username=account.username or "private-user",
                user_id=account.user_id or 321,
            )
        return resolved, []


class FakeAuthLoginClient:
    async def login(self, account: ArchidektAccount) -> AuthenticatedAccount:
        del account
        return AuthenticatedAccount(token="secret", username="tester", user_id=123)

    async def resolve_account(self, account: ArchidektAccount) -> AuthenticatedAccount:
        return await self.login(account)

    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        del page_size
        if isinstance(account, AuthenticatedAccount):
            resolved = account
        else:
            resolved = AuthenticatedAccount(token="secret", username="tester", user_id=123)
        return (
            resolved,
            [
                PersonalDeckSummary(id=7, name="Artifacts", owner_username="tester", owner_id=123),
                PersonalDeckSummary(id=8, name="Graveyard Value", owner_username="tester", owner_id=123),
            ],
        )


class CountingFakeAuthLoginClient(FakeAuthLoginClient):
    def __init__(self) -> None:
        self.list_calls = 0

    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        self.list_calls += 1
        return await super().list_personal_decks(account, page_size)


class TokenScopedDeckListClient:
    def __init__(self) -> None:
        self.list_calls: list[str] = []

    async def resolve_account(self, account: ArchidektAccount) -> AuthenticatedAccount:
        return AuthenticatedAccount(
            token=account.token or "secret",
            username=account.username,
            user_id=account.user_id,
        )

    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        del page_size
        if isinstance(account, AuthenticatedAccount):
            token = account.token
            username = account.username or "tester"
            user_id = account.user_id or 123
        else:
            token = account.token or "secret"
            username = account.username or "tester"
            user_id = account.user_id or 123
        self.list_calls.append(token)
        deck_id = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:6], 16)
        return (
            AuthenticatedAccount(token=token, username=username, user_id=user_id),
            [
                PersonalDeckSummary(
                    id=deck_id,
                    name=f"Deck for {token}",
                    owner_username=username,
                    owner_id=user_id,
                )
            ],
        )


class TokenScopedDeckMutationClient(TokenScopedDeckListClient):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls: list[str] = []
        self.deck_card_calls: list[tuple[str, int]] = []

    async def create_deck(
        self,
        account: AuthenticatedAccount,
        deck: PersonalDeckCreateInput,
    ) -> tuple[dict[str, Any], PersonalDeckSummary | None]:
        self.create_calls.append(account.token)
        summary = PersonalDeckSummary(
            id=99,
            name=deck.name,
            owner_username=account.username or "tester",
            owner_id=account.user_id or 123,
        )
        return {"id": 99, "name": deck.name}, summary

    async def fetch_deck_cards(
        self,
        account: AuthenticatedAccount,
        deck_id: int,
        include_deleted: bool = False,
    ) -> dict[str, object]:
        del include_deleted
        self.deck_card_calls.append((account.token, deck_id))
        return {"deckId": deck_id, "cards": []}


class VerifiedTokenAuthClient(FakeAuthMutationClient):
    def __init__(self, identities_by_token: dict[str, AuthenticatedAccount]) -> None:
        super().__init__()
        self.identities_by_token = identities_by_token

    async def resolve_account(self, account: ArchidektAccount) -> AuthenticatedAccount:
        token = account.token or ""
        resolved = self.identities_by_token[token]
        return AuthenticatedAccount(
            token=token,
            username=resolved.username,
            user_id=resolved.user_id,
        )


class FakeCatalogLookupClient:
    def __init__(self, references_by_name: dict[str, list[ArchidektCardReference]]) -> None:
        self.references_by_name = {
            key.casefold(): value for key, value in references_by_name.items()
        }
        self.calls: list[ArchidektCardSearchFilters] = []

    async def search_cards(
        self,
        filters: ArchidektCardSearchFilters,
    ) -> tuple[list[ArchidektCardReference], int | None, bool | None]:
        self.calls.append(filters)
        results: list[ArchidektCardReference] = []
        for exact_name in filters.exact_name:
            matches = self.references_by_name.get(exact_name.casefold(), [])
            for match in matches:
                results.append(match.model_copy(update={"requested_exact_name": exact_name}))
        return results, len(results), False


class FakeScryfallClient:
    def __init__(self, raw_cards: list[dict[str, object]]) -> None:
        self.raw_cards = raw_cards

    async def search_unowned_cards(
        self,
        filters: CardSearchFilters,
        owned_oracle_ids: set[str],
        owned_names: set[str],
    ) -> tuple[list[dict[str, object]], str, bool | None, list[str]]:
        del filters
        del owned_oracle_ids
        del owned_names
        return self.raw_cards, "name:\"test\"", False, ["Scryfall stub used in tests."]


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class RecordingDeckListHttpClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        del kwargs
        url_text = str(url)
        self.urls.append(url_text)
        if "/api/decks/curated/self/" in url_text:
            payload: dict[str, object] | list[object] = {"results": []}
        else:
            payload = {
                "results": [
                    {
                        "id": 17,
                        "name": "Known Identity Deck",
                        "owner": {"id": 123, "username": "tester"},
                    }
                ],
                "next": None,
        }
        return httpx.Response(200, json=payload, request=httpx.Request(method, url_text))


class IdentityResolvingHttpClient:
    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        del kwargs
        payload = {
            "results": [
                {
                    "id": 17,
                    "name": "Verified Identity Deck",
                    "owner": {"id": 123, "username": "verified-user"},
                }
            ]
        }
        return httpx.Response(200, json=payload, request=httpx.Request(method, str(url)))


class FakeStatusHttpResponse(FakeHttpResponse):
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        super().__init__(payload)
        self.status_code = status_code


class FakeDeckMutationHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        content: str | None = None,
        json: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
        **kwargs: object,
    ) -> FakeStatusHttpResponse:
        if method == "PATCH":
            return await self.patch(url, json=json, headers=headers)
        return FakeStatusHttpResponse(200, {})

    async def patch(
        self,
        url: str,
        json: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeStatusHttpResponse:
        payload = dict(json or {})
        self.calls.append(
            {
                "url": url,
                "json": payload,
                "headers": dict(headers or {}),
            }
        )
        cards = payload.get("cards") or []
        if len(cards) > 1:
            return FakeStatusHttpResponse(400, {"error": "batch failed"})

        card = cards[0]
        if card.get("patchId") == "ok-card":
            return FakeStatusHttpResponse(201, {"add": [{"deckRelationId": 99, "cardId": card.get("cardid")}]})
        return FakeStatusHttpResponse(400, {"error": "bad card"})


class FakeCollectionDeleteHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        content: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeStatusHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "content": content,
                "headers": dict(headers or {}),
            }
        )
        return FakeStatusHttpResponse(200, {"deleted_ids": [9001, 9002]})


class FakeCardCatalogHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        content: str | None = None,
        json: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
        **kwargs: object,
    ) -> FakeHttpResponse:
        if method == "GET":
            return await self.get(url, params=params, headers=headers)
        return FakeHttpResponse({"count": 0, "next": None, "results": []})

    async def get(
        self,
        url: str,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeHttpResponse:
        recorded_params = dict(params or {})
        self.calls.append(
            {
                "url": url,
                "params": recorded_params,
                "headers": dict(headers or {}),
            }
        )
        exact_name = str(recorded_params.get("name") or "")
        payloads = {
            "Sol Ring": {
                "count": 1,
                "next": None,
                "results": [
                    {
                        "id": 150824,
                        "uid": "sol-ring-printing",
                        "displayName": "Sol Ring",
                        "rarity": "uncommon",
                        "releasedAt": "2024-01-01T00:00:00Z",
                        "prices": {"tcg": 1.25},
                        "owned": 3,
                        "oracleCard": {
                            "id": 15342,
                            "uid": "sol-ring-oracle",
                            "name": "Sol Ring",
                            "manaCost": "{1}",
                            "cmc": 1,
                            "text": "{T}: Add {C}{C}.",
                            "colors": [],
                            "colorIdentity": [],
                            "superTypes": [],
                            "types": ["Artifact"],
                            "subTypes": [],
                            "defaultCategory": "Ramp",
                        },
                        "edition": {
                            "editioncode": "clb",
                            "editionname": "Commander Legends: Battle for Baldur's Gate",
                        },
                    }
                ],
            },
            "Arcane Signet": {
                "count": 1,
                "next": None,
                "results": [
                    {
                        "id": 150825,
                        "uid": "arcane-signet-printing",
                        "displayName": "Arcane Signet",
                        "rarity": "common",
                        "releasedAt": "2024-01-01T00:00:00Z",
                        "prices": {"tcg": 0.75},
                        "owned": 4,
                        "oracleCard": {
                            "id": 15343,
                            "uid": "arcane-signet-oracle",
                            "name": "Arcane Signet",
                            "manaCost": "{2}",
                            "cmc": 2,
                            "text": "{T}: Add one mana of any color in your commander's color identity.",
                            "colors": [],
                            "colorIdentity": [],
                            "superTypes": [],
                            "types": ["Artifact"],
                            "subTypes": [],
                            "defaultCategory": "Ramp",
                        },
                        "edition": {
                            "editioncode": "cmm",
                            "editionname": "Commander Masters",
                        },
                    }
                ],
            },
            "Lightning Bolt": {
                "count": 1,
                "next": None,
                "results": [
                    {
                        "id": 123,
                        "uid": "lightning-bolt-printing",
                        "displayName": "Lightning Bolt",
                        "rarity": "common",
                        "releasedAt": "2024-01-01T00:00:00Z",
                        "prices": {"tcg": 2.50},
                        "owned": 0,
                        "oracleCard": {
                            "id": 99999,
                            "uid": "lightning-bolt-oracle",
                            "name": "Lightning Bolt",
                            "manaCost": "{R}",
                            "cmc": 1,
                            "text": "Lightning Bolt deals 3 damage to any target.",
                            "colors": ["R"],
                            "colorIdentity": ["R"],
                            "superTypes": [],
                            "types": ["Instant"],
                            "subTypes": [],
                            "defaultCategory": "Burn",
                        },
                        "edition": {
                            "editioncode": "m10",
                            "editionname": "Magic 2010",
                        },
                    }
                ],
            },
        }
        return FakeHttpResponse(payloads.get(exact_name, {"count": 0, "next": None, "results": []}))


class FakeRedis:
    def __init__(self) -> None:
        self.storage: dict[str, tuple[object, datetime | None]] = {}

    def _live_entry(self, key: str) -> tuple[object, datetime | None] | None:
        entry = self.storage.get(key)
        if entry is None:
            return None

        payload, expires_at = entry
        if expires_at is not None and datetime.now(UTC) >= expires_at:
            self.storage.pop(key, None)
            return None
        return payload, expires_at

    async def get(self, key: str) -> str | None:
        entry = self._live_entry(key)
        if entry is None:
            return None

        payload, _ = entry
        if not isinstance(payload, str):
            return None
        return payload

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        expires_at = datetime.now(UTC) + timedelta(seconds=ex) if ex else None
        self.storage[key] = (value, expires_at)
        return True

    async def ttl(self, key: str) -> int:
        entry = self._live_entry(key)
        if entry is None:
            return -2

        _, expires_at = entry
        if expires_at is None:
            return -1

        remaining = int((expires_at - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            self.storage.pop(key, None)
            return -2
        return remaining

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.storage:
                deleted += 1
            self.storage.pop(key, None)
        return deleted

    async def sadd(self, key: str, *values: str) -> int:
        entry = self._live_entry(key)
        expires_at = entry[1] if entry is not None else None
        existing = entry[0] if entry is not None else set()
        members = set(existing) if isinstance(existing, set) else set()
        added = 0
        for value in values:
            if value not in members:
                added += 1
                members.add(value)
        self.storage[key] = (members, expires_at)
        return added

    async def smembers(self, key: str) -> set[str]:
        entry = self._live_entry(key)
        if entry is None:
            return set()
        payload, _ = entry
        if not isinstance(payload, set):
            return set()
        return {str(member) for member in payload}

    async def expire(self, key: str, seconds: int) -> bool:
        entry = self._live_entry(key)
        if entry is None:
            return False
        payload, _ = entry
        self.storage[key] = (payload, datetime.now(UTC) + timedelta(seconds=seconds))
        return True

    async def execute_command(self, command: str, *args: object) -> object:
        normalized = command.upper()
        if normalized == "SADD":
            key = str(args[0])
            values = tuple(str(value) for value in args[1:])
            return await self.sadd(key, *values)
        if normalized == "SMEMBERS":
            return await self.smembers(str(args[0]))
        if normalized == "EXPIRE":
            return await self.expire(str(args[0]), int(args[1]))
        raise NotImplementedError(f"FakeRedis does not implement {command}")

    async def aclose(self) -> None:
        return None


class TtlTrackingRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.ttl_calls: list[str] = []

    async def ttl(self, key: str) -> int:
        self.ttl_calls.append(key)
        return await super().ttl(key)


class FailingDeckListRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.fail_get = False
        self.fail_execute = False
        self.fail_delete = False

    async def get(self, key: str) -> str | None:
        if self.fail_get:
            raise RedisError("simulated redis read failure")
        return await super().get(key)

    async def execute_command(self, command: str, *args: object) -> object:
        if self.fail_execute:
            raise RedisError("simulated redis command failure")
        return await super().execute_command(command, *args)

    async def delete(self, *keys: str) -> int:
        if self.fail_delete:
            raise RedisError("simulated redis delete failure")
        return await super().delete(*keys)
