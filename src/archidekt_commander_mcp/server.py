from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import httpx
import redis.asyncio as redis_async
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

if __package__ in {None, ""}:
    import sys

    package_root = Path(__file__).resolve().parents[1]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

    from archidekt_commander_mcp.clients import (
        ArchidektPublicCollectionClient,
        CollectionCache,
        ScryfallClient,
        card_matches_scryfall_filters,
        scryfall_price_key,
    )
    from archidekt_commander_mcp.config import RuntimeSettings
    from archidekt_commander_mcp.filtering import (
        aggregate_owned_results,
        paginate_results,
        record_matches_filters,
        sort_card_results,
    )
    from archidekt_commander_mcp.models import (
        CardResult,
        CardSearchFilters,
        CollectionLocator,
        CollectionOverview,
        CollectionOverviewRequest,
        CollectionSearchRequest,
        SearchResponse,
    )
    from archidekt_commander_mcp.webui import render_home_page
else:
    from .clients import (
        ArchidektPublicCollectionClient,
        CollectionCache,
        ScryfallClient,
        card_matches_scryfall_filters,
        scryfall_price_key,
    )
    from .config import RuntimeSettings
    from .filtering import (
        aggregate_owned_results,
        paginate_results,
        record_matches_filters,
        sort_card_results,
    )
    from .models import (
        CardResult,
        CardSearchFilters,
        CollectionLocator,
        CollectionOverview,
        CollectionOverviewRequest,
        CollectionSearchRequest,
        SearchResponse,
    )
    from .webui import render_home_page


SERVER_INSTRUCTIONS = """
You are a stateless Commander deckbuilding MCP server.

Every tool call must include a `collection` object containing one of:
- `collection_id`
- `collection_url`
- `username`

Optional collection fields:
- `game` (1 = Paper, 2 = MTGO, 3 = Arena)

Stateless rules:
- Never assume the server remembers a previous user's collection.
- Reuse the `collection` object in every call related to the same user request.
- If the user asks about owned cards, use `search_owned_cards`.
- If the user asks about missing cards or upgrades, use `search_unowned_cards`.
- Use `get_collection_overview` when you need context on the owned pool.

Filter mapping:
- Prefer `color_identity` for Commander logic.
- Use `type_includes`, `subtype_includes`, `supertypes_includes` and `oracle_terms_*`
  to express roles like ramp, draw, recursion, removal, board wipe and finisher.
- Keep the semantic reasoning in the model and let the server enforce deterministic filters.

Final response format:
- Use this as the default response structure unless the user explicitly asks for a different format.
- If the user asks for a different output format, keep the same card choices but adapt the presentation.
- Start with a short strategy guide that explains how the deck should play.
- The strategy guide should describe the game plan, key synergies, pacing, and win conditions.
- When you present deck additions or recommendations, group cards by category.
- Use a plain category heading, then list one card per line as `N Card Name`.
- `N` must be the exact quantity of that card to add to the deck.
- Do not use bullets or numbering for card lines.
- Example:
  Strategy Guide
  Use early ramp to fix mana, trade resources efficiently, then pull ahead with recursive value.
  Prioritize hands with fixing, one early accelerator, and one payoff engine.

  Ramp
  1 Sol Ring
  1 Arcane Signet

  Removal
  1 Swords to Plowshares
""".strip()
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


class DeckbuildingService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": settings.user_agent},
        )
        self.redis_client = redis_async.from_url(settings.redis_url, decode_responses=True)
        self.archidekt_client = ArchidektPublicCollectionClient(self.http_client, settings)
        self.scryfall_client = ScryfallClient(self.http_client, settings)
        self.cache = CollectionCache(
            self.archidekt_client,
            self.redis_client,
            settings.cache_ttl_seconds,
            settings.redis_key_prefix,
        )
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for_collection(self, collection: CollectionLocator) -> asyncio.Lock:
        return self._locks.setdefault(collection.cache_key, asyncio.Lock())

    async def get_snapshot(
        self,
        collection: CollectionLocator,
        force_refresh: bool = False,
    ):
        async with self._lock_for_collection(collection):
            return await self.cache.get_snapshot(collection, force_refresh=force_refresh)

    async def get_collection_overview(self, collection: CollectionLocator) -> CollectionOverview:
        snapshot = await self.get_snapshot(collection)
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
    ) -> SearchResponse:
        snapshot = await self.get_snapshot(collection)
        matching_records = [
            record for record in snapshot.records if record_matches_filters(record, filters)
        ]
        results = aggregate_owned_results(
            matching_records,
            filters,
            collection_id=snapshot.collection_id,
            base_url=self.settings.normalized_archidekt_base_url,
        )
        sorted_results = sort_card_results(results, filters)
        paged_results = paginate_results(sorted_results, filters.page, filters.limit)
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
            notes=[
                f"Collection snapshot fetched at {snapshot.fetched_at.isoformat()}",
                f"Collection locator: {describe_collection_locator(collection)}",
                "Deterministic search executed against the requested public Archidekt collection.",
            ],
            results=paged_results,
        )

    async def search_unowned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
    ) -> SearchResponse:
        snapshot = await self.get_snapshot(collection)
        raw_cards, query_used, has_more, notes = await self.scryfall_client.search_unowned_cards(
            filters=filters,
            owned_oracle_ids=snapshot.owned_oracle_ids,
            owned_names=snapshot.owned_names,
        )

        filtered_cards = [card for card in raw_cards if card_matches_scryfall_filters(card, filters)]
        mapped_results = [self._map_scryfall_card(card, filters) for card in filtered_cards]
        sorted_results = sort_card_results(mapped_results, filters)
        paged_results = paginate_results(sorted_results, filters.page, filters.limit)

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
                "Owned cards were excluded deterministically using the requested Archidekt collection.",
            ],
            results=paged_results,
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


