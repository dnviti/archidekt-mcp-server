from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .cards import PersonalDeckCardUsage
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

COLLECTION_EXPORT_FIELDS = (
    "quantity",
    "card__oracleCard__name",
    "modifier",
    "condition",
    "createdAt",
    "language",
    "purchasePrice",
    "tags",
    "card__edition__editionname",
    "card__edition__editioncode",
    "card__multiverseid",
    "card__uid",
    "card__oracle__uid",
    "card__mtgoNormalId",
    "card__collectorNumber",
    "card__color",
    "card__colorIdentity",
    "card__manaCost",
    "card__types",
    "card__subtypes",
    "card__supertypes",
    "card__rarity",
    "card__prices__ck",
    "card__prices__tcg",
    "card__prices__scg",
    "card__prices__mtgo",
    "card__prices__cm",
    "card__prices__mp",
    "card__prices__tcg_land",
    "card__cmc",
)

_COLLECTION_EXPORT_FIELDS_DESCRIPTION = (
    "Archidekt collection export fields. Defaults to the fields used by Archidekt's own "
    "collection data viewer. Common fields include quantity, card__oracleCard__name, "
    "card__edition__editioncode, card__uid, card__oracle__uid, card__collectorNumber, "
    "card__manaCost, card__types, card__rarity, and card__prices__tcg."
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


@dataclass(slots=True)
class CollectionExportDocument:
    collection_id: int
    game: int
    endpoint_url: str
    fields: tuple[str, ...]
    page_size: int
    fetched_pages: int
    total_rows: int
    more_available: bool
    csv_content: str


class CollectionReadOptions(BaseModel):
    fields: list[str] = Field(
        default_factory=lambda: list(COLLECTION_EXPORT_FIELDS),
        min_length=1,
        description=_COLLECTION_EXPORT_FIELDS_DESCRIPTION,
    )
    page_size: int = Field(
        default=2500,
        ge=1,
        le=2500,
        description="Archidekt export page size. Archidekt's client currently uses 2500.",
    )
    max_pages: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description=(
            "Optional page cap. Omit this to fetch the whole collection export. "
            "When set and Archidekt has more pages, the response marks more_available=true."
        ),
    )
    include_csv_content: bool = Field(
        default=False,
        description=(
            "Return the full CSV content in the MCP response. Keep false when exporting to a file "
            "unless the model needs to parse the full CSV directly."
        ),
    )
    preview_rows: int = Field(
        default=25,
        ge=0,
        le=100,
        description="Number of parsed CSV rows to include as a preview in the MCP response.",
    )
    export_to_file: bool = Field(
        default=False,
        description="Write the full CSV export to a local file and return the path.",
    )
    file_path: str | None = Field(
        default=None,
        description=(
            "Optional local CSV output path. Providing this also enables file export. "
            "Relative paths are resolved from the MCP server process working directory."
        ),
    )
    overwrite: bool = Field(
        default=False,
        description="Allow replacing an existing file at file_path.",
    )

    @field_validator("fields", mode="before")
    @classmethod
    def normalize_export_fields(cls, value: object) -> object:
        if value is None:
            return list(COLLECTION_EXPORT_FIELDS)
        if isinstance(value, str):
            return _normalize_string_list([value])
        if isinstance(value, list):
            return _normalize_string_list([str(item) for item in value])
        return value

    @field_validator("file_path", mode="before")
    @classmethod
    def normalize_export_file_path(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_file_export(self) -> "CollectionReadOptions":
        if self.file_path:
            self.export_to_file = True
        return self


class CollectionExportFile(BaseModel):
    path: str
    bytes: int
    content_type: str = "text/csv"


class CollectionReadResponse(BaseModel):
    collection_id: int
    game: int
    endpoint_url: str
    fields: list[str]
    page_size: int
    fetched_pages: int
    total_rows: int
    more_available: bool
    csv_size_bytes: int
    csv_content: str | None = None
    csv_preview: str
    rows_preview: list[dict[str, str]] = Field(default_factory=list)
    file: CollectionExportFile | None = None
    notes: list[str] = Field(default_factory=list)


AvailabilityStatus = Literal[
    "available",
    "insufficient_available_copies",
    "all_copies_used",
    "not_owned",
]


class CollectionAvailabilityCardRequest(BaseModel):
    name: str | None = Field(
        default=None,
        description="Card name to check. Exact owned card names are matched case-insensitively.",
    )
    oracle_id: str | None = Field(
        default=None,
        description="Optional Scryfall/Archidekt oracle id to match the owned card across printings.",
    )
    card_id: int | None = Field(
        default=None,
        ge=1,
        description="Optional Archidekt card id. If owned, availability is expanded to the card's oracle identity.",
    )
    requested_quantity: int = Field(
        default=1,
        ge=1,
        description="Copies the new deck wants to use from the collection.",
    )

    @field_validator("name", "oracle_id", mode="before")
    @classmethod
    def normalize_card_lookup_text(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_lookup(self) -> "CollectionAvailabilityCardRequest":
        if not any([self.name, self.oracle_id, self.card_id]):
            raise ValueError("Provide at least one of name, oracle_id, or card_id.")
        return self


class CollectionAvailabilityOptions(BaseModel):
    collection_only: bool = Field(
        default=True,
        description=(
            "When true, cards without enough free collection copies are marked `must_not_use`."
        ),
    )
    exclude_deck_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Deck ids to ignore in usage totals. Use this when editing an existing deck and its current "
            "copies should remain available to that same deck."
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description="Force-refresh both the collection snapshot and personal deck usage index.",
    )

    @field_validator("exclude_deck_ids", mode="before")
    @classmethod
    def normalize_excluded_deck_ids(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, int):
            return [value]
        return value


class CollectionCardAvailability(BaseModel):
    requested_name: str | None = None
    matched_name: str | None = None
    oracle_id: str | None = None
    requested_quantity: int
    collection_quantity: int
    used_in_decks_quantity: int
    available_quantity: int
    enough_copies: bool
    must_not_use: bool
    status: AvailabilityStatus
    archidekt_card_ids: list[int] = Field(default_factory=list)
    archidekt_record_ids: list[int] = Field(default_factory=list)
    personal_deck_usage: list[PersonalDeckCardUsage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CollectionAvailabilityResponse(BaseModel):
    collection_id: int
    owner_username: str | None = None
    account_username: str | None = None
    collection_only: bool
    checked_at: datetime
    collection_fetched_at: datetime
    usage_fetched_at: datetime
    all_requested_available: bool
    blocked_count: int
    notes: list[str] = Field(default_factory=list)
    results: list[CollectionCardAvailability]


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
