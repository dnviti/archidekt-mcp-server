from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
import hashlib
import os
import tempfile
from typing import Any
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import ValidationError
import httpx
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from archidekt_commander_mcp.clients import ArchidektAuthenticatedClient, ArchidektRequestGate, CollectionCache, ScryfallClient
from archidekt_commander_mcp.integrations.public_collection import ArchidektPublicCollectionClient
from archidekt_commander_mcp.config import RuntimeSettings
from archidekt_commander_mcp.mcp_auth import (
    AUTH_SCOPE,
    ArchidektAccessToken,
    RedisArchidektOAuthProvider,
)
from archidekt_commander_mcp.models import (
    ArchidektAccount,
    ArchidektCardReference,
    ArchidektCardSearchFilters,
    AuthenticatedAccount,
    CardResult,
    CardSearchFilters,
    CollectionAvailabilityCardRequest,
    CollectionAvailabilityOptions,
    CollectionCardDelete,
    CollectionReadOptions,
    CollectionSearchRequest,
    CollectionCardUpsert,
    CollectionCardRecord,
    CollectionLocator,
    CollectionSnapshot,
    PersonalDeckCardModifications,
    PersonalDeckCardMutation,
    PersonalDeckCardUsage,
    PersonalDeckCreateInput,
    PersonalDeckSummary,
)
from archidekt_commander_mcp.server import (
    DeckbuildingService,
    PersonalDeckUsageSnapshot,
    build_arg_parser,
    build_runtime_settings_from_args,
    create_server,
)
from archidekt_commander_mcp.server_contracts import SERVER_INSTRUCTIONS
from archidekt_commander_mcp.webui import render_home_page