def create_server(runtime_settings: RuntimeSettings | None = None) -> FastMCP:
    runtime = runtime_settings or RuntimeSettings()
    service_state: dict[str, DeckbuildingService | None] = {"service": None}

    def build_service() -> DeckbuildingService:
        return DeckbuildingService(runtime)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        normalized_level = configure_logging(runtime.log_level)
        LOGGER.info(
            "Starting Archidekt Commander MCP server transport=%s host=%s port=%s mcp_path=%s log_level=%s",
            runtime.transport,
            runtime.host,
            runtime.port,
            runtime.streamable_http_path,
            normalized_level,
        )
        try:
            yield
        finally:
            LOGGER.info("Shutting down Archidekt Commander MCP server")

    server = FastMCP(
        name="archidekt-commander",
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://archidekt.com",
        dependencies=["httpx", "mcp", "pydantic", "redis", "starlette", "uvicorn"],
        log_level=runtime.log_level,
        host=runtime.host,
        port=runtime.port,
        streamable_http_path=runtime.streamable_http_path,
        stateless_http=runtime.stateless_http,
        lifespan=lifespan,
    )

    async def get_service() -> DeckbuildingService:
        active_service = service_state["service"]
        if active_service is None:
            LOGGER.info("Creating DeckbuildingService")
            active_service = build_service()
            service_state["service"] = active_service
        return active_service

    async def with_service(
        handler: Callable[[DeckbuildingService], Awaitable[BaseModel | dict[str, Any]]],
    ) -> BaseModel | dict[str, Any]:
        active_service = await get_service()
        return await handler(active_service)

    @server.custom_route("/", methods=["GET"])
    async def homepage(_: Request) -> Response:
        return HTMLResponse(render_home_page(runtime))

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "service": "archidekt-commander-mcp",
                "transport": runtime.transport,
                "mcp_path": runtime.streamable_http_path,
                "stateless_http": runtime.stateless_http,
                "cache_backend": "redis",
            }
        )

    @server.custom_route("/api/overview", methods=["POST"])
    async def api_overview(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionOverviewRequest,
            lambda payload: with_service(
                lambda active_service: active_service.get_collection_overview(payload.collection)
            ),
        )

    @server.custom_route("/api/search-owned", methods=["POST"])
    async def api_search_owned(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionSearchRequest,
            lambda payload: with_service(
                lambda active_service: active_service.search_owned_cards(
                    payload.collection,
                    _cap_limit(payload.filters, runtime.max_search_results),
                ),
            ),
        )

    @server.custom_route("/api/search-unowned", methods=["POST"])
    async def api_search_unowned(request: Request) -> Response:
        return await _handle_api_request(
            request,
            CollectionSearchRequest,
            lambda payload: with_service(
                lambda active_service: active_service.search_unowned_cards(
                    payload.collection,
                    _cap_limit(payload.filters, runtime.max_search_results),
                ),
            ),
        )

    @server.resource("deckbuilder://collection-contract")
    def collection_contract() -> str:
        return (
            "Every tool call must include `collection` with one of collection_id, "
            "collection_url, or username. The server is stateless, so do not rely on implicit session state."
        )

    @server.resource("deckbuilder://filter-reference")
    def filter_reference() -> str:
        return (
            "Primary filters: exact_name, name_terms_all, oracle_terms_all, oracle_terms_any, "
            "oracle_terms_exclude, type_includes, subtype_includes, supertypes_includes, keywords_any, "
            "color_identity, color_identity_mode, colors, colors_mode, cmc_min, cmc_max, mana_values, "
            "commander_legal, rarities, set_codes, finishes, collection_tags_any, min_quantity, max_quantity, "
            "max_price, price_source, include_tokens, unique_by, sort_by, sort_direction, limit, and page."
        )

    @server.resource("deckbuilder://routing-guide")
    def routing_guide() -> str:
        return (
            "Use search_owned_cards for owned cards and search_unowned_cards for missing cards. "
            "For Commander requests, prefer color_identity over current colors."
        )

    @server.tool(
        description=(
            "Return an overview of the requested public Archidekt collection. "
            "Always requires a `collection` object."
        )
    )
    async def get_collection_overview(collection: CollectionLocator) -> dict:
        active_service = await get_service()
        LOGGER.info("Tool call: get_collection_overview locator=%s", describe_collection_locator(collection))
        overview = await active_service.get_collection_overview(collection)
        return overview.model_dump(mode="json")

    @server.tool(
        description=(
            "Force-refresh the Redis cache entry for the requested collection. "
            "Always requires a `collection` object."
        )
    )
    async def refresh_collection_cache(collection: CollectionLocator) -> dict:
        active_service = await get_service()
        LOGGER.info("Tool call: refresh_collection_cache locator=%s", describe_collection_locator(collection))
        await active_service.get_snapshot(collection, force_refresh=True)
        overview = await active_service.get_collection_overview(collection)
        return overview.model_dump(mode="json")

    @server.tool(
        description=(
            "Search only cards owned in the Archidekt collection provided in the request. "
            "Requires `collection` and accepts `filters`."
        )
    )
    async def search_owned_cards(
        collection: CollectionLocator,
        filters: CardSearchFilters | None = None,
    ) -> dict:
        active_service = await get_service()
        capped_filters = _cap_limit(_coerce_filters(filters), runtime.max_search_results)
        LOGGER.info(
            "Tool call: search_owned_cards locator=%s page=%s limit=%s filters=%s",
            describe_collection_locator(collection),
            capped_filters.page,
            capped_filters.limit,
            capped_filters.model_dump(mode="json"),
        )
        response = await active_service.search_owned_cards(collection, capped_filters)
        return response.model_dump(mode="json")

    @server.tool(
        description=(
            "Search Scryfall for cards not owned in the Archidekt collection provided in the request. "
            "Requires `collection` and accepts `filters`."
        )
    )
    async def search_unowned_cards(
        collection: CollectionLocator,
        filters: CardSearchFilters | None = None,
    ) -> dict:
        active_service = await get_service()
        capped_filters = _cap_limit(_coerce_filters(filters), runtime.max_search_results)
        LOGGER.info(
            "Tool call: search_unowned_cards locator=%s page=%s limit=%s filters=%s",
            describe_collection_locator(collection),
            capped_filters.page,
            capped_filters.limit,
            capped_filters.model_dump(mode="json"),
        )
        response = await active_service.search_unowned_cards(collection, capped_filters)
        return response.model_dump(mode="json")

    return server


