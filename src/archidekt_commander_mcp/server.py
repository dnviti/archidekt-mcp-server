# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from pathlib import Path

if __package__ in {None, ""}:
    import sys

    package_root = Path(__file__).resolve().parents[1]
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

    from archidekt_commander_mcp.app.factory import create_server
    from archidekt_commander_mcp.runtime_cli import (
        build_arg_parser,
        build_runtime_settings_from_args,
        configure_logging,
        main,
    )
    from archidekt_commander_mcp.services.account_resolution import (
        describe_account,
        describe_collection_locator,
    )
    from archidekt_commander_mcp.services.deck_usage import PersonalDeckUsageSnapshot
    from archidekt_commander_mcp.services.deckbuilding import DeckbuildingService, LOGGER
else:
    from .app.factory import create_server
    from .runtime_cli import (
        build_arg_parser,
        build_runtime_settings_from_args,
        configure_logging,
        main,
    )
    from .services.account_resolution import (
        describe_account,
        describe_collection_locator,
    )
    from .services.deck_usage import PersonalDeckUsageSnapshot
    from .services.deckbuilding import DeckbuildingService, LOGGER


app = create_server()
mcp = app


__all__ = [
    "DeckbuildingService",
    "LOGGER",
    "PersonalDeckUsageSnapshot",
    "build_arg_parser",
    "build_runtime_settings_from_args",
    "configure_logging",
    "describe_account",
    "describe_collection_locator",
    "main",
    "create_server",
    "app",
    "mcp",
]


if __name__ == "__main__":
    main()
