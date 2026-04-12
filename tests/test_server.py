from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from starlette.testclient import TestClient

from archidekt_commander_mcp.clients import CollectionCache
from archidekt_commander_mcp.config import RuntimeSettings
from archidekt_commander_mcp.models import (
    ArchidektAccount,
    AuthenticatedAccount,
    CardResult,
    CollectionCardUpsert,
    CollectionCardRecord,
    CollectionLocator,
    CollectionSnapshot,
    PersonalDeckCardUsage,
)
from archidekt_commander_mcp.server import DeckbuildingService, PersonalDeckUsageSnapshot, create_server


class CollectionLocatorTests(unittest.TestCase):
    def test_accepts_username_and_builds_cache_key(self) -> None:
        locator = CollectionLocator(username="ExampleUser", game=2)
        self.assertEqual(locator.cache_key, "user:exampleuser:game:2")
        self.assertEqual(locator.display_locator, "username=ExampleUser")

    def test_extracts_collection_id_from_url(self) -> None:
        locator = CollectionLocator(collection_url="https://archidekt.com/collection/v2/548188")
        self.assertEqual(locator.static_collection_id, 548188)
        self.assertEqual(locator.cache_key, "id:548188:game:1")

    def test_requires_at_least_one_locator(self) -> None:
        with self.assertRaises(ValidationError):
            CollectionLocator()


class ArchidektAccountTests(unittest.TestCase):
    def test_accepts_token_only(self) -> None:
        account = ArchidektAccount(token="secret-token")
        self.assertEqual(account.display_identity, "token-provided")

    def test_accepts_username_and_password(self) -> None:
        account = ArchidektAccount(username="ExampleUser", password="hunter2")
        self.assertEqual(account.display_identity, "username=ExampleUser")

    def test_rejects_missing_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            ArchidektAccount(username="ExampleUser")


class HttpRouteTests(unittest.TestCase):
    def test_health_route(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_login_api_rejects_missing_account(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.post("/api/login", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())

    def test_overview_api_rejects_missing_collection(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.post("/api/overview", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())


class RuntimeSettingsEnvTests(unittest.TestCase):
    def test_reads_runtime_settings_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ARCHIDEKT_MCP_HOST": "127.0.0.1",
                "ARCHIDEKT_MCP_PORT": "9000",
                "ARCHIDEKT_MCP_REDIS_URL": "redis://redis:6379/5",
                "ARCHIDEKT_MCP_CACHE_TTL_SECONDS": "1234",
                "ARCHIDEKT_MCP_PERSONAL_DECK_CACHE_TTL_SECONDS": "222",
            },
            clear=False,
        ):
            settings = RuntimeSettings()

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.redis_url, "redis://redis:6379/5")
        self.assertEqual(settings.cache_ttl_seconds, 1234)
        self.assertEqual(settings.personal_deck_cache_ttl_seconds, 222)