app = create_server()
mcp = app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archidekt Commander MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="streamable-http",
        help="MCP transport to use. Default: streamable-http.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for the Web UI / HTTP MCP server.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for the Web UI / HTTP MCP server.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    )
    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=86400,
        help="Redis TTL in seconds for collection snapshots.",
    )
    parser.add_argument(
        "--redis-url",
        default=RuntimeSettings().redis_url,
        help="Redis connection URL for the shared collection cache.",
    )
    parser.add_argument(
        "--redis-key-prefix",
        default=RuntimeSettings().redis_key_prefix,
        help="Prefix used for Redis keys created by this server.",
    )
    parser.add_argument(
        "--http-timeout-seconds",
        type=float,
        default=30.0,
        help="HTTP timeout for Archidekt and Scryfall requests.",
    )
    parser.add_argument(
        "--max-search-results",
        type=int,
        default=50,
        help="Maximum number of results returned per search page.",
    )
    parser.add_argument(
        "--scryfall-max-pages",
        type=int,
        default=6,
        help="Maximum number of Scryfall pages scanned for unowned searches.",
    )
    parser.add_argument(
        "--user-agent",
        default="archidekt-commander-mcp/0.2 (+mailto:replace-me@example.com)",
        help="User-Agent sent to Archidekt and Scryfall.",
    )
    parser.add_argument(
        "--streamable-http-path",
        default="/mcp",
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


async def _handle_api_request(
    request: Request,
    model_cls: type[ModelT],
    handler: Callable[[ModelT], Awaitable[BaseModel | dict[str, Any]]],
) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body.")

    try:
        parsed = model_cls.model_validate(payload)
    except ValidationError as error:
        return _json_error(422, "Invalid payload.", error.errors())

    try:
        result = await handler(parsed)
    except httpx.HTTPStatusError as error:
        return _json_error(502, "Remote HTTP error from Archidekt or Scryfall.", str(error))
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        return _json_error(400, str(error))
    except Exception as error:  # pragma: no cover
        LOGGER.exception("Unhandled API error")
        return _json_error(500, "Internal server error.", str(error))

    if isinstance(result, BaseModel):
        return JSONResponse(result.model_dump(mode="json"))
    return JSONResponse(result)


def _json_error(status_code: int, message: str, details: Any | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(payload, status_code=status_code)


def _coerce_filters(filters: CardSearchFilters | None) -> CardSearchFilters:
    return filters if filters is not None else CardSearchFilters()


def _cap_limit(filters: CardSearchFilters, max_limit: int) -> CardSearchFilters:
    if filters.limit <= max_limit:
        return filters
    return filters.model_copy(update={"limit": max_limit})


def _extract_face_image(card_faces: list[dict[str, Any]]) -> str | None:
    for face in card_faces:
        image_uris = face.get("image_uris") or {}
        if image_uris.get("normal"):
            return image_uris["normal"]
        if image_uris.get("large"):
            return image_uris["large"]
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


if __name__ == "__main__":
    main()