from tests.support import (
    CountingFakeAuthLoginClient,
    FailingDeckListRedis,
    FakeAuthLoginClient,
    FakeAuthMutationClient,
    FakeCardCatalogHttpClient,
    FakeCatalogLookupClient,
    FakeCollectionClient,
    FakeCollectionDeleteHttpClient,
    FakeDeckMutationHttpClient,
    FakeRedis,
    FakeScryfallClient,
    IdentityResolvingHttpClient,
    RecordingDeckListHttpClient,
    TokenScopedDeckListClient,
    TokenScopedDeckMutationClient,
    TtlTrackingRedis,
    VerifiedTokenAuthClient,
)


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
    def test_route_inventory_matches_frozen_contract(self) -> None:
        server = create_server(RuntimeSettings())
        app = server.streamable_http_app()
        route_paths = {getattr(route, "path", "") for route in app.routes}

        self.assertEqual(
            route_paths,
            {
                "/",
                "/favicon.ico",
                "/assets/{asset_name}",
                "/health",
                "/api/login",
                "/api/personal-decks",
                "/api/cards/search",
                "/api/personal-deck-cards",
                "/api/personal-decks/create",
                "/api/personal-decks/update",
                "/api/personal-decks/delete",
                "/api/personal-decks/modify-cards",
                "/api/collection/upsert",
                "/api/collection/delete",
                "/api/overview",
                "/api/search-owned",
                "/api/search-unowned",
                "/mcp",
            },
        )

    def test_auth_enabled_route_inventory_includes_oauth_routes(self) -> None:
        with patch("archidekt_commander_mcp.app_factory.redis_async.from_url", return_value=FakeRedis()):
            server = create_server(
                RuntimeSettings(
                    auth_enabled=True,
                    public_base_url="https://testserver",
                    redis_url="redis://fake/0",
                )
            )
        app = server.streamable_http_app()
        route_paths = {getattr(route, "path", "") for route in app.routes}

        self.assertIn("/auth/archidekt-login", route_paths)
        self.assertIn("/register", route_paths)
        self.assertIn("/authorize", route_paths)
        self.assertIn("/token", route_paths)
        self.assertIn("/revoke", route_paths)
        self.assertIn("/.well-known/oauth-authorization-server", route_paths)

    def test_streamable_http_path_override_changes_mcp_route(self) -> None:
        server = create_server(RuntimeSettings(streamable_http_path="/custom-mcp"))
        app = server.streamable_http_app()
        route_paths = {getattr(route, "path", "") for route in app.routes}

        self.assertIn("/custom-mcp", route_paths)
        self.assertNotIn("/mcp", route_paths)

    def test_health_route(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_static_webui_assets_are_served(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())

        favicon = client.get("/favicon.ico")
        logo = client.get("/assets/logo-generated.png")
        missing = client.get("/assets/not-found.png")

        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(favicon.headers["content-type"], "image/x-icon")
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.headers["content-type"], "image/png")
        self.assertEqual(missing.status_code, 404)

    def test_trusted_proxy_headers_update_request_client_host(self) -> None:
        async def client_ip(request: Any) -> JSONResponse:
            return JSONResponse({"client_host": request.client.host if request.client else None})

        server = create_server(RuntimeSettings(forwarded_allow_ips="10.0.0.2"))
        app = server.streamable_http_app()
        app.add_route("/debug-client-ip", client_ip, methods=["GET"])
        client = TestClient(app, client=("10.0.0.2", 50000))

        response = client.get(
            "/debug-client-ip",
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_host"], "203.0.113.9")

    def test_untrusted_proxy_headers_do_not_update_request_client_host(self) -> None:
        async def client_ip(request: Any) -> JSONResponse:
            return JSONResponse({"client_host": request.client.host if request.client else None})

        server = create_server(RuntimeSettings(forwarded_allow_ips="127.0.0.1"))
        app = server.streamable_http_app()
        app.add_route("/debug-client-ip", client_ip, methods=["GET"])
        client = TestClient(app, client=("10.0.0.2", 50000))

        response = client.get(
            "/debug-client-ip",
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_host"], "10.0.0.2")

    def test_trusted_real_ip_header_updates_request_client_host(self) -> None:
        async def client_ip(request: Any) -> JSONResponse:
            return JSONResponse({"client_host": request.client.host if request.client else None})

        server = create_server(RuntimeSettings(forwarded_allow_ips="10.0.0.2"))
        app = server.streamable_http_app()
        app.add_route("/debug-client-ip", client_ip, methods=["GET"])
        client = TestClient(app, client=("10.0.0.2", 50000))

        response = client.get(
            "/debug-client-ip",
            headers={"X-Real-IP": "203.0.113.42"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_host"], "203.0.113.42")

    def test_untrusted_real_ip_header_does_not_update_request_client_host(self) -> None:
        async def client_ip(request: Any) -> JSONResponse:
            return JSONResponse({"client_host": request.client.host if request.client else None})

        server = create_server(RuntimeSettings(forwarded_allow_ips="127.0.0.1"))
        app = server.streamable_http_app()
        app.add_route("/debug-client-ip", client_ip, methods=["GET"])
        client = TestClient(app, client=("10.0.0.2", 50000))

        response = client.get(
            "/debug-client-ip",
            headers={"X-Real-IP": "203.0.113.42"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_host"], "10.0.0.2")

    def test_post_only_api_routes_reject_get_with_405(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.get("/api/login")
        self.assertEqual(response.status_code, 405)

    def test_login_api_rejects_invalid_json_with_400(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.post(
            "/api/login",
            content="{invalid-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid JSON body.")

    def test_login_api_rejects_missing_account(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.post("/api/login", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_overview_api_rejects_missing_collection(self) -> None:
        server = create_server(RuntimeSettings())
        client = TestClient(server.streamable_http_app())
        response = client.post("/api/overview", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())

    def test_collection_search_request_normalizes_legacy_sort_alias(self) -> None:
        request = CollectionSearchRequest.model_validate(
            {
                "collection": {"username": "tester"},
                "filters": {"sort": "price_desc", "limit": 5},
            }
        )
        self.assertEqual(request.filters.sort_by, "unit_price")
        self.assertEqual(request.filters.sort_direction, "desc")

    def test_tool_annotations_distinguish_read_only_and_mutating_tools(self) -> None:
        server = create_server(RuntimeSettings())
        tools = asyncio.run(server.list_tools())
        tools_by_name = {tool.name: tool for tool in tools}

        self.assertTrue(tools_by_name["search_owned_cards"].annotations.readOnlyHint)
        self.assertTrue(tools_by_name["search_unowned_cards"].annotations.readOnlyHint)
        self.assertTrue(tools_by_name["get_personal_deck_cards"].annotations.readOnlyHint)
        self.assertTrue(tools_by_name["list_personal_decks"].annotations.readOnlyHint)
        self.assertTrue(tools_by_name["check_collection_card_availability"].annotations.readOnlyHint)
        self.assertFalse(tools_by_name["login_archidekt"].annotations.readOnlyHint)
        self.assertFalse(tools_by_name["read_collection"].annotations.destructiveHint)
        self.assertFalse(tools_by_name["create_personal_deck"].annotations.destructiveHint)
        self.assertTrue(tools_by_name["modify_personal_deck_cards"].annotations.destructiveHint)
        self.assertTrue(tools_by_name["delete_personal_deck"].annotations.destructiveHint)
        self.assertTrue(tools_by_name["delete_collection_entries"].annotations.destructiveHint)
        self.assertFalse(tools_by_name["refresh_collection_cache"].annotations.readOnlyHint)

    def test_tool_roster_exactly_matches_contract(self) -> None:
        server = create_server(RuntimeSettings())
        tools = asyncio.run(server.list_tools())
        tool_names = [tool.name for tool in tools]

        self.assertEqual(
            tool_names,
            [
                "login_archidekt",
                "list_personal_decks",
                "search_archidekt_cards",
                "get_personal_deck_cards",
                "create_personal_deck",
                "update_personal_deck",
                "delete_personal_deck",
                "modify_personal_deck_cards",
                "upsert_collection_entries",
                "delete_collection_entries",
                "get_collection_overview",
                "read_collection",
                "check_collection_card_availability",
                "refresh_collection_cache",
                "search_owned_cards",
                "search_unowned_cards",
            ],
        )


class ApiErrorMappingTests(unittest.TestCase):
    def test_http_status_error_maps_to_502(self) -> None:
        async def fake_get_collection_overview(
            self,
            collection: CollectionLocator,
            account: ArchidektAccount | AuthenticatedAccount | None = None,
        ) -> object:
            del self
            del collection
            del account
            request = httpx.Request("GET", "https://archidekt.com/api/collection/v2/")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("upstream unavailable", request=request, response=response)

        with patch(
            "archidekt_commander_mcp.server.DeckbuildingService.get_collection_overview",
            new=fake_get_collection_overview,
        ):
            server = create_server(RuntimeSettings())
            client = TestClient(server.streamable_http_app(), raise_server_exceptions=False)
            response = client.post("/api/overview", json={"collection": {"username": "tester"}})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Remote HTTP error from Archidekt or Scryfall.")

    def test_upstream_auth_error_maps_to_reconnect_required(self) -> None:
        async def fake_get_collection_overview(
            self,
            collection: CollectionLocator,
            account: ArchidektAccount | AuthenticatedAccount | None = None,
        ) -> object:
            del self
            del collection
            del account
            request = httpx.Request("GET", "https://archidekt.com/api/decks/v3/")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("archidekt token expired", request=request, response=response)

        with patch(
            "archidekt_commander_mcp.server.DeckbuildingService.get_collection_overview",
            new=fake_get_collection_overview,
        ):
            server = create_server(RuntimeSettings())
            client = TestClient(server.streamable_http_app(), raise_server_exceptions=False)
            response = client.post("/api/overview", json={"collection": {"username": "tester"}})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"],
            "Archidekt authentication needs attention. Reconnect Archidekt and try again.",
        )

    def test_value_error_maps_to_400(self) -> None:
        async def fake_get_collection_overview(
            self,
            collection: CollectionLocator,
            account: ArchidektAccount | AuthenticatedAccount | None = None,
        ) -> object:
            del self
            del collection
            del account
            raise ValueError("collection locator rejected")

        with patch(
            "archidekt_commander_mcp.server.DeckbuildingService.get_collection_overview",
            new=fake_get_collection_overview,
        ):
            server = create_server(RuntimeSettings())
            client = TestClient(server.streamable_http_app())
            response = client.post("/api/overview", json={"collection": {"username": "tester"}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "collection locator rejected")

    def test_unhandled_error_maps_to_500(self) -> None:
        async def fake_get_collection_overview(
            self,
            collection: CollectionLocator,
            account: ArchidektAccount | AuthenticatedAccount | None = None,
        ) -> object:
            del self
            del collection
            del account
            raise Exception("unexpected boom")

        with patch(
            "archidekt_commander_mcp.server.DeckbuildingService.get_collection_overview",
            new=fake_get_collection_overview,
        ):
            server = create_server(RuntimeSettings())
            client = TestClient(server.streamable_http_app(), raise_server_exceptions=False)
            response = client.post("/api/overview", json={"collection": {"username": "tester"}})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"], "Internal server error.")
        self.assertEqual(response.json()["details"], "unexpected boom")


class RuntimeCliContractTests(unittest.TestCase):
    def test_parser_exposes_frozen_cli_flags(self) -> None:
        parser = build_arg_parser()
        option_strings = {option for action in parser._actions for option in action.option_strings}

        self.assertTrue(
            {
                "--transport",
                "--host",
                "--port",
                "--log-level",
                "--cache-ttl-seconds",
                "--personal-deck-cache-ttl-seconds",
                "--redis-url",
                "--redis-key-prefix",
                "--http-timeout-seconds",
                "--max-search-results",
                "--scryfall-max-pages",
                "--user-agent",
                "--streamable-http-path",
                "--forwarded-allow-ips",
            }.issubset(option_strings)
        )

    def test_runtime_settings_builder_maps_cli_args(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                "9001",
                "--log-level",
                "debug",
                "--cache-ttl-seconds",
                "1234",
                "--personal-deck-cache-ttl-seconds",
                "321",
                "--redis-url",
                "redis://localhost:6379/9",
                "--redis-key-prefix",
                "test-prefix",
                "--http-timeout-seconds",
                "45",
                "--max-search-results",
                "33",
                "--scryfall-max-pages",
                "8",
                "--user-agent",
                "archidekt-mcp-test/1.0",
                "--streamable-http-path",
                "/custom-mcp",
                "--forwarded-allow-ips",
                "10.0.0.0/8,172.16.0.0/12",
            ]
        )

        settings = build_runtime_settings_from_args(args)

        self.assertEqual(settings.transport, "streamable-http")
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 9001)
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.cache_ttl_seconds, 1234)
        self.assertEqual(settings.personal_deck_cache_ttl_seconds, 321)
        self.assertEqual(settings.redis_url, "redis://localhost:6379/9")
        self.assertEqual(settings.redis_key_prefix, "test-prefix")
        self.assertEqual(settings.http_timeout_seconds, 45.0)
        self.assertEqual(settings.max_search_results, 33)
        self.assertEqual(settings.scryfall_max_pages, 8)
        self.assertEqual(settings.user_agent, "archidekt-mcp-test/1.0")
        self.assertEqual(settings.streamable_http_path, "/custom-mcp")
        self.assertEqual(settings.forwarded_allow_ips, "10.0.0.0/8,172.16.0.0/12")


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
                "ARCHIDEKT_MCP_FORWARDED_ALLOW_IPS": "10.0.0.0/8,172.16.0.0/12",
            },
            clear=False,
        ):
            settings = RuntimeSettings()

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.redis_url, "redis://redis:6379/5")
        self.assertEqual(settings.cache_ttl_seconds, 1234)
        self.assertEqual(settings.personal_deck_cache_ttl_seconds, 222)
        self.assertEqual(settings.forwarded_allow_ips, "10.0.0.0/8,172.16.0.0/12")

    def test_accepts_non_expiring_auth_ttls_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ARCHIDEKT_MCP_AUTH_ACCESS_TOKEN_TTL_SECONDS": "infinite",
                "ARCHIDEKT_MCP_AUTH_REFRESH_TOKEN_TTL_SECONDS": "0",
            },
            clear=False,
        ):
            settings = RuntimeSettings()

        self.assertIsNone(settings.auth_access_token_ttl_seconds)
        self.assertIsNone(settings.auth_refresh_token_ttl_seconds)


class WebUiTests(unittest.TestCase):
    def test_oauth_enabled_page_guides_nontechnical_deckbuilding_chatbot_flow(self) -> None:
        html = render_home_page(RuntimeSettings(auth_enabled=True))
        self.assertNotIn("Account JSON", html)
        self.assertNotIn("Test Auth Login", html)
        self.assertNotIn("Filters JSON", html)
        self.assertNotIn("API Test Result", html)
        self.assertNotIn("window.localStorage", html)

        self.assertIn("/favicon.ico", html)
        self.assertIn("/assets/logo-generated.png", html)
        self.assertIn("Your Archidekt Collection", html)
        self.assertIn("What Do You Want To Build?", html)
        self.assertIn("Connect Your Chatbot", html)
        self.assertIn("ChatGPT", html)
        self.assertIn("Claude", html)
        self.assertIn("Copy This Request", html)
        self.assertIn('const authEnabled = true;', html)
        self.assertIn("new URL(mcpPath, window.location.origin)", html)
        self.assertIn("function buildDeckBrief()", html)
        self.assertIn("Private Archidekt sign-in is available when your chatbot connects", html)
        self.assertIn("For Commander, use more than 1 copy only for basic lands.", html)
        self.assertIn("wait for my explicit confirmation", html)

    def test_server_instructions_explain_quantity_rules(self) -> None:
        self.assertIn("Collection quantities may be any positive integer.", SERVER_INSTRUCTIONS)
        self.assertIn("quantity lives inside `modifications.quantity`", SERVER_INSTRUCTIONS)
        self.assertIn("provide `record_id` when updating an existing row", SERVER_INSTRUCTIONS)
        self.assertIn("Use `delete_collection_entries`", SERVER_INSTRUCTIONS)
        self.assertIn("For Commander decks, only basic lands should normally exceed 1 copy.", SERVER_INSTRUCTIONS)
        self.assertIn("use at most `4` copies of a non-basic card", SERVER_INSTRUCTIONS)

    def test_quantity_fields_expose_copy_rule_descriptions(self) -> None:
        deck_quantity_description = (
            PersonalDeckCardModifications.model_json_schema()["properties"]["quantity"]["description"]
        )
        collection_quantity_description = (
            CollectionCardUpsert.model_json_schema()["properties"]["quantity"]["description"]
        )
        collection_record_id_description = (
            CollectionCardUpsert.model_json_schema()["properties"]["record_id"]["description"]
        )
        collection_delete_record_id_description = (
            CollectionCardDelete.model_json_schema()["properties"]["record_id"]["description"]
        )

        self.assertIn("Values greater than 1 are allowed.", deck_quantity_description)
        self.assertIn("Commander decks", deck_quantity_description)
        self.assertIn("4 copies or fewer", deck_quantity_description)
        self.assertIn("Any positive integer is allowed.", collection_quantity_description)
        self.assertIn("update an existing row", collection_record_id_description)
        self.assertIn("archidekt_record_ids", collection_delete_record_id_description)


class OAuthProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_oauth_sessions_do_not_expire(self) -> None:
        redis_client = FakeRedis()
        provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await provider.register_client(client)

        redirect_to_form = await provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]
        redirect_back = await provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await provider.load_authorization_code(client, code)

        self.assertIsNotNone(loaded_code)
        assert loaded_code is not None

        token = await provider.exchange_authorization_code(client, loaded_code)
        self.assertIsNone(token.expires_in)
        self.assertIsNotNone(token.refresh_token)

        access = await provider.load_access_token(token.access_token)
        self.assertIsNotNone(access)
        assert access is not None
        self.assertIsNone(access.expires_at)

        refresh = await provider.load_refresh_token(client, token.refresh_token or "")
        self.assertIsNotNone(refresh)
        assert refresh is not None
        self.assertIsNone(refresh.expires_at)

        session = await provider.load_session(access.session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertIsNone(session.access_expires_at)
        self.assertIsNone(session.refresh_expires_at)

        self.assertEqual(await redis_client.ttl(provider._key("access-token", token.access_token)), -1)
        self.assertEqual(await redis_client.ttl(provider._key("refresh-token", token.refresh_token or "")), -1)
        self.assertEqual(await redis_client.ttl(provider._key("session", access.session_id)), -1)

    async def test_oauth_session_stores_login_credentials_and_replaces_archidekt_token(self) -> None:
        redis_client = FakeRedis()
        provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await provider.register_client(client)

        redirect_to_form = await provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]
        redirect_back = await provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
            login_account=ArchidektAccount(username="tester", password="hunter2"),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await provider.load_authorization_code(client, code)
        assert loaded_code is not None

        token = await provider.exchange_authorization_code(client, loaded_code)
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        session = await provider.load_session(access.session_id)
        assert session is not None
        self.assertEqual(session.archidekt_login_identifier, "tester")
        self.assertEqual(session.archidekt_login_identifier_type, "username")
        self.assertEqual(session.archidekt_login_password, "hunter2")

        updated_session = await provider.replace_archidekt_session_token(
            access.session_id,
            archidekt_token="arch-token-renewed",
            archidekt_username="tester-renewed",
            archidekt_user_id=456,
        )
        assert updated_session is not None
        self.assertEqual(updated_session.archidekt_token, "arch-token-renewed")
        self.assertEqual(updated_session.archidekt_login_password, "hunter2")

        updated_access = await provider.load_access_token(token.access_token)
        updated_refresh = await provider.load_refresh_token(client, token.refresh_token or "")
        assert updated_access is not None
        assert updated_refresh is not None
        self.assertEqual(updated_access.archidekt_token, "arch-token-renewed")
        self.assertIsNone(updated_access.archidekt_login_password)
        self.assertEqual(updated_refresh.archidekt_token, "arch-token-renewed")
        self.assertEqual(updated_refresh.archidekt_login_identifier, "tester")

    async def test_default_refresh_does_not_revoke_active_access_token(self) -> None:
        redis_client = FakeRedis()
        provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await provider.register_client(client)

        redirect_to_form = await provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]
        redirect_back = await provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await provider.load_authorization_code(client, code)

        self.assertIsNotNone(loaded_code)
        assert loaded_code is not None

        token = await provider.exchange_authorization_code(client, loaded_code)
        refresh = await provider.load_refresh_token(client, token.refresh_token or "")
        self.assertIsNotNone(refresh)
        assert refresh is not None

        refreshed = await provider.exchange_refresh_token(client, refresh, [AUTH_SCOPE])
        self.assertEqual(refreshed.access_token, token.access_token)
        self.assertEqual(refreshed.refresh_token, token.refresh_token)
        self.assertIsNotNone(await provider.load_access_token(token.access_token))

    async def test_default_provider_migrates_existing_expiring_session_to_non_expiring(self) -> None:
        redis_client = FakeRedis()
        first_provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
            auth_code_ttl_seconds=600,
            access_token_ttl_seconds=3600,
            refresh_token_ttl_seconds=7200,
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await first_provider.register_client(client)

        redirect_to_form = await first_provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]
        redirect_back = await first_provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await first_provider.load_authorization_code(client, code)
        assert loaded_code is not None
        token = await first_provider.exchange_authorization_code(client, loaded_code)

        second_provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
        )
        access = await second_provider.load_access_token(token.access_token)
        self.assertIsNotNone(access)
        assert access is not None
        self.assertIsNone(access.expires_at)

        refresh = await second_provider.load_refresh_token(client, token.refresh_token or "")
        self.assertIsNotNone(refresh)
        assert refresh is not None
        self.assertIsNone(refresh.expires_at)

        session = await second_provider.load_session(access.session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertIsNone(session.access_expires_at)
        self.assertIsNone(session.refresh_expires_at)

        self.assertEqual(await redis_client.ttl(second_provider._key("access-token", token.access_token)), -1)
        self.assertEqual(await redis_client.ttl(second_provider._key("refresh-token", token.refresh_token or "")), -1)
        self.assertEqual(await redis_client.ttl(second_provider._key("session", access.session_id)), -1)

    async def test_authorize_and_exchange_round_trip(self) -> None:
        redis_client = FakeRedis()
        provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
            auth_code_ttl_seconds=600,
            access_token_ttl_seconds=3600,
            refresh_token_ttl_seconds=7200,
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await provider.register_client(client)

        redirect_to_form = await provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]

        redirect_back = await provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await provider.load_authorization_code(client, code)

        self.assertIsNotNone(loaded_code)
        assert loaded_code is not None

        token = await provider.exchange_authorization_code(client, loaded_code)
        self.assertEqual(token.scope, AUTH_SCOPE)
        self.assertIsNotNone(token.refresh_token)

        access = await provider.load_access_token(token.access_token)
        self.assertIsNotNone(access)
        assert access is not None
        self.assertEqual(access.archidekt_username, "tester")
        self.assertEqual(access.archidekt_user_id, 123)

        refresh = await provider.load_refresh_token(client, token.refresh_token or "")
        self.assertIsNotNone(refresh)
        assert refresh is not None

        rotated = await provider.exchange_refresh_token(client, refresh, [AUTH_SCOPE])
        self.assertIsNotNone(await provider.load_access_token(rotated.access_token))

    async def test_session_survives_provider_recreation_via_redis(self) -> None:
        redis_client = FakeRedis()
        first_provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
            auth_code_ttl_seconds=600,
            access_token_ttl_seconds=3600,
            refresh_token_ttl_seconds=7200,
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await first_provider.register_client(client)

        redirect_to_form = await first_provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]
        redirect_back = await first_provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await first_provider.load_authorization_code(client, code)
        assert loaded_code is not None
        token = await first_provider.exchange_authorization_code(client, loaded_code)

        second_provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
            auth_code_ttl_seconds=600,
            access_token_ttl_seconds=3600,
            refresh_token_ttl_seconds=7200,
        )
        access = await second_provider.load_access_token(token.access_token)
        self.assertIsNotNone(access)
        assert access is not None
        refresh = await second_provider.load_refresh_token(client, token.refresh_token or "")
        self.assertIsNotNone(refresh)
        assert refresh is not None

        session = await second_provider.load_session(access.session_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.archidekt_username, "tester")
        self.assertEqual(session.archidekt_user_id, 123)
        self.assertEqual(session.archidekt_token, "arch-token")
        self.assertEqual(session.client_id, "client-1")
        self.assertEqual(session.access_token, token.access_token)
        self.assertEqual(session.refresh_token, token.refresh_token)

    async def test_non_expiring_tokens_persist_without_redis_ttl(self) -> None:
        redis_client = FakeRedis()
        provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="http://127.0.0.1:8000",
            auth_code_ttl_seconds=600,
            access_token_ttl_seconds=None,
            refresh_token_ttl_seconds=None,
        )
        client = OAuthClientInformationFull(
            client_id="client-1",
            client_secret="secret",
            redirect_uris=["https://chat.openai.com/a/oauth/callback"],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=AUTH_SCOPE,
        )
        await provider.register_client(client)

        redirect_to_form = await provider.authorize(
            client,
            AuthorizationParams(
                state="state-123",
                scopes=[AUTH_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chat.openai.com/a/oauth/callback",
                redirect_uri_provided_explicitly=True,
                resource="http://127.0.0.1:8000/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect_to_form).query)["request_id"][0]
        redirect_back = await provider.complete_authorization(
            request_id,
            AuthenticatedAccount(token="arch-token", username="tester", user_id=123),
        )
        code = parse_qs(urlparse(redirect_back).query)["code"][0]
        loaded_code = await provider.load_authorization_code(client, code)
        assert loaded_code is not None

        token = await provider.exchange_authorization_code(client, loaded_code)
        access = await provider.load_access_token(token.access_token)
        refresh = await provider.load_refresh_token(client, token.refresh_token or "")
        assert access is not None
        assert refresh is not None

        self.assertIsNone(token.expires_in)
        self.assertIsNone(access.expires_at)
        self.assertIsNone(refresh.expires_at)
        self.assertEqual(await redis_client.ttl(provider._key("access-token", token.access_token)), -1)
        self.assertEqual(await redis_client.ttl(provider._key("refresh-token", token.refresh_token or "")), -1)
        self.assertEqual(await redis_client.ttl(provider._key("session", access.session_id)), -1)


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode().rstrip("=")


def _collection_record(
    *,
    record_id: int,
    name: str,
    quantity: int,
    oracle_id: str,
    card_id: int,
) -> CollectionCardRecord:
    return CollectionCardRecord(
        record_id=record_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        quantity=quantity,
        foil=False,
        modifier=None,
        tags=(),
        condition_code=None,
        language_code=None,
        name=name,
        display_name=None,
        oracle_text="",
        mana_cost=None,
        cmc=None,
        colors=(),
        color_identity=(),
        supertypes=(),
        types=(),
        subtypes=(),
        type_line="",
        keywords=(),
        rarity=None,
        set_code=None,
        set_name=None,
        commander_legal=True,
        oracle_id=oracle_id,
        card_id=card_id,
        printing_id=None,
        edhrec_rank=None,
        image_uri=None,
        prices={},
    )


class _AvailabilityAuthClient:
    async def list_personal_decks(
        self,
        account: ArchidektAccount | AuthenticatedAccount,
        page_size: int = 100,
    ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
        del page_size
        resolved = (
            account
            if isinstance(account, AuthenticatedAccount)
            else AuthenticatedAccount(
                token=account.token or "secret",
                username=account.username,
                user_id=account.user_id,
            )
        )
        return (
            resolved,
            [
                PersonalDeckSummary(
                    id=7,
                    name="Existing Ramp",
                    owner_username=resolved.username,
                    owner_id=resolved.user_id,
                )
            ],
        )

    async def fetch_deck_cards(
        self,
        account: AuthenticatedAccount,
        deck_id: int,
        include_deleted: bool = False,
    ) -> dict[str, object]:
        del account
        del deck_id
        del include_deleted
        return {
            "cards": [
                {
                    "quantity": 1,
                    "categories": ["Ramp"],
                    "card": {
                        "displayName": "Sol Ring",
                        "oracleCard": {
                            "uid": "sol-ring-oracle",
                            "name": "Sol Ring",
                        },
                    },
                },
                {
                    "quantity": 1,
                    "categories": ["Ramp"],
                    "card": {
                        "displayName": "Arcane Signet",
                        "oracleCard": {
                            "uid": "arcane-signet-oracle",
                            "name": "Arcane Signet",
                        },
                    },
                },
            ]
        }


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

    async def test_collection_cache_hit_does_not_probe_redis_ttl(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=123,
            owner_id=456,
            owner_username="tester",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=0,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/123",
            records=[],
        )
        collection = CollectionLocator(username="tester")
        redis_client = TtlTrackingRedis()

        writer_cache = CollectionCache(FakeCollectionClient(snapshot), redis_client, ttl_seconds=86400)
        await writer_cache.get_snapshot(collection)

        reader_client = FakeCollectionClient(snapshot)
        reader_cache = CollectionCache(reader_client, redis_client, ttl_seconds=86400)
        cached_snapshot = await reader_cache.get_snapshot(collection)

        self.assertEqual(cached_snapshot.collection_id, 123)
        self.assertEqual(reader_client.calls, 0)
        self.assertEqual(redis_client.ttl_calls, [])


class CollectionReadExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_export_client_uses_archidekt_export_endpoint_pages(self) -> None:
        requests: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            requests.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "authorization": request.headers.get("Authorization"),
                    "payload": payload,
                }
            )
            page = int(payload["page"])
            content_by_page = {
                1: "Quantity,Name,Edition Code\r\n4,+2 Mace,afr\r\n",
                2: "1,Sol Ring,clb\r\n",
            }
            return httpx.Response(
                200,
                json={
                    "content": content_by_page[page],
                    "totalRows": 2,
                    "moreContent": page == 1,
                },
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = ArchidektPublicCollectionClient(http_client, RuntimeSettings())
            document = await client.fetch_collection_export(
                CollectionLocator(collection_id=809591),
                CollectionReadOptions(
                    fields=[
                        "quantity",
                        "card__oracleCard__name",
                        "card__edition__editioncode",
                    ],
                    page_size=1,
                ),
                auth_token="secret-token",
            )

        self.assertEqual(document.collection_id, 809591)
        self.assertEqual(document.total_rows, 2)
        self.assertEqual(document.fetched_pages, 2)
        self.assertFalse(document.more_available)
        self.assertEqual(
            document.csv_content,
            "Quantity,Name,Edition Code\r\n4,+2 Mace,afr\r\n1,Sol Ring,clb\r\n",
        )
        self.assertEqual(
            [request["url"] for request in requests],
            [
                "https://archidekt.com/api/collection/export/v2/809591/",
                "https://archidekt.com/api/collection/export/v2/809591/",
            ],
        )
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["authorization"], "JWT secret-token")
        self.assertEqual(requests[0]["payload"]["pageSize"], 1)

    async def test_read_collection_can_write_export_file_and_return_preview(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "content": "Quantity,Name,Edition Code\r\n1,Sol Ring,clb\r\n",
                    "totalRows": 1,
                    "moreContent": False,
                },
                request=request,
            )

        service = DeckbuildingService(RuntimeSettings())
        original_http_client = service.http_client
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            service.http_client = http_client
            service.archidekt_client = ArchidektPublicCollectionClient(
                http_client,
                service.settings,
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                file_path = os.path.join(tmpdir, "collection.csv")
                response = await service.read_collection(
                    CollectionLocator(collection_id=809591),
                    CollectionReadOptions(
                        fields=[
                            "quantity",
                            "card__oracleCard__name",
                            "card__edition__editioncode",
                        ],
                        export_to_file=True,
                        file_path=file_path,
                    ),
                )

                self.assertEqual(response.total_rows, 1)
                self.assertIsNone(response.csv_content)
                self.assertEqual(response.rows_preview[0]["Name"], "Sol Ring")
                self.assertIsNotNone(response.file)
                assert response.file is not None
                self.assertEqual(response.file.path, file_path)
                with open(file_path, encoding="utf-8", newline="") as exported_file:
                    self.assertEqual(
                        exported_file.read(),
                        "Quantity,Name,Edition Code\r\n1,Sol Ring,clb\r\n",
                    )
        await original_http_client.aclose()
        await service.redis_client.aclose()


class ArchidektCatalogSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_batches_multiple_exact_name_lookups_in_one_call(self) -> None:
        http_client = FakeCardCatalogHttpClient()
        client = ArchidektAuthenticatedClient(http_client, RuntimeSettings())
        filters = ArchidektCardSearchFilters(exact_name=["Sol Ring", "Arcane Signet"])

        results, total_matches, has_more = await client.search_cards(filters)

        self.assertEqual(len(http_client.calls), 2)
        self.assertEqual(
            [call["params"]["name"] for call in http_client.calls],
            ["Sol Ring", "Arcane Signet"],
        )
        self.assertEqual(total_matches, 2)
        self.assertFalse(has_more)
        self.assertEqual(
            [result.requested_exact_name for result in results],
            ["Sol Ring", "Arcane Signet"],
        )
        self.assertEqual([result.card_id for result in results], [150824, 150825])

    async def test_modify_deck_cards_retries_individual_cards_after_batch_400(self) -> None:
        http_client = FakeDeckMutationHttpClient()
        client = ArchidektAuthenticatedClient(http_client, RuntimeSettings())
        result = await client.modify_deck_cards(
            AuthenticatedAccount(token="secret", username="tester", user_id=1),
            deck_id=123,
            cards=[
                PersonalDeckCardMutation(action="add", card_id=150824, patch_id="ok-card"),
                PersonalDeckCardMutation(action="add", card_id=150825, patch_id="bad-card"),
            ],
        )

        self.assertEqual(len(http_client.calls), 3)
        self.assertEqual(result["successful_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["successful_mutations"][0]["request"]["patchId"], "ok-card")
        self.assertEqual(result["failed_mutations"][0]["request"]["patchId"], "bad-card")

    async def test_deck_card_mutation_payload_omits_null_modification_fields(self) -> None:
        client = ArchidektAuthenticatedClient(FakeDeckMutationHttpClient(), RuntimeSettings())
        payload = client._deck_card_mutation_payload(
            PersonalDeckCardMutation(
                action="add",
                card_id=150824,
                patch_id="null-filter-test",
                modifications=PersonalDeckCardModifications(quantity=1),
            ),
            1,
        )

        self.assertEqual(payload["modifications"], {"quantity": 1})

    async def test_deck_card_mutation_payload_turns_modify_quantity_zero_into_remove(self) -> None:
        client = ArchidektAuthenticatedClient(FakeDeckMutationHttpClient(), RuntimeSettings())
        payload = client._deck_card_mutation_payload(
            PersonalDeckCardMutation(
                action="modify",
                deck_relation_id=77,
                patch_id="remove-zero-test",
                categories=["Ramp"],
                modifications=PersonalDeckCardModifications(quantity=0, label="ignored"),
            ),
            1,
        )

        self.assertEqual(payload["action"], "remove")
        self.assertEqual(payload["patchId"], "remove-zero-test")
        self.assertEqual(payload["deckRelationId"], 77)
        self.assertNotIn("categories", payload)
        self.assertNotIn("modifications", payload)

    async def test_delete_collection_entries_uses_bulk_delete_payload(self) -> None:
        http_client = FakeCollectionDeleteHttpClient()
        client = ArchidektAuthenticatedClient(http_client, RuntimeSettings())

        result = await client.delete_collection_entries(
            AuthenticatedAccount(token="secret", username="tester", user_id=1),
            [9001, 9002],
        )

        self.assertEqual(result["deleted_ids"], [9001, 9002])
        self.assertEqual(len(http_client.calls), 1)
        self.assertEqual(http_client.calls[0]["method"], "DELETE")
        self.assertTrue(str(http_client.calls[0]["url"]).endswith("/api/collection/bulk/"))
        self.assertEqual(
            json.loads(str(http_client.calls[0]["content"])),
            {"ids": [9001, 9002]},
        )
        self.assertEqual(
            http_client.calls[0]["headers"],
            {
                "Authorization": "JWT secret",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def test_authenticated_request_renews_archidekt_token_after_401(self) -> None:
        seen_authorizations: list[str | None] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_authorizations.append(request.headers.get("Authorization"))
            if len(seen_authorizations) == 1:
                return httpx.Response(401, json={"detail": "expired"}, request=request)
            return httpx.Response(
                200,
                json={"id": 99, "name": "Renewed Deck"},
                request=request,
            )

        async def renew_account(account: AuthenticatedAccount) -> AuthenticatedAccount:
            self.assertEqual(account.token, "old-token")
            return AuthenticatedAccount(
                token="new-token",
                username=account.username,
                user_id=account.user_id,
                auth_session_id=account.auth_session_id,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = ArchidektAuthenticatedClient(
                http_client,
                RuntimeSettings(),
                renew_account=renew_account,
            )
            account = AuthenticatedAccount(
                token="old-token",
                username="tester",
                user_id=123,
                auth_session_id="session-1",
            )

            payload, summary = await client.create_deck(
                account,
                PersonalDeckCreateInput(name="Renewed Deck", deck_format=3),
            )

        self.assertEqual(payload["id"], 99)
        self.assertIsNotNone(summary)
        self.assertEqual(account.token, "new-token")
        self.assertEqual(seen_authorizations, ["JWT old-token", "JWT new-token"])

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

    async def test_private_snapshot_cache_hit_does_not_probe_redis_ttl(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=654,
            owner_username="private-user",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=0,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/321",
            records=[],
        )
        redis_client = TtlTrackingRedis()
        account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
        collection = CollectionLocator(collection_id=321)

        first_service = DeckbuildingService(RuntimeSettings())
        first_original_redis = first_service.redis_client
        first_service.redis_client = redis_client
        first_service.archidekt_client = FakeCollectionClient(snapshot)
        await first_original_redis.aclose()
        try:
            await first_service.get_snapshot(collection, account=account)
        finally:
            await first_service.http_client.aclose()

        second_service = DeckbuildingService(RuntimeSettings())
        second_original_redis = second_service.redis_client
        second_service.redis_client = redis_client
        second_service.archidekt_client = FakeCollectionClient(snapshot)
        await second_original_redis.aclose()
        try:
            cached_snapshot = await second_service.get_snapshot(collection, account=account)
            self.assertEqual(cached_snapshot.collection_id, 321)
            self.assertEqual(second_service.archidekt_client.calls, 0)
            self.assertEqual(redis_client.ttl_calls, [])
        finally:
            await second_service.http_client.aclose()


class AuthenticatedDeckListingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_identity_list_personal_decks_skips_curated_self(self) -> None:
        http_client = RecordingDeckListHttpClient()
        client = ArchidektAuthenticatedClient(http_client, RuntimeSettings())

        resolved_account, decks = await client.list_personal_decks(
            AuthenticatedAccount(token="secret", username="tester", user_id=123)
        )

        self.assertEqual(resolved_account.username, "tester")
        self.assertEqual(resolved_account.user_id, 123)
        self.assertEqual([deck.name for deck in decks], ["Known Identity Deck"])
        self.assertTrue(any("/api/decks/v3/" in url for url in http_client.urls))
        self.assertFalse(any("/api/decks/curated/self/" in url for url in http_client.urls))

    async def test_resolve_account_ignores_asserted_identity_for_token_auth(self) -> None:
        client = ArchidektAuthenticatedClient(IdentityResolvingHttpClient(), RuntimeSettings())

        resolved_account = await client.resolve_account(
            ArchidektAccount(token="secret", username="spoofed-user", user_id=999)
        )

        self.assertEqual(resolved_account.username, "verified-user")
        self.assertEqual(resolved_account.user_id, 123)


class AuthenticatedResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_owned_cards_backfills_archidekt_card_ids_when_snapshot_lacks_them(self) -> None:
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
                    quantity=1,
                    foil=False,
                    modifier=None,
                    tags=(),
                    condition_code=None,
                    language_code=None,
                    name="Sol Ring",
                    display_name=None,
                    oracle_text="{T}: Add {C}{C}.",
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
                    set_code="clb",
                    set_name="Commander Legends: Battle for Baldur's Gate",
                    commander_legal=True,
                    oracle_id="sol-ring-oracle",
                    card_id=None,
                    printing_id="sol-ring-printing",
                    edhrec_rank=1,
                    image_uri=None,
                    prices={"tcg": 1.25},
                )
            ],
        )
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        collection_client = FakeCollectionClient(snapshot)
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        service.auth_client = FakeCatalogLookupClient(
            {
                "Sol Ring": [
                    ArchidektCardReference(
                        card_id=150824,
                        requested_exact_name="Sol Ring",
                        oracle_id="sol-ring-oracle",
                        name="Sol Ring",
                        display_name="Sol Ring",
                        set_code="clb",
                        set_name="Commander Legends: Battle for Baldur's Gate",
                    )
                ]
            }
        )
        await original_redis.aclose()
        try:
            response = await service.search_owned_cards(
                CollectionLocator(username="tester"),
                CardSearchFilters(exact_name=["Sol Ring"]),
            )
            self.assertEqual(response.results[0].archidekt_card_ids, [150824])
        finally:
            await service.http_client.aclose()

    async def test_search_unowned_cards_adds_archidekt_card_ids_for_insertion(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=123,
            owner_id=456,
            owner_username="tester",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=0,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/123",
            records=[],
        )
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        collection_client = FakeCollectionClient(snapshot)
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        service.scryfall_client = FakeScryfallClient(
            [
                {
                    "name": "Arcane Signet",
                    "mana_cost": "{2}",
                    "cmc": 2,
                    "type_line": "Artifact",
                    "oracle_text": "{T}: Add one mana of any color in your commander's color identity.",
                    "colors": [],
                    "color_identity": [],
                    "keywords": [],
                    "rarity": "common",
                    "set": "cmm",
                    "set_name": "Commander Masters",
                    "finishes": ["nonfoil"],
                    "legalities": {"commander": "legal"},
                    "edhrec_rank": 10,
                    "prices": {"usd": "0.75"},
                    "oracle_id": "arcane-signet-oracle",
                    "scryfall_uri": "https://scryfall.com/card/cmm/arcane-signet",
                    "image_uris": {"normal": "https://img.example/arcane-signet.jpg"},
                }
            ]
        )
        service.auth_client = FakeCatalogLookupClient(
            {
                "Arcane Signet": [
                    ArchidektCardReference(
                        card_id=150825,
                        requested_exact_name="Arcane Signet",
                        oracle_id="arcane-signet-oracle",
                        name="Arcane Signet",
                        display_name="Arcane Signet",
                        set_code="cmm",
                        set_name="Commander Masters",
                    )
                ]
            }
        )
        await original_redis.aclose()
        try:
            response = await service.search_unowned_cards(
                CollectionLocator(username="tester"),
                CardSearchFilters(exact_name=["Arcane Signet"]),
            )
            self.assertEqual(response.results[0].archidekt_card_ids, [150825])
        finally:
            await service.http_client.aclose()

    async def test_check_collection_card_availability_blocks_used_up_collection_cards(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=321,
            owner_username="tester",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=2,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/321",
            records=[
                _collection_record(
                    record_id=1,
                    name="Sol Ring",
                    quantity=1,
                    oracle_id="sol-ring-oracle",
                    card_id=150824,
                ),
                _collection_record(
                    record_id=2,
                    name="Arcane Signet",
                    quantity=2,
                    oracle_id="arcane-signet-oracle",
                    card_id=150825,
                ),
            ],
        )
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        collection_client = FakeCollectionClient(snapshot)
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        service.auth_client = _AvailabilityAuthClient()
        await original_redis.aclose()
        try:
            response = await service.check_collection_card_availability(
                CollectionLocator(collection_id=321),
                [
                    CollectionAvailabilityCardRequest(name="Sol Ring"),
                    CollectionAvailabilityCardRequest(name="Arcane Signet"),
                ],
                CollectionAvailabilityOptions(collection_only=True),
                AuthenticatedAccount(token="secret", username="tester", user_id=321),
            )

            results_by_name = {result.matched_name: result for result in response.results}
            self.assertFalse(response.all_requested_available)
            self.assertEqual(response.blocked_count, 1)
            self.assertEqual(results_by_name["Sol Ring"].collection_quantity, 1)
            self.assertEqual(results_by_name["Sol Ring"].used_in_decks_quantity, 1)
            self.assertEqual(results_by_name["Sol Ring"].available_quantity, 0)
            self.assertFalse(results_by_name["Sol Ring"].enough_copies)
            self.assertTrue(results_by_name["Sol Ring"].must_not_use)
            self.assertEqual(results_by_name["Sol Ring"].status, "all_copies_used")
            self.assertEqual(results_by_name["Arcane Signet"].available_quantity, 1)
            self.assertTrue(results_by_name["Arcane Signet"].enough_copies)
            self.assertFalse(results_by_name["Arcane Signet"].must_not_use)
        finally:
            await service.http_client.aclose()

    async def test_authenticated_search_owned_cards_includes_available_quantity(self) -> None:
        snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=321,
            owner_username="tester",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=1,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/321",
            records=[
                _collection_record(
                    record_id=1,
                    name="Sol Ring",
                    quantity=1,
                    oracle_id="sol-ring-oracle",
                    card_id=150824,
                )
            ],
        )
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        collection_client = FakeCollectionClient(snapshot)
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        service.auth_client = _AvailabilityAuthClient()
        await original_redis.aclose()
        try:
            response = await service.search_owned_cards(
                CollectionLocator(collection_id=321),
                CardSearchFilters(exact_name=["Sol Ring"]),
                AuthenticatedAccount(token="secret", username="tester", user_id=321),
            )

            self.assertEqual(response.results[0].available_quantity, 0)
            self.assertFalse(response.results[0].collection_only_usable)
            self.assertIn("zero free copies", " ".join(response.notes))
        finally:
            await service.http_client.aclose()

    async def test_login_archidekt_includes_personal_decks_context(self) -> None:
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        service.auth_client = FakeAuthLoginClient()
        await original_redis.aclose()
        try:
            response = await service.login_archidekt(
                ArchidektAccount(username="tester", password="hunter2")
            )
            self.assertEqual(response.account.username, "tester")
            self.assertEqual(response.collection.collection_id, 123)
            self.assertIsNotNone(response.personal_decks)
            assert response.personal_decks is not None
            self.assertEqual(response.personal_decks.total_decks, 2)
            self.assertEqual(response.personal_decks.decks[0].name, "Artifacts")
        finally:
            await service.http_client.aclose()

    async def test_login_archidekt_uses_mcp_auth_context_when_account_is_omitted(self) -> None:
        service = DeckbuildingService(RuntimeSettings())
        service.auth_client = FakeAuthLoginClient()
        auth_token = ArchidektAccessToken(
            token="mcp-access-token",
            client_id="client-1",
            scopes=[AUTH_SCOPE],
            expires_at=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            resource="http://127.0.0.1:8000/mcp",
            archidekt_token="arch-token",
            archidekt_username="tester",
            archidekt_user_id=123,
            session_id="session-1",
        )
        reset_token = auth_context_var.set(AuthenticatedUser(auth_token))
        try:
            response = await service.login_archidekt()
            self.assertEqual(response.account.username, "tester")
            self.assertEqual(response.collection.collection_id, 123)
            self.assertIsNotNone(response.personal_decks)
            assert response.personal_decks is not None
            self.assertEqual(response.personal_decks.total_decks, 2)
            self.assertTrue(any("MCP auth session" in note for note in response.notes))
        finally:
            auth_context_var.reset(reset_token)
            await service.aclose()

    async def test_get_personal_deck_cards_maps_relation_and_card_ids(self) -> None:
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        service.auth_client = FakeAuthMutationClient()
        await original_redis.aclose()
        account = AuthenticatedAccount(token="secret", username="tester", user_id=1)
        try:
            response = await service.get_personal_deck_cards(deck_id=55, account=account)
            self.assertEqual(response.deck_id, 55)
            self.assertEqual(response.total_cards, 1)
            self.assertEqual(response.cards[0].deck_relation_id, 77)
            self.assertEqual(response.cards[0].archidekt_card_id, 150824)
            self.assertEqual(response.cards[0].name, "Sol Ring")
            self.assertEqual(response.cards[0].type_line, "Artifact")
        finally:
            await service.http_client.aclose()

    async def test_modify_personal_deck_cards_backfills_card_id_from_deck_relation_id(self) -> None:
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        fake_auth_client = FakeAuthMutationClient()
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="secret", username="tester", user_id=1)
        try:
            response = await service.modify_personal_deck_cards(
                deck_id=55,
                cards=[PersonalDeckCardMutation(action="remove", deck_relation_id=77)],
                account=account,
            )
            self.assertEqual(response.affected_count, 1)
            self.assertTrue(
                any("Backfilled Archidekt `card_id` values" in note for note in response.notes)
            )
            self.assertEqual(fake_auth_client.modify_calls[0][0].card_id, 150824)
        finally:
            await service.http_client.aclose()

    async def test_auth_context_keeps_private_collection_cache_user_isolated(self) -> None:
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
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        redis_client = FakeRedis()
        collection_client = FakeCollectionClient(snapshot)
        service.redis_client = redis_client
        service.cache.redis = redis_client
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        await original_redis.aclose()
        collection = CollectionLocator(collection_id=321)
        first_token = auth_context_var.set(
            AuthenticatedUser(
                ArchidektAccessToken(
                    token="mcp-access-token-1",
                    client_id="client-1",
                    scopes=[AUTH_SCOPE],
                    expires_at=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
                    resource="http://127.0.0.1:8000/mcp",
                    archidekt_token="arch-token-1",
                    archidekt_username="tester-1",
                    archidekt_user_id=111,
                    session_id="session-1",
                )
            )
        )
        try:
            first_response = await service.get_collection_overview(collection)
            self.assertEqual(first_response.collection_id, 321)
        finally:
            auth_context_var.reset(first_token)

        second_token = auth_context_var.set(
            AuthenticatedUser(
                ArchidektAccessToken(
                    token="mcp-access-token-2",
                    client_id="client-1",
                    scopes=[AUTH_SCOPE],
                    expires_at=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
                    resource="http://127.0.0.1:8000/mcp",
                    archidekt_token="arch-token-2",
                    archidekt_username="tester-2",
                    archidekt_user_id=222,
                    session_id="session-2",
                )
            )
        )
        try:
            second_response = await service.get_collection_overview(collection)
            self.assertEqual(second_response.collection_id, 321)
        finally:
            auth_context_var.reset(second_token)

        first_token_repeat = auth_context_var.set(
            AuthenticatedUser(
                ArchidektAccessToken(
                    token="mcp-access-token-1b",
                    client_id="client-1",
                    scopes=[AUTH_SCOPE],
                    expires_at=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
                    resource="http://127.0.0.1:8000/mcp",
                    archidekt_token="arch-token-1",
                    archidekt_username="tester-1",
                    archidekt_user_id=111,
                    session_id="session-1b",
                )
            )
        )
        try:
            cached_response = await service.get_collection_overview(collection)
            self.assertEqual(cached_response.collection_id, 321)
        finally:
            auth_context_var.reset(first_token_repeat)
            await service.http_client.aclose()

        self.assertEqual(collection_client.calls, 2)
        self.assertEqual(collection_client.auth_tokens, ["arch-token-1", "arch-token-2"])
        redis_keys = list(redis_client.storage)
        self.assertTrue(
            any("private:collection:private-collection:id:321:game:1:user:111" in key for key in redis_keys)
        )
        self.assertTrue(
            any("private:collection:private-collection:id:321:game:1:user:222" in key for key in redis_keys)
        )

    async def test_token_payload_cannot_read_another_users_warmed_private_cache(self) -> None:
        victim_snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=321,
            owner_username="victim-user",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=1,
            fetched_at=datetime.now(UTC),
            source_url="https://archidekt.com/collection/v2/321",
            records=[],
        )
        attacker_snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=999,
            owner_username="attacker-user",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=1,
            fetched_at=datetime.now(UTC) + timedelta(seconds=5),
            source_url="https://archidekt.com/collection/v2/321",
            records=[],
        )
        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        redis_client = FakeRedis()
        collection_client = FakeCollectionClient(victim_snapshot)
        service.redis_client = redis_client
        service.cache.redis = redis_client
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        service.auth_client = VerifiedTokenAuthClient(
            {
                "victim-token": AuthenticatedAccount(
                    token="victim-token", username="victim-user", user_id=321
                ),
                "attacker-token": AuthenticatedAccount(
                    token="attacker-token", username="attacker-user", user_id=999
                ),
            }
        )
        await original_redis.aclose()
        locator = CollectionLocator(collection_id=321)
        try:
            victim_response = await service.get_snapshot(
                locator,
                account=ArchidektAccount(token="victim-token"),
            )
            self.assertEqual(victim_response.owner_username, "victim-user")

            collection_client.snapshot = attacker_snapshot
            attacker_response = await service.get_snapshot(
                locator,
                account=ArchidektAccount(
                    token="attacker-token",
                    username="victim-user",
                    user_id=321,
                ),
            )

            self.assertEqual(attacker_response.owner_username, "attacker-user")
            self.assertEqual(collection_client.calls, 2)
            self.assertEqual(collection_client.auth_tokens, ["victim-token", "attacker-token"])
        finally:
            await service.aclose()

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
        account = AuthenticatedAccount(token="secret")
        resolved_account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
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
                service._private_snapshot_cache_key(private_locator, resolved_account),
            )
            public_key = service.cache._redis_key(public_locator.cache_key)

            self.assertIn(private_key, redis_client.storage)
            self.assertIn(public_key, redis_client.storage)

            response = await service.upsert_collection_entries(
                entries=[CollectionCardUpsert(card_id=150824, quantity=1, game=1)],
                account=account,
            )

            self.assertEqual(response.affected_count, 1)
            self.assertEqual(response.account_username, "private-user")
            self.assertNotIn(private_key, redis_client.storage)
            self.assertNotIn(public_key, redis_client.storage)
        finally:
            await service.http_client.aclose()

    async def test_delete_collection_entries_invalidates_public_and_private_caches(self) -> None:
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
        account = AuthenticatedAccount(token="secret")
        resolved_account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
        private_locator = CollectionLocator(collection_id=321)
        public_locator = CollectionLocator(username="private-user")

        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        service.redis_client = redis_client
        service.cache.redis = redis_client
        service.archidekt_client = FakeCollectionClient(snapshot)
        service.cache.client = service.archidekt_client
        fake_auth_client = FakeAuthMutationClient()
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        try:
            await service.get_snapshot(private_locator, account=account)
            await service.cache.get_snapshot(public_locator)

            private_key = service._private_redis_key(
                "collection",
                service._private_snapshot_cache_key(private_locator, resolved_account),
            )
            public_key = service.cache._redis_key(public_locator.cache_key)

            self.assertIn(private_key, redis_client.storage)
            self.assertIn(public_key, redis_client.storage)

            response = await service.delete_collection_entries(
                entries=[CollectionCardDelete(record_id=404552200, game=1)],
                account=account,
            )

            self.assertEqual(response.affected_count, 1)
            self.assertEqual(response.action, "delete")
            self.assertEqual(response.account_username, "private-user")
            self.assertEqual(fake_auth_client.delete_calls, [[404552200]])
            self.assertNotIn(private_key, redis_client.storage)
            self.assertNotIn(public_key, redis_client.storage)
        finally:
            await service.http_client.aclose()

    async def test_cross_service_invalidation_prevents_stale_private_memory_reuse(self) -> None:
        stale_snapshot = CollectionSnapshot(
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
        fresh_snapshot = CollectionSnapshot(
            collection_id=321,
            owner_id=321,
            owner_username="private-user",
            game=1,
            page_size=100,
            total_pages=1,
            total_records=2,
            fetched_at=datetime.now(UTC) + timedelta(seconds=5),
            source_url="https://archidekt.com/collection/v2/321",
            records=[],
        )
        redis_client = FakeRedis()
        account = AuthenticatedAccount(token="secret")
        locator = CollectionLocator(collection_id=321)

        first_service = DeckbuildingService(RuntimeSettings())
        first_original_redis = first_service.redis_client
        first_collection_client = FakeCollectionClient(stale_snapshot)
        first_service.redis_client = redis_client
        first_service.archidekt_client = first_collection_client
        first_service.auth_client = FakeAuthMutationClient()
        await first_original_redis.aclose()

        second_service = DeckbuildingService(RuntimeSettings())
        second_original_redis = second_service.redis_client
        second_service.redis_client = redis_client
        second_service.cache.redis = redis_client
        second_service.auth_client = FakeAuthMutationClient()
        await second_original_redis.aclose()

        try:
            first_response = await first_service.get_snapshot(locator, account=account)
            self.assertEqual(first_response.total_records, 1)
            self.assertEqual(first_collection_client.calls, 1)

            await second_service.upsert_collection_entries(
                entries=[CollectionCardUpsert(card_id=150824, quantity=1, game=1)],
                account=account,
            )

            first_collection_client.snapshot = fresh_snapshot
            refreshed_response = await first_service.get_snapshot(locator, account=account)

            self.assertEqual(refreshed_response.total_records, 2)
            self.assertEqual(first_collection_client.calls, 2)
        finally:
            await first_service.aclose()
            await second_service.aclose()

    async def test_recent_collection_write_bypasses_authenticated_snapshot_cache_while_marker_is_active(self) -> None:
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
        account = AuthenticatedAccount(token="secret")
        resolved_account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
        locator = CollectionLocator(collection_id=321)

        service = DeckbuildingService(RuntimeSettings())
        original_redis = service.redis_client
        redis_client = FakeRedis()
        collection_client = FakeCollectionClient(snapshot)
        service.redis_client = redis_client
        service.cache.redis = redis_client
        service.archidekt_client = collection_client
        service.cache.client = collection_client
        service.auth_client = FakeAuthMutationClient()
        await original_redis.aclose()
        try:
            await service.upsert_collection_entries(
                entries=[CollectionCardUpsert(card_id=150824, quantity=1, game=1)],
                account=account,
            )
            cache_key = service._private_snapshot_cache_key(locator, resolved_account)
            service._store_private_memory_cache(
                service._private_snapshot_cache,
                cache_key,
                snapshot,
            )

            first_result = await service.get_snapshot(locator, account=account)
            second_result = await service.get_snapshot(locator, account=account)
            marker_key = service._private_redis_key(
                "recent-collection-write",
                service._collection_write_marker_key(resolved_account, locator.game),
            )

            self.assertEqual(first_result.collection_id, 321)
            self.assertEqual(second_result.collection_id, 321)
            self.assertEqual(collection_client.calls, 2)
            self.assertNotIn(
                service._private_redis_key("collection", cache_key),
                redis_client.storage,
            )
            self.assertIn(marker_key, redis_client.storage)
        finally:
            await service.http_client.aclose()

    async def test_recent_collection_write_marker_is_shared_across_services(self) -> None:
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
        account = AuthenticatedAccount(token="secret")
        resolved_account = AuthenticatedAccount(token="secret", username="private-user", user_id=321)
        locator = CollectionLocator(collection_id=321)
        redis_client = FakeRedis()

        first_service = DeckbuildingService(RuntimeSettings())
        first_original_redis = first_service.redis_client
        first_collection_client = FakeCollectionClient(snapshot)
        first_service.redis_client = redis_client
        first_service.cache.redis = redis_client
        first_service.archidekt_client = first_collection_client
        first_service.cache.client = first_collection_client
        first_service.auth_client = FakeAuthMutationClient()
        await first_original_redis.aclose()

        second_service = DeckbuildingService(RuntimeSettings())
        second_original_redis = second_service.redis_client
        second_service.redis_client = redis_client
        second_service.cache.redis = redis_client
        second_service.archidekt_client = FakeCollectionClient(snapshot)
        second_service.cache.client = second_service.archidekt_client
        second_service.auth_client = FakeAuthMutationClient()
        await second_original_redis.aclose()

        try:
            await first_service.get_snapshot(locator, account=account)
            await second_service.upsert_collection_entries(
                entries=[CollectionCardUpsert(card_id=150824, quantity=1, game=1)],
                account=account,
            )

            first_result = await first_service.get_snapshot(locator, account=account)
            second_result = await first_service.get_snapshot(locator, account=account)
            cache_key = first_service._private_redis_key(
                "collection",
                first_service._private_snapshot_cache_key(locator, resolved_account),
            )

            self.assertEqual(first_result.collection_id, 321)
            self.assertEqual(second_result.collection_id, 321)
            self.assertEqual(first_collection_client.calls, 3)
            self.assertNotIn(cache_key, redis_client.storage)
        finally:
            await first_service.aclose()
            await second_service.aclose()


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


class OAuthHttpRouteTests(unittest.TestCase):
    def test_stale_oauth_archidekt_token_returns_reconnect_required(self) -> None:
        redis_client = FakeRedis()
        provider = RedisArchidektOAuthProvider(
            redis_client,
            key_prefix="archidekt-commander",
            issuer_url="https://testserver",
        )
        session = provider._build_session(
            client_id="web-ui-client",
            scopes=[AUTH_SCOPE],
            resource="https://testserver/mcp",
            archidekt_token="expired-archidekt-token",
            archidekt_username="oauth-user",
            archidekt_user_id=111,
        )
        asyncio.run(provider._store_session(session["access"], session["refresh"], session["record"]))

        async def fake_list_personal_decks(
            self,
            account: ArchidektAccount | AuthenticatedAccount,
            page_size: int = 100,
        ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
            del self
            del account
            del page_size
            request = httpx.Request("GET", "https://archidekt.com/api/decks/v3/")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("archidekt token expired", request=request, response=response)

        with (
            patch("archidekt_commander_mcp.app_factory.redis_async.from_url", return_value=redis_client),
            patch(
                "archidekt_commander_mcp.app_factory.ArchidektAuthenticatedClient.list_personal_decks",
                new=fake_list_personal_decks,
            ),
        ):
            server = create_server(
                RuntimeSettings(
                    auth_enabled=True,
                    public_base_url="https://testserver",
                    redis_url="redis://fake/0",
                )
            )
            client = TestClient(server.streamable_http_app(), raise_server_exceptions=False)
            response = client.post(
                "/api/login",
                json={},
                headers={"Authorization": f"Bearer {session['access'].token}"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"],
            "Archidekt authentication needs attention. Reconnect Archidekt and try again.",
        )

    def test_oauth_round_trip_supports_accountless_private_api_calls(self) -> None:
        redis_client = FakeRedis()

        async def fake_login(
            self,
            account: ArchidektAccount,
        ) -> AuthenticatedAccount:
            del self
            identifier = account.username or account.email or "unknown"
            return AuthenticatedAccount(
                token=f"arch-token-{identifier}",
                username=account.username or "oauth-user",
                user_id=111,
            )

        async def fake_list_personal_decks(
            self,
            account: ArchidektAccount | AuthenticatedAccount,
            page_size: int = 100,
        ) -> tuple[AuthenticatedAccount, list[PersonalDeckSummary]]:
            del self
            del page_size
            if isinstance(account, AuthenticatedAccount):
                resolved = account
            else:
                resolved = AuthenticatedAccount(
                    token=account.token or "arch-token-oauth-user",
                    username=account.username or "oauth-user",
                    user_id=account.user_id or 111,
                )
            return (
                resolved,
                [PersonalDeckSummary(id=17, name="OAuth Deck", owner_username=resolved.username, owner_id=resolved.user_id)],
            )

        with (
            patch("archidekt_commander_mcp.app_factory.redis_async.from_url", return_value=redis_client),
            patch("archidekt_commander_mcp.app_factory.ArchidektAuthenticatedClient.login", new=fake_login),
            patch(
                "archidekt_commander_mcp.app_factory.ArchidektAuthenticatedClient.list_personal_decks",
                new=fake_list_personal_decks,
            ),
        ):
            server = create_server(
                RuntimeSettings(
                    auth_enabled=True,
                    public_base_url="https://testserver",
                    redis_url="redis://fake/0",
                )
            )
            client = TestClient(server.streamable_http_app())

            registration_response = client.post(
                "/register",
                json={
                    "redirect_uris": ["https://chat.openai.com/a/oauth/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": AUTH_SCOPE,
                    "client_name": "Test MCP Client",
                },
            )
            self.assertEqual(registration_response.status_code, 201)
            client_id = registration_response.json()["client_id"]

            verifier = "oauth-verifier-123"
            authorize_response = client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "https://chat.openai.com/a/oauth/callback",
                    "scope": AUTH_SCOPE,
                    "state": "state-123",
                    "resource": "https://testserver/mcp",
                    "code_challenge": _pkce_challenge(verifier),
                    "code_challenge_method": "S256",
                },
                follow_redirects=False,
            )
            self.assertEqual(authorize_response.status_code, 302)
            auth_login_url = authorize_response.headers["location"]
            self.assertIn("/auth/archidekt-login?request_id=", auth_login_url)

            login_page_response = client.get(auth_login_url)
            self.assertEqual(login_page_response.status_code, 200)
            self.assertIn("Connect Archidekt", login_page_response.text)

            request_id = parse_qs(urlparse(auth_login_url).query)["request_id"][0]
            auth_form_response = client.post(
                "/auth/archidekt-login",
                data={
                    "request_id": request_id,
                    "identifier": "oauth-user",
                    "password": "super-secret",
                },
                follow_redirects=False,
            )
            self.assertEqual(auth_form_response.status_code, 302)
            redirect_back = auth_form_response.headers["location"]
            redirect_query = parse_qs(urlparse(redirect_back).query)
            self.assertEqual(redirect_query["state"][0], "state-123")
            code = redirect_query["code"][0]

            token_response = client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "redirect_uri": "https://chat.openai.com/a/oauth/callback",
                    "code_verifier": verifier,
                },
            )
            self.assertEqual(token_response.status_code, 200)
            access_token = token_response.json()["access_token"]
            self.assertTrue(access_token)
            self.assertIsNone(token_response.json().get("expires_in"))

            login_response = client.post(
                "/api/login",
                json={},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(login_response.status_code, 200)
            login_payload = login_response.json()
            self.assertEqual(login_payload["account"]["username"], "oauth-user")
            self.assertEqual(login_payload["collection"]["collection_id"], 111)
            self.assertEqual(login_payload["personal_decks"]["total_decks"], 1)
            self.assertEqual(login_payload["personal_decks"]["decks"][0]["name"], "OAuth Deck")
            self.assertTrue(any("MCP auth session" in note for note in login_payload["notes"]))

            decks_response = client.post(
                "/api/personal-decks",
                json={},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(decks_response.status_code, 200)
            self.assertEqual(decks_response.json()["decks"][0]["name"], "OAuth Deck")


class ArchidektRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_429_with_retry_after_header(self) -> None:
        sleep_durations: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        responses = []
        for _ in range(2):
            response = httpx.Response(429, headers={"Retry-After": "2.5"})
            response._request = httpx.Request("GET", "https://archidekt.com/api/cards/v2/")
            responses.append(response)
        success_response = httpx.Response(
            200,
            json={"count": 1, "next": None, "results": []},
        )
        success_response._request = httpx.Request("GET", "https://archidekt.com/api/cards/v2/")
        responses.append(success_response)

        call_count = 0

        class FakeGate:
            async def wait_for_slot(self) -> None:
                pass

        class FakeHttpClient:
            async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
                nonlocal call_count
                response = responses[call_count]
                call_count += 1
                return response

        settings = RuntimeSettings(archidekt_retry_max_attempts=3, archidekt_retry_base_delay_seconds=1.0)
        gate = ArchidektRequestGate.from_settings(settings)
        gate._sleep = fake_sleep
        client = ArchidektAuthenticatedClient(FakeHttpClient(), settings, request_gate=gate)

        result = await client._request_archidekt("GET", "https://archidekt.com/api/cards/v2/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(call_count, 3)
        self.assertEqual(len(sleep_durations), 2)
        self.assertAlmostEqual(sleep_durations[0], 2.5, places=1)

    async def test_retries_with_exponential_backoff_without_retry_after(self) -> None:
        sleep_durations: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        responses = []
        for _ in range(2):
            response = httpx.Response(429)
            response._request = httpx.Request("GET", "https://archidekt.com/api/cards/v2/")
            responses.append(response)
        success_response = httpx.Response(
            200,
            json={"count": 1, "next": None, "results": []},
        )
        success_response._request = httpx.Request("GET", "https://archidekt.com/api/cards/v2/")
        responses.append(success_response)

        call_count = 0

        class FakeGate:
            async def wait_for_slot(self) -> None:
                pass

        class FakeHttpClient:
            async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
                nonlocal call_count
                response = responses[call_count]
                call_count += 1
                return response

        settings = RuntimeSettings(archidekt_retry_max_attempts=3, archidekt_retry_base_delay_seconds=1.0)
        gate = ArchidektRequestGate.from_settings(settings)
        gate._sleep = fake_sleep
        client = ArchidektAuthenticatedClient(FakeHttpClient(), settings, request_gate=gate)

        result = await client._request_archidekt("GET", "https://archidekt.com/api/cards/v2/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(call_count, 3)
        self.assertEqual(len(sleep_durations), 2)
        self.assertAlmostEqual(sleep_durations[0], 1.0, places=1)
        self.assertAlmostEqual(sleep_durations[1], 2.0, places=1)

    async def test_raises_final_429_after_max_attempts(self) -> None:
        sleep_durations: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        call_count = 0

        class FakeGate:
            async def wait_for_slot(self) -> None:
                pass

        class FakeHttpClient:
            async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                response = httpx.Response(429)
                response._request = httpx.Request("GET", "https://archidekt.com/api/cards/v2/")
                return response

        settings = RuntimeSettings(archidekt_retry_max_attempts=3, archidekt_retry_base_delay_seconds=1.0)
        gate = ArchidektRequestGate.from_settings(settings)
        gate._sleep = fake_sleep
        client = ArchidektAuthenticatedClient(FakeHttpClient(), settings, request_gate=gate)

        result = await client._request_archidekt("GET", "https://archidekt.com/api/cards/v2/")
        self.assertEqual(result.status_code, 429)
        self.assertEqual(call_count, 3)
        self.assertEqual(len(sleep_durations), 2)

    async def test_non_429_errors_are_not_retried(self) -> None:
        call_count = 0

        class FakeGate:
            async def wait_for_slot(self) -> None:
                pass

        class FakeHttpClient:
            async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                response = httpx.Response(500)
                response._request = httpx.Request("GET", "https://archidekt.com/api/cards/v2/")
                return response

        settings = RuntimeSettings(archidekt_retry_max_attempts=3, archidekt_retry_base_delay_seconds=1.0)
        gate = ArchidektRequestGate.from_settings(settings)
        gate._sleep = lambda s: None
        client = ArchidektAuthenticatedClient(FakeHttpClient(), settings, request_gate=gate)

        result = await client._request_archidekt("GET", "https://archidekt.com/api/cards/v2/")
        self.assertEqual(result.status_code, 500)
        self.assertEqual(call_count, 1)


class AuthenticatedDeckListCachingTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_token_only_authenticated_deck_list_calls_reuse_cache(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        fake_auth_client = TokenScopedDeckListClient()
        service.redis_client = fake_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="token-repeat")
        try:
            first_account, first_decks = await service._get_authenticated_deck_list(account)
            second_account, second_decks = await service._get_authenticated_deck_list(account)

            cache_key = service._private_redis_key(
                "authenticated-deck-list",
                service._private_authenticated_deck_list_cache_key(account),
            )
            self.assertEqual(first_account.token, "token-repeat")
            self.assertEqual(second_account.token, "token-repeat")
            self.assertEqual(first_account.username, "tester")
            self.assertEqual(second_account.username, "tester")
            self.assertEqual(first_decks[0].name, "Deck for token-repeat")
            self.assertEqual(second_decks[0].name, "Deck for token-repeat")
            self.assertEqual(fake_auth_client.list_calls, ["token-repeat"])
            self.assertIn(cache_key, fake_redis.storage)
        finally:
            await service.aclose()

    async def test_different_tokens_with_same_claimed_identity_do_not_share_authenticated_deck_list_cache(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        fake_auth_client = TokenScopedDeckListClient()
        service.redis_client = fake_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        first_account = AuthenticatedAccount(token="token-alpha", username="tester", user_id=123)
        second_account = AuthenticatedAccount(token="token-bravo", username="tester", user_id=123)
        try:
            _, first_decks = await service._get_authenticated_deck_list(first_account)
            _, second_decks = await service._get_authenticated_deck_list(second_account)

            first_cache_key = service._private_redis_key(
                "authenticated-deck-list",
                service._private_authenticated_deck_list_cache_key(first_account),
            )
            second_cache_key = service._private_redis_key(
                "authenticated-deck-list",
                service._private_authenticated_deck_list_cache_key(second_account),
            )
            self.assertEqual(fake_auth_client.list_calls, ["token-alpha", "token-bravo"])
            self.assertEqual(first_decks[0].name, "Deck for token-alpha")
            self.assertEqual(second_decks[0].name, "Deck for token-bravo")
            self.assertNotEqual(first_cache_key, second_cache_key)
            self.assertIn(first_cache_key, fake_redis.storage)
            self.assertIn(second_cache_key, fake_redis.storage)
        finally:
            await service.aclose()

    async def test_login_and_list_personal_decks_share_cached_deck_list(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_auth_client = CountingFakeAuthLoginClient()
        service.redis_client = FakeRedis()
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        try:
            login_response = await service.login_archidekt(
                ArchidektAccount(username="tester", password="hunter2")
            )
            self.assertEqual(login_response.account.username, "tester")

            response = await service.list_personal_decks(login_response.account)
            self.assertEqual(response.owner_username, "tester")
            self.assertEqual(response.total_decks, 2)
            self.assertEqual(fake_auth_client.list_calls, 1)
        finally:
            await service.aclose()

    async def test_deck_write_invalidates_deck_list_cache(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="secret", username="tester", user_id=123)
        try:
            await service.list_personal_decks(account)
            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="New Deck", deck_format=1),
                account=account,
            )

            list_response = await service.list_personal_decks(account)
            self.assertEqual(list_response.owner_username, "tester")
            self.assertEqual(fake_auth_client.list_calls, ["secret", "secret"])
        finally:
            await service.aclose()

    async def test_deck_write_clears_local_deck_list_cache_when_redis_invalidation_fails(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        failing_redis = FailingDeckListRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = failing_redis
        service.cache.redis = failing_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="secret", username="tester", user_id=123)
        try:
            await service.list_personal_decks(account)
            failing_redis.fail_execute = True

            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Invalidate Failure", deck_format=1),
                account=account,
            )

            list_response = await service.list_personal_decks(account)
            self.assertEqual(list_response.owner_username, "tester")
            self.assertEqual(fake_auth_client.list_calls, ["secret", "secret"])
        finally:
            await service.aclose()

    async def test_deck_write_invalidates_deck_list_cache_for_other_tokens_of_same_user(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        first_account = AuthenticatedAccount(token="token-alpha", username="tester", user_id=123)
        second_account = AuthenticatedAccount(token="token-bravo", username="tester", user_id=123)
        try:
            await service.list_personal_decks(first_account)
            await service.list_personal_decks(second_account)
            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Updated Deck", deck_format=1),
                account=first_account,
            )

            response = await service.list_personal_decks(second_account)
            self.assertEqual(response.owner_username, "tester")
            self.assertEqual(
                fake_auth_client.list_calls,
                ["token-alpha", "token-bravo", "token-bravo"],
            )
        finally:
            await service.aclose()

    async def test_deck_write_invalidates_deck_list_cache_across_services_for_same_user(self) -> None:
        shared_redis = FakeRedis()
        shared_auth_client = TokenScopedDeckMutationClient()

        first_service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        first_original_redis = first_service.redis_client
        first_service.redis_client = shared_redis
        first_service.cache.redis = shared_redis
        first_service.auth_client = shared_auth_client
        await first_original_redis.aclose()

        second_service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        second_original_redis = second_service.redis_client
        second_service.redis_client = shared_redis
        second_service.cache.redis = shared_redis
        second_service.auth_client = shared_auth_client
        await second_original_redis.aclose()

        first_account = AuthenticatedAccount(token="token-alpha", username="tester", user_id=123)
        second_account = AuthenticatedAccount(token="token-bravo", username="tester", user_id=123)
        try:
            await first_service.list_personal_decks(first_account)
            await second_service.list_personal_decks(second_account)
            await first_service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Shared Update", deck_format=1),
                account=first_account,
            )

            response = await second_service.list_personal_decks(second_account)
            self.assertEqual(response.owner_username, "tester")
            self.assertEqual(
                shared_auth_client.list_calls,
                ["token-alpha", "token-bravo", "token-bravo"],
            )
        finally:
            await first_service.aclose()
            await second_service.aclose()

    async def test_deck_write_invalidates_personal_deck_usage_cache(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="usage-token", username="tester", user_id=123)
        try:
            await service._get_personal_deck_usage_snapshot(account)
            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Usage Refresh", deck_format=1),
                account=account,
            )
            await service._get_personal_deck_usage_snapshot(account)

            self.assertEqual(fake_auth_client.list_calls, ["usage-token", "usage-token"])
            self.assertEqual(len(fake_auth_client.deck_card_calls), 2)
        finally:
            await service.aclose()

    async def test_deck_write_invalidates_token_only_personal_deck_usage_cache(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        fake_redis = FakeRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = fake_redis
        service.cache.redis = fake_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        token_only_account = AuthenticatedAccount(token="usage-token")
        resolved_account = AuthenticatedAccount(token="usage-token", username="tester", user_id=123)
        try:
            await service._get_personal_deck_usage_snapshot(token_only_account)
            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Usage Token Refresh", deck_format=1),
                account=resolved_account,
            )
            await service._get_personal_deck_usage_snapshot(token_only_account)

            self.assertEqual(fake_auth_client.list_calls, ["usage-token", "usage-token"])
            self.assertEqual(len(fake_auth_client.deck_card_calls), 2)
        finally:
            await service.aclose()

    async def test_deck_write_refreshes_personal_deck_usage_when_usage_redis_invalidation_fails(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        failing_redis = FailingDeckListRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = failing_redis
        service.cache.redis = failing_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="usage-token", username="tester", user_id=123)
        try:
            await service._get_personal_deck_usage_snapshot(account)
            failing_redis.fail_delete = True

            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Usage Redis Failure", deck_format=1),
                account=account,
            )
            await service.list_personal_decks(account)
            await service._get_personal_deck_usage_snapshot(account)

            self.assertEqual(fake_auth_client.list_calls, ["usage-token", "usage-token"])
            self.assertEqual(len(fake_auth_client.deck_card_calls), 2)
        finally:
            await service.aclose()

    async def test_deck_write_refreshes_deck_list_when_usage_read_happens_first_after_redis_invalidation_failure(self) -> None:
        service = DeckbuildingService(RuntimeSettings(personal_deck_cache_ttl_seconds=900))
        original_redis = service.redis_client
        failing_redis = FailingDeckListRedis()
        fake_auth_client = TokenScopedDeckMutationClient()
        service.redis_client = failing_redis
        service.cache.redis = failing_redis
        service.auth_client = fake_auth_client
        await original_redis.aclose()
        account = AuthenticatedAccount(token="mirror-token", username="tester", user_id=123)
        try:
            await service.list_personal_decks(account)
            await service._get_personal_deck_usage_snapshot(account)
            failing_redis.fail_delete = True

            await service.create_personal_deck(
                deck=PersonalDeckCreateInput(name="Mirror Redis Failure", deck_format=1),
                account=account,
            )
            await service._get_personal_deck_usage_snapshot(account)
            await service.list_personal_decks(account)

            self.assertEqual(fake_auth_client.list_calls, ["mirror-token", "mirror-token"])
            self.assertEqual(len(fake_auth_client.deck_card_calls), 2)
        finally:
            await service.aclose()


class ExactNameCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_name_search_warms_cache_for_backfill(self) -> None:
        settings = RuntimeSettings(personal_deck_cache_ttl_seconds=0, archidekt_exact_name_cache_ttl_seconds=900)
        http_client = FakeCardCatalogHttpClient()
        client = ArchidektAuthenticatedClient(http_client, settings, redis_client=None)
        filters = ArchidektCardSearchFilters(exact_name=["Lightning Bolt"], game=1)

        result1, total1, has_more1 = await client.search_cards(filters)
        self.assertEqual(len(result1), 1)
        self.assertEqual(result1[0].name, "Lightning Bolt")
        first_call_count = len(http_client.calls)

        result2, total2, has_more2 = await client.search_cards(filters)
        self.assertEqual(len(result2), 1)
        self.assertEqual(result2[0].name, "Lightning Bolt")

        self.assertEqual(len(http_client.calls), first_call_count,
                         "Second exact name search should hit cache and not make additional HTTP requests")

    async def test_query_search_is_not_cached(self) -> None:
        settings = RuntimeSettings(personal_deck_cache_ttl_seconds=0, archidekt_exact_name_cache_ttl_seconds=900)
        http_client = FakeCardCatalogHttpClient()
        client = ArchidektAuthenticatedClient(http_client, settings, redis_client=None)

        filters = ArchidektCardSearchFilters(query="Lightning", game=1)
        await client.search_cards(filters)
        await client.search_cards(filters)

        self.assertGreaterEqual(len(http_client.calls), 2,
                                 "Query-based search should not be cached and should make fresh requests")

    async def test_exact_name_cache_is_case_insensitive(self) -> None:
        settings = RuntimeSettings(personal_deck_cache_ttl_seconds=0, archidekt_exact_name_cache_ttl_seconds=900)
        http_client = FakeCardCatalogHttpClient()
        client = ArchidektAuthenticatedClient(http_client, settings, redis_client=None)

        await client.search_cards(ArchidektCardSearchFilters(exact_name=["Lightning Bolt"], game=1))
        first_call_count = len(http_client.calls)
        await client.search_cards(ArchidektCardSearchFilters(exact_name=["lightning bolt"], game=1))

        self.assertEqual(
            len(http_client.calls),
            first_call_count,
            "Exact-name cache should reuse case-insensitive key for the same name.",
        )


class ArchidektRequestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_archidekt_gate_delays_requests_beyond_budget(self) -> None:
        current_time = 0.0
        sleep_calls: list[float] = []

        def fake_time() -> float:
            return current_time

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            nonlocal current_time
            current_time += seconds

        gate = ArchidektRequestGate(
            max_requests=2,
            window_seconds=1.0,
            time_source=fake_time,
            sleep=fake_sleep,
        )

        await gate.wait_for_slot()
        await gate.wait_for_slot()

        self.assertEqual(len(sleep_calls), 0)

        current_time = 0.5

        await gate.wait_for_slot()
        self.assertGreater(len(sleep_calls), 0,
                           "Third request in the same window should have been delayed")

    async def test_scryfall_requests_bypass_gate(self) -> None:
        gate_calls = 0

        class CountingGate:
            async def wait_for_slot(self) -> None:
                nonlocal gate_calls
                gate_calls += 1

        settings = RuntimeSettings()
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(5))
        CountingGate()

        ScryfallClient(http_client, settings)
        self.assertEqual(gate_calls, 0,
                          "ScryfallClient should not use the Archidekt request gate")

        await http_client.aclose()


if __name__ == "__main__":
    unittest.main()
