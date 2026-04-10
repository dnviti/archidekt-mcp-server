from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from pydantic import ValidationError
from starlette.testclient import TestClient

from archidekt_commander_mcp.clients import CollectionCache
from archidekt_commander_mcp.config import RuntimeSettings
from archidekt_commander_mcp.models import CollectionCardRecord, CollectionLocator, CollectionSnapshot
from archidekt_commander_mcp.server import create_server


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


class HttpRouteTests(unittest.TestCase):
    def test_health_route(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_overview_api_rejects_missing_collection(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.post("/api/overview", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())


class FakeCollectionClient:
    def __init__(self, snapshot: CollectionSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def fetch_snapshot(self, collection: CollectionLocator) -> CollectionSnapshot:
        del collection
        self.calls += 1
        return self.snapshot


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


if __name__ == "__main__":
    unittest.main()
