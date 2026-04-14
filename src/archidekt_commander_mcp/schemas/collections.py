from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .search import _normalize_optional_text, _normalize_string_list


_COLLECTION_QUANTITY_DESCRIPTION = (
    "Owned copies to store in the collection. Any positive integer is allowed."
)

_COLLECTION_RECORD_ID_DESCRIPTION = (
    "Existing Archidekt collection record id. Provide this to update an existing row; "
    "omit it to create a new collection record."
)

_COLLECTION_DELETE_RECORD_ID_DESCRIPTION = (
    "Existing Archidekt collection record id to delete. Use `search_owned_cards` and reuse "
    "the returned `archidekt_record_ids` values."
)


@dataclass(slots=True)
class CollectionCardRecord:
    record_id: int
    created_at: datetime | None
    updated_at: datetime | None
    quantity: int
    foil: bool
    modifier: str | None
    tags: tuple[str, ...]
    condition_code: int | None
    language_code: int | None
    name: str
    display_name: str | None
    oracle_text: str
    mana_cost: str | None
    cmc: float | None
    colors: tuple[str, ...]
    color_identity: tuple[str, ...]
    supertypes: tuple[str, ...]
    types: tuple[str, ...]
    subtypes: tuple[str, ...]
    type_line: str
    keywords: tuple[str, ...]
    rarity: str | None
    set_code: str | None
    set_name: str | None
    commander_legal: bool | None
    oracle_id: str | None
    card_id: int | None
    printing_id: str | None
    edhrec_rank: int | None
    image_uri: str | None
    prices: dict[str, float | None]


@dataclass(slots=True)
class CollectionSnapshot:
    collection_id: int
    owner_id: int | None
    owner_username: str | None
    game: int
    page_size: int
    total_pages: int
    total_records: int
    fetched_at: datetime
    source_url: str
    records: list[CollectionCardRecord]

    @property
    def owned_oracle_ids(self) -> set[str]:
        return {record.oracle_id for record in self.records if record.oracle_id}

    @property
    def owned_names(self) -> set[str]:
        return {record.name.casefold() for record in self.records if record.name}


class CollectionOverview(BaseModel):
    collection_id: int
    owner_id: int | None = None
    owner_username: str | None = None
    game: int
    total_records: int
    unique_oracle_cards: int
    total_owned_quantity: int
    total_pages: int
    page_size: int
    source_url: str
    fetched_at: datetime


class CollectionCardUpsert(BaseModel):
    record_id: int | None = Field(
        default=None,
        ge=1,
        description=_COLLECTION_RECORD_ID_DESCRIPTION,
    )
    card_id: int = Field(ge=1)
    quantity: int = Field(ge=1, description=_COLLECTION_QUANTITY_DESCRIPTION)
    game: int = Field(default=1, ge=1, le=3)
    modifier: str | None = None
    language: int | None = Field(default=None, ge=1)
    condition: int | None = Field(default=None, ge=1)
    tags: list[str] = Field(default_factory=list)
    purchase_price: float | None = Field(default=None, ge=0)

    @field_validator("modifier", mode="before")
    @classmethod
    def normalize_optional_collection_modifier(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_collection_tags(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return _normalize_string_list([value])
        if isinstance(value, list):
            return _normalize_string_list([str(item) for item in value])
        return value


class CollectionCardDelete(BaseModel):
    record_id: int = Field(ge=1, description=_COLLECTION_DELETE_RECORD_ID_DESCRIPTION)
    game: int | None = Field(
        default=None,
        ge=1,
        le=3,
        description="Optional Archidekt game id used only to narrow local cache invalidation.",
    )


class CollectionCardUpsertResult(BaseModel):
    operation: Literal["created", "updated", "deleted"]
    record_id: int | None = None
    card_id: int | None = None
    game: int | None = None
    result: dict[str, object] = Field(default_factory=dict)


class CollectionMutationResponse(BaseModel):
    action: Literal["upsert", "delete"]
    account_username: str | None = None
    affected_count: int
    processed_at: datetime
    notes: list[str] = Field(default_factory=list)
    results: list[CollectionCardUpsertResult] = Field(default_factory=list)
