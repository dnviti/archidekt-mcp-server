from __future__ import annotations

# pyright: reportMissingImports=false

from .integrations.authenticated import ArchidektAuthenticatedClient
from .integrations.collection_cache import CollectionCache
from .integrations.http_base import _ArchidektHttpClientBase
from .integrations.public_collection import ArchidektPublicCollectionClient
from .integrations.request_gate import ArchidektRequestGate
from .integrations.scryfall import (
    ScryfallClient,
    _color_query,
    build_scryfall_query,
    card_matches_scryfall_filters,
    map_scryfall_order,
    scryfall_price_key,
)
from .integrations.serialization import (
    _dedupe_personal_decks,
    _parse_datetime,
    _safe_float,
    _safe_int,
    build_archidekt_exact_name_filters,
    deserialize_collection_snapshot,
    serialize_collection_snapshot,
)

__all__ = [
    "ArchidektAuthenticatedClient",
    "ArchidektPublicCollectionClient",
    "ArchidektRequestGate",
    "CollectionCache",
    "ScryfallClient",
    "_ArchidektHttpClientBase",
    "_color_query",
    "_dedupe_personal_decks",
    "_parse_datetime",
    "_safe_float",
    "_safe_int",
    "build_archidekt_exact_name_filters",
    "build_scryfall_query",
    "card_matches_scryfall_filters",
    "deserialize_collection_snapshot",
    "map_scryfall_order",
    "scryfall_price_key",
    "serialize_collection_snapshot",
]
