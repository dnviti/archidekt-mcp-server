# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

import argparse
import logging

from .config import RuntimeSettings
from .services.deckbuilding import LOGGER


VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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
    from .app.factory import create_server

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
