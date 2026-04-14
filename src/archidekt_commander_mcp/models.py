from __future__ import annotations

# pyright: reportMissingImports=false

from archidekt_commander_mcp.schemas.accounts import (
    ArchidektAccount,
    ArchidektLoginRequest,
    ArchidektLoginResponse,
    AuthenticatedAccount,
    CollectionDeleteRequest,
    CollectionLocator,
    CollectionOverviewRequest,
    CollectionSearchRequest,
    CollectionUpsertRequest,
    PersonalDeckCardsMutationRequest,
    PersonalDeckCardsRequest,
    PersonalDeckCreateRequest,
    PersonalDeckDeleteRequest,
    PersonalDecksRequest,
    PersonalDeckUpdateRequest,
)
from archidekt_commander_mcp.schemas.cards import ArchidektCardReference, ArchidektCardSearchResponse, CardResult
from archidekt_commander_mcp.schemas.collections import (
    _COLLECTION_DELETE_RECORD_ID_DESCRIPTION,
    _COLLECTION_QUANTITY_DESCRIPTION,
    _COLLECTION_RECORD_ID_DESCRIPTION,
    CollectionCardDelete,
    CollectionCardRecord,
    CollectionCardUpsert,
    CollectionCardUpsertResult,
    CollectionMutationResponse,
    CollectionOverview,
    CollectionSnapshot,
)
from archidekt_commander_mcp.schemas.decks import (
    _DECK_FORMAT_DESCRIPTION,
    _DECK_MUTATION_QUANTITY_DESCRIPTION,
    PersonalDeckCardModifications,
    PersonalDeckCardMutation,
    PersonalDeckCardRecord,
    PersonalDeckCardsResponse,
    PersonalDeckCardUsage,
    PersonalDeckCreateInput,
    PersonalDeckMutationResponse,
    PersonalDecksResponse,
    PersonalDeckSummary,
    PersonalDeckUpdateInput,
)
from archidekt_commander_mcp.schemas.search import (
    _extract_sort_alias,
    _normalize_optional_text,
    _normalize_sort_field_and_direction,
    _normalize_sort_token,
    _normalize_string_list,
    _SORT_DIRECTION_ALIASES,
    _SORT_FIELD_ALIASES,
    ArchidektCardSearchFilters,
    ArchidektCardSearchRequest,
    CardSearchFilters,
    ColorMode,
    ColorSymbol,
    Finish,
    PriceSource,
    Rarity,
    SearchResponse,
    SortDirection,
    SortField,
    UniqueMode,
)


CardResult.model_rebuild(_types_namespace={"PersonalDeckCardUsage": PersonalDeckCardUsage})
SearchResponse.model_rebuild(_types_namespace={"CardResult": CardResult})

CollectionSearchRequest.model_rebuild(_types_namespace={"CardSearchFilters": CardSearchFilters})
PersonalDeckCreateRequest.model_rebuild(_types_namespace={"PersonalDeckCreateInput": PersonalDeckCreateInput})
PersonalDeckUpdateRequest.model_rebuild(_types_namespace={"PersonalDeckUpdateInput": PersonalDeckUpdateInput})
PersonalDeckCardsMutationRequest.model_rebuild(
    _types_namespace={"PersonalDeckCardMutation": PersonalDeckCardMutation}
)
CollectionUpsertRequest.model_rebuild(_types_namespace={"CollectionCardUpsert": CollectionCardUpsert})
CollectionDeleteRequest.model_rebuild(_types_namespace={"CollectionCardDelete": CollectionCardDelete})
ArchidektLoginResponse.model_rebuild(_types_namespace={"PersonalDecksResponse": PersonalDecksResponse})


__all__ = [
    "ColorSymbol",
    "ColorMode",
    "UniqueMode",
    "SortDirection",
    "Finish",
    "PriceSource",
    "SortField",
    "Rarity",
    "_SORT_FIELD_ALIASES",
    "_SORT_DIRECTION_ALIASES",
    "_DECK_FORMAT_DESCRIPTION",
    "_DECK_MUTATION_QUANTITY_DESCRIPTION",
    "_COLLECTION_QUANTITY_DESCRIPTION",
    "_COLLECTION_RECORD_ID_DESCRIPTION",
    "_COLLECTION_DELETE_RECORD_ID_DESCRIPTION",
    "_normalize_optional_text",
    "_normalize_string_list",
    "_normalize_sort_token",
    "_normalize_sort_field_and_direction",
    "_extract_sort_alias",
    "CollectionLocator",
    "ArchidektAccount",
    "AuthenticatedAccount",
    "CardSearchFilters",
    "CollectionCardRecord",
    "CollectionSnapshot",
    "CardResult",
    "CollectionOverview",
    "CollectionOverviewRequest",
    "CollectionSearchRequest",
    "ArchidektCardSearchFilters",
    "ArchidektCardSearchRequest",
    "ArchidektLoginRequest",
    "PersonalDecksRequest",
    "PersonalDeckCardsRequest",
    "PersonalDeckSummary",
    "PersonalDeckCardUsage",
    "PersonalDecksResponse",
    "ArchidektCardReference",
    "ArchidektCardSearchResponse",
    "PersonalDeckCardRecord",
    "PersonalDeckCardsResponse",
    "PersonalDeckCreateInput",
    "PersonalDeckUpdateInput",
    "PersonalDeckCardModifications",
    "PersonalDeckCardMutation",
    "PersonalDeckCreateRequest",
    "PersonalDeckUpdateRequest",
    "PersonalDeckDeleteRequest",
    "PersonalDeckCardsMutationRequest",
    "PersonalDeckMutationResponse",
    "CollectionCardUpsert",
    "CollectionUpsertRequest",
    "CollectionCardDelete",
    "CollectionDeleteRequest",
    "CollectionCardUpsertResult",
    "CollectionMutationResponse",
    "ArchidektLoginResponse",
    "SearchResponse",
]