class FakeCollectionClient:
    def __init__(self, snapshot: CollectionSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def fetch_snapshot(
        self,
        collection: CollectionLocator,
        auth_token: str | None = None,
    ) -> CollectionSnapshot:
        del collection
        del auth_token
        self.calls += 1
        return self.snapshot


class FakeAuthMutationClient:
    def __init__(self) -> None:
        self.upsert_calls: list[CollectionCardUpsert] = []

    async def upsert_collection_entry(
        self,
        account: AuthenticatedAccount,
        entry: CollectionCardUpsert,
    ) -> dict[str, object]:
        del account
        self.upsert_calls.append(entry)
        return {"id": entry.record_id or 9001, "card": entry.card_id, "quantity": entry.quantity}

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


class FakeRedis:
    def __init__(self) -> None:
        self.storage: dict[str, tuple[str, datetime | None]] = {}

    async def get(self, key: str) -> str | None:
        entry = self.storage.get(key)
        if entry is None:
            return None

        payload, expires_at = entry
        if expires_at is not None and datetime.now(UTC) >= expires_at:
            self.storage.pop(key, None)
            return None
        return payload

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        expires_at = datetime.now(UTC) + timedelta(seconds=ex) if ex else None
        self.storage[key] = (value, expires_at)
        return True

    async def ttl(self, key: str) -> int:
        entry = self.storage.get(key)
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

    async def delete(self, key: str) -> int:
        existed = key in self.storage
        self.storage.pop(key, None)
        return 1 if existed else 0


class CollectionCacheRedisTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_snapshot_from_redis_without_refetching(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=123,
            owner_id=456,
            owner_username="tester",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=1,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/123",
            records=[
                CollectionCardRecord(
                    record_id=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    quantity=2,
                    foil=False,
                    modifier=None,
                    tags=("ramp",),
                    condition_code=None,
                    language_code=None,
                    name="Sol Ring",
                    display_name=None,
                    oracle_text="Add two colorless mana.",
                    mana_cost="{1}",
                    cmc=1.0,
                    colors=(),
                    color_identity=(),
                    supertypes=(),
                    types=("Artifact",),
                    subtypes=(),
                    type_line="Artifact",
                    keywords=(),
                    rarity="uncommon",
                    set_code="lea",
                    set_name="Limited Edition Alpha",
                    commander_legal=True,
                    oracle_id="sol-ring-oracle",
                    card_id=150824,
                    printing_id="sol-ring-printing",
                    edhrec_rank=1,
                    image_uri=None,
                    prices={"tcg": 1.5},
                )
            ],
        )
        collection = CollectionLocator(username="tester")
        redis_client = FakeRedis()

        first_client = FakeCollectionClient(snapshot)
        first_cache = CollectionCache(first_client, redis_client, ttl_seconds=86400)
        first_snapshot = await first_cache.get_snapshot(collection)
        self.assertEqual(first_client.calls, 1)
        self.assertEqual(first_snapshot.collection_id, 123)

        second_client = FakeCollectionClient(snapshot)
        second_cache = CollectionCache(second_client, redis_client, ttl_seconds=86400)
        second_snapshot = await second_cache.get_snapshot(collection)

        self.assertEqual(second_client.calls, 0)
        self.assertEqual(second_snapshot.collection_id, 123)
        self.assertEqual(second_snapshot.records[0].name, "Sol Ring")
        self.assertTrue(redis_client.storage)

    async def test_private_snapshot_reuses_redis_cache_across_services(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=654,
            owner_username="private-user",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=1,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/321",
            records=[],
        )
        redis_client = FakeRedis()
        account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
        collection = CollectionLocator(collection_id=321)

        first_service = DeckbuildingService(RuntimeSettings())
        first_original_redis = first_service.redis_client
        first_service.redis_client = redis_client
        first_service.archidekt_client = FakeCollectionClient(snapshot)
        await first_original_redis.aclose()
        try:
            first_result = await first_service.get_snapshot(collection, account=account)
            self.assertEqual(first_result.collection_id, 321)
            self.assertEqual(first_service.archidekt_client.calls, 1)
        finally:
            await first_service.http_client.aclose()

        second_service = DeckbuildingService(RuntimeSettings())
        second_original_redis = second_service.redis_client
        second_service.redis_client = redis_client
        second_service.archidekt_client = FakeCollectionClient(snapshot)
        await second_original_redis.aclose()
        try:
            second_result = await second_service.get_snapshot(collection, account=account)
            self.assertEqual(second_result.collection_id, 321)
            self.assertEqual(second_service.archidekt_client.calls, 0)
        finally:
            await second_service.http_client.aclose()


class AuthenticatedResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_personal_deck_cards_maps_relation_and_card_ids(self) -> None:
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        service.auth_client = FakeAuthMutationClient()
        await original_redis.aclose()
        account = AuthenticatedAccount(token="secret", username="tester", user_id=1)
        try:
            response = await service.get_personal_deck_cards(account, deck_id=55)
            self.assertEqual(response.deck_id, 55)
            self.assertEqual(response.total_cards, 1)
            self.assertEqual(response.cards[0].deck_relation_id, 77)
            self.assertEqual(response.cards[0].archidekt_card_id, 150824)
            self.assertEqual(response.cards[0].name, "Sol Ring")
            self.assertEqual(response.cards[0].type_line, "Artifact")
        finally:
            await service.http_client.aclose()

    async def test_upsert_collection_entries_invalidates_public_and_private_caches(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=321,
            owner_username="private-user",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=1,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/321",
            records=[],
        )
        redis_client = FakeRedis()
        account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
        private_locator = CollectionLocator(collection_id=321)
        public_locator = CollectionLocator(username="private-user")

        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        service.redis_client = redis_client
        service.cache.redis = redis_client
        service.archidekt_client = FakeCollectionClient(snapshot)
        service.cache.client = service.archidekt_client
        service.auth_client = FakeAuthMutationClient()
        await original_redis.aclose()
        try:
            await service.get_snapshot(private_locator, account=account)
            await service.cache.get_snapshot(public_locator)

            private_key = service._private_redis_key(
                "collection",
                service._private_snapshot_cache_key(private_locator, account),
            )
            public_key = service.cache._redis_key(public_locator.cache_key)

            self.assertIn(private_key, redis_client.storage)
            self.assertIn(public_key, redis_client.storage)

            response = await service.upsert_collection_entries(
                account,
                [CollectionCardUpsert(card_id=150824, quantity=1, game=1)],
            )

            self.assertEqual(response.affected_count, 1)
            self.assertNotIn(private_key, redis_client.storage)
            self.assertNotIn(public_key, redis_client.storage)
        finally:
            await service.http_client.aclose()


class PersonalDeckUsageAnnotationTests(unittest.TestCase):
    def test_applies_personal_deck_usage_by_oracle_id(self) -> None:
        service = DeckbuildingService(RuntimeSettings())
        try:
            result = CardResult(
                source="collection",
                ownership_scope="owned",
                name="Sol Ring",
                oracle_id="sol-ring-oracle",
            )
            snapshot = PersonalDeckUsageSnapshot(
                account=AuthenticatedAccount(token="secret", username="tester", user_id=1),
                decks=[],
                usage_by_oracle_id={
                    "sol-ring-oracle": [
                        PersonalDeckCardUsage(
                            deck_id=7,
                            deck_name="Artifacts",
                            quantity=1,
                            categories=["Ramp"],
                        )
                    ]
                },
                usage_by_name={},
                fetched_at=datetime.now(UTC),
            )

            service._apply_personal_deck_usage([result], snapshot)

            self.assertEqual(result.personal_deck_count, 1)
            self.assertEqual(result.personal_deck_total_quantity, 1)
            self.assertEqual(result.personal_deck_usage[0].deck_name, "Artifacts")
        finally:
            asyncio.run(service.aclose())


if __name__ == "__main__":
    unittest.main()
