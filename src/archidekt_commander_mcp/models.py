from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator


ColorSymbol = Literal["W", "U", "B", "R", "G"]
ColorMode = Literal["ignore", "subset", "exact", "overlap"]
UniqueMode = Literal["oracle", "printing"]
SortDirection = Literal["asc", "desc"]
Finish = Literal["normal", "foil"]
PriceSource = Literal["tcg", "ck", "cm", "scg", "mp", "ct", "usd", "eur", "tix"]
SortField = Literal[
    "name",
    "cmc",
    "quantity",
    "unit_price",
    "total_value",
    "updated_at",
    "added_at",
    "edhrec_rank",
]
Rarity = Literal["common", "uncommon", "rare", "mythic", "special", "bonus"]


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    compact = " ".join(str(value).strip().split())
    return compact or None


def _normalize_string_list(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        compact = " ".join(value.strip().split())
        if compact:
            normalized.append(compact)
    return normalized


class CollectionLocator(BaseModel):
    collection_id: int | None = Field(default=None, ge=1)
    collection_url: str | None = None
    username: str | None = None
    game: int = Field(default=1, ge=1, le=3)

    @field_validator("collection_url", "username", mode="before")
    @classmethod
    def normalize_optional_text_fields(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_locator(self) -> "CollectionLocator":
        if not any([self.collection_id, self.collection_url, self.username]):
            raise ValueError(
                "Provide at least one of collection_id, collection_url, or username."
            )

        if self.collection_url and self.collection_id_from_url is None:
            raise ValueError("collection_url does not contain a valid Archidekt collection id.")

        return self

    @computed_field
    @property
    def collection_id_from_url(self) -> int | None:
        if not self.collection_url:
            return None

        match = re.search(r"/collection(?:/v2)?/(\d+)", self.collection_url)
        if not match:
            return None
        return int(match.group(1))

    @computed_field
    @property
    def static_collection_id(self) -> int | None:
        return self.collection_id or self.collection_id_from_url

    @computed_field
    @property
    def cache_key(self) -> str:
        if self.static_collection_id is not None:
            return f"id:{self.static_collection_id}:game:{self.game}"
        return f"user:{(self.username or '').casefold()}:game:{self.game}"

    @computed_field
    @property
    def display_locator(self) -> str:
        if self.static_collection_id is not None:
            return f"collection_id={self.static_collection_id}"
        return f"username={self.username}"


class ArchidektAccount(BaseModel):
    token: str | None = None
    username: str | None = None
    email: str | None = None
    password: str | None = None
    user_id: int | None = Field(default=None, ge=1)

    @field_validator("token", "username", "email", "password", mode="before")
    @classmethod
    def normalize_optional_account_fields(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_account(self) -> "ArchidektAccount":
        if self.token:
            return self

        if self.password and (self.username or self.email):
            return self

        raise ValueError(
            "Provide either token, or password plus username/email for authenticated Archidekt requests."
        )

    @computed_field
    @property
    def display_identity(self) -> str:
        if self.username:
            return f"username={self.username}"
        if self.email:
            return f"email={self.email}"
        if self.user_id is not None:
            return f"user_id={self.user_id}"
        return "token-provided"


class AuthenticatedAccount(BaseModel):
    token: str
    username: str | None = None
    user_id: int | None = Field(default=None, ge=1)


class CardSearchFilters(BaseModel):
    exact_name: list[str] = Field(default_factory=list)
    name_terms_all: list[str] = Field(default_factory=list)
    oracle_terms_all: list[str] = Field(default_factory=list)
    oracle_terms_any: list[str] = Field(default_factory=list)
    oracle_terms_exclude: list[str] = Field(default_factory=list)
    type_includes: list[str] = Field(default_factory=list)
    type_excludes: list[str] = Field(default_factory=list)
    subtype_includes: list[str] = Field(default_factory=list)
    subtype_excludes: list[str] = Field(default_factory=list)
    supertypes_includes: list[str] = Field(default_factory=list)
    supertypes_excludes: list[str] = Field(default_factory=list)
    keywords_any: list[str] = Field(default_factory=list)
    colors: list[ColorSymbol] = Field(default_factory=list)
    colors_mode: ColorMode = Field(default="ignore")
    color_identity: list[ColorSymbol] = Field(default_factory=list)
    color_identity_mode: ColorMode = Field(default="ignore")
    cmc_min: float | None = Field(default=None, ge=0)
    cmc_max: float | None = Field(default=None, ge=0)
    mana_values: list[int] = Field(default_factory=list)
    commander_legal: bool | None = None
    rarities: list[Rarity] = Field(default_factory=list)
    set_codes: list[str] = Field(default_factory=list)
    finishes: list[Finish] = Field(default_factory=list)
    collection_tags_any: list[str] = Field(default_factory=list)
    min_quantity: int | None = Field(default=None, ge=1)
    max_quantity: int | None = Field(default=None, ge=1)
    max_price: float | None = Field(default=None, ge=0)
    price_source: PriceSource = Field(default="tcg")
    include_tokens: bool = False
    unique_by: UniqueMode = Field(default="oracle")
    sort_by: SortField = Field(default="name")
    sort_direction: SortDirection = Field(default="asc")
    limit: int = Field(default=25, ge=1, le=100)
    page: int = Field(default=1, ge=1, le=1000)

    @field_validator(
        "exact_name",
        "name_terms_all",
        "oracle_terms_all",
        "oracle_terms_any",
        "oracle_terms_exclude",
        "type_includes",
        "type_excludes",
        "subtype_includes",
        "subtype_excludes",
        "supertypes_includes",
        "supertypes_excludes",
        "keywords_any",
        "collection_tags_any",
        mode="before",
    )
    @classmethod
    def normalize_text_lists(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return _normalize_string_list([value])
        if isinstance(value, list):
            return _normalize_string_list([str(item) for item in value])
        return value

    @field_validator("set_codes", mode="before")
    @classmethod
    def normalize_set_codes(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip().lower()]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return value

    @field_validator("colors", "color_identity", mode="before")
    @classmethod
    def normalize_colors(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            return [str(item).strip().upper() for item in value if str(item).strip()]
        return value

    @field_validator("rarities", mode="before")
    @classmethod
    def normalize_rarities(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip().lower()]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return value


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


class CardResult(BaseModel):
    source: Literal["collection", "scryfall"]
    ownership_scope: Literal["owned", "unowned"]
    name: str
    quantity: int | None = None
    mana_cost: str | None = None
    cmc: float | None = None
    type_line: str | None = None
    oracle_text: str | None = None
    colors: list[str] = Field(default_factory=list)
    color_identity: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    rarity: str | None = None
    set_code: str | None = None
    set_name: str | None = None
    finishes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    commander_legal: bool | None = None
    edhrec_rank: int | None = None
    unit_price: float | None = None
    total_value: float | None = None
    price_source: str | None = None
    added_at: datetime | None = None
    updated_at: datetime | None = None
    oracle_id: str | None = None
    source_uri: str | None = None
    image_uri: str | None = None
    archidekt_card_ids: list[int] = Field(default_factory=list)
    archidekt_record_ids: list[int] = Field(default_factory=list)
    personal_deck_count: int | None = None
    personal_deck_total_quantity: int | None = None
    personal_deck_usage: list["PersonalDeckCardUsage"] = Field(default_factory=list)


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


class CollectionOverviewRequest(BaseModel):
    collection: CollectionLocator
    account: ArchidektAccount | None = None


class CollectionSearchRequest(BaseModel):
    collection: CollectionLocator
    filters: CardSearchFilters = Field(default_factory=CardSearchFilters)
    account: ArchidektAccount | None = None


class ArchidektCardSearchFilters(BaseModel):
    query: str | None = None
    exact_name: str | None = None
    edition_code: str | None = None
    game: int = Field(default=1, ge=1, le=3)
    include_tokens: bool = False
    include_digital: bool = False
    all_editions: bool = False
    page: int = Field(default=1, ge=1, le=1000)

    @field_validator("query", "exact_name", "edition_code", mode="before")
    @classmethod
    def normalize_optional_search_terms(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_lookup(self) -> "ArchidektCardSearchFilters":
        if not self.query and not self.exact_name:
            raise ValueError("Provide either `query` or `exact_name` for Archidekt card lookup.")
        return self


class ArchidektCardSearchRequest(BaseModel):
    filters: ArchidektCardSearchFilters


class ArchidektLoginRequest(BaseModel):
    account: ArchidektAccount | None = None


class PersonalDecksRequest(BaseModel):
    account: ArchidektAccount | None = None


class PersonalDeckCardsRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)
    include_deleted: bool = False


class PersonalDeckSummary(BaseModel):
    id: int
    name: str
    size: int | None = None
    deck_format: int | None = None
    edh_bracket: int | None = None
    private: bool = False
    unlisted: bool = False
    theorycrafted: bool = False
    game: int | None = None
    tags: list[str] = Field(default_factory=list)
    parent_folder_id: int | None = None
    has_primer: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    featured: str | None = None
    custom_featured: str | None = None
    owner_id: int | None = None
    owner_username: str | None = None
    colors: dict[str, int] = Field(default_factory=dict)


class PersonalDeckCardUsage(BaseModel):
    deck_id: int
    deck_name: str
    quantity: int
    categories: list[str] = Field(default_factory=list)
    private: bool = False
    unlisted: bool = False
    theorycrafted: bool = False
    updated_at: datetime | None = None


class PersonalDecksResponse(BaseModel):
    owner_username: str | None = None
    total_decks: int
    fetched_at: datetime
    notes: list[str] = Field(default_factory=list)
    decks: list[PersonalDeckSummary]


class ArchidektCardReference(BaseModel):
    card_id: int
    uid: str | None = None
    oracle_card_id: int | None = None
    oracle_id: str | None = None
    name: str
    display_name: str | None = None
    mana_cost: str | None = None
    cmc: float | None = None
    oracle_text: str | None = None
    colors: list[str] = Field(default_factory=list)
    color_identity: list[str] = Field(default_factory=list)
    supertypes: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    subtypes: list[str] = Field(default_factory=list)
    set_code: str | None = None
    set_name: str | None = None
    rarity: str | None = None
    released_at: datetime | None = None
    prices: dict[str, float | None] = Field(default_factory=dict)
    owned: int | None = None
    default_category: str | None = None


class ArchidektCardSearchResponse(BaseModel):
    page: int
    returned_count: int
    total_matches: int | None = None
    has_more: bool | None = None
    notes: list[str] = Field(default_factory=list)
    results: list[ArchidektCardReference]


class PersonalDeckCardRecord(BaseModel):
    deck_relation_id: int | None = None
    quantity: int = 0
    categories: list[str] = Field(default_factory=list)
    deleted_at: datetime | None = None
    archidekt_card_id: int | None = None
    uid: str | None = None
    oracle_card_id: int | None = None
    oracle_id: str | None = None
    name: str
    display_name: str | None = None
    mana_cost: str | None = None
    cmc: float | None = None
    type_line: str | None = None
    oracle_text: str | None = None
    modifier: str | None = None
    custom_cmc: float | None = None
    companion: bool | None = None
    flipped_default: bool | None = None
    label: str | None = None


class PersonalDeckCardsResponse(BaseModel):
    deck_id: int
    include_deleted: bool = False
    fetched_at: datetime
    total_cards: int
    notes: list[str] = Field(default_factory=list)
    cards: list[PersonalDeckCardRecord]


class PersonalDeckCreateInput(BaseModel):
    name: str
    deck_format: int = Field(ge=1)
    edh_bracket: int | None = Field(default=None, ge=1, le=5)
    description: str | None = None
    featured: str | None = None
    playmat: str | None = None
    copy_id: int | None = Field(default=None, ge=1)
    private: bool = False
    unlisted: bool = False
    theorycrafted: bool = False
    game: int = Field(default=1, ge=1, le=3)
    parent_folder_id: int | None = Field(default=None, ge=1)
    card_package: dict[str, object] | list[object] | None = None
    extras: dict[str, object] | list[object] | None = None

    @field_validator("name", "description", "featured", "playmat", mode="before")
    @classmethod
    def normalize_required_deck_text(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_name(self) -> "PersonalDeckCreateInput":
        if not self.name:
            raise ValueError("Deck name may not be empty.")
        return self


class PersonalDeckUpdateInput(BaseModel):
    name: str | None = None
    deck_format: int | None = Field(default=None, ge=1)
    edh_bracket: int | None = Field(default=None, ge=1, le=5)
    description: str | None = None
    featured: str | None = None
    playmat: str | None = None
    copy_id: int | None = Field(default=None, ge=1)
    private: bool | None = None
    unlisted: bool | None = None
    theorycrafted: bool | None = None
    game: int | None = Field(default=None, ge=1, le=3)
    parent_folder_id: int | None = Field(default=None, ge=1)
    card_package: dict[str, object] | list[object] | None = None
    extras: dict[str, object] | list[object] | None = None

    @field_validator("name", "description", "featured", "playmat", mode="before")
    @classmethod
    def normalize_optional_deck_text(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_update_payload(self) -> "PersonalDeckUpdateInput":
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("Provide at least one deck field to update.")
        return self


class PersonalDeckCardModifications(BaseModel):
    quantity: int | None = Field(default=None, ge=0)
    modifier: str | None = None
    custom_cmc: float | None = Field(default=None, ge=0)
    companion: bool | None = None
    flipped_default: bool | None = None
    label: str | None = None

    @field_validator("modifier", "label", mode="before")
    @classmethod
    def normalize_optional_modification_text(cls, value: object) -> str | None:
        return _normalize_optional_text(value)


class PersonalDeckCardMutation(BaseModel):
    action: Literal["add", "modify", "remove"]
    card_id: int | None = Field(default=None, ge=1)
    custom_card_id: int | None = Field(default=None, ge=1)
    deck_relation_id: int | None = Field(default=None, ge=1)
    categories: list[str] = Field(default_factory=list)
    patch_id: str | None = None
    modifications: PersonalDeckCardModifications = Field(default_factory=PersonalDeckCardModifications)

    @field_validator("categories", mode="before")
    @classmethod
    def normalize_categories(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return _normalize_string_list([value])
        if isinstance(value, list):
            return _normalize_string_list([str(item) for item in value])
        return value

    @field_validator("patch_id", mode="before")
    @classmethod
    def normalize_patch_id(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_mutation(self) -> "PersonalDeckCardMutation":
        has_card_ref = self.card_id is not None or self.custom_card_id is not None
        if self.action == "add" and not has_card_ref:
            raise ValueError("`add` deck card mutations require `card_id` or `custom_card_id`.")
        if self.action in {"modify", "remove"} and self.deck_relation_id is None:
            raise ValueError("`modify` and `remove` deck card mutations require `deck_relation_id`.")
        if self.action == "modify":
            has_changes = bool(self.categories) or any(
                value is not None for value in self.modifications.model_dump().values()
            )
            if not has_changes:
                raise ValueError("`modify` deck card mutations require categories or modifications.")
        return self


class PersonalDeckCreateRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck: PersonalDeckCreateInput


class PersonalDeckUpdateRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)
    deck: PersonalDeckUpdateInput


class PersonalDeckDeleteRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)


class PersonalDeckCardsMutationRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)
    cards: list[PersonalDeckCardMutation] = Field(min_length=1)


class PersonalDeckMutationResponse(BaseModel):
    action: Literal["created", "updated", "deleted", "modified-cards"]
    deck_id: int
    account_username: str | None = None
    affected_count: int | None = None
    processed_at: datetime
    notes: list[str] = Field(default_factory=list)
    deck: PersonalDeckSummary | None = None
    result: dict[str, object] | None = None


class CollectionCardUpsert(BaseModel):
    record_id: int | None = Field(default=None, ge=1)
    card_id: int = Field(ge=1)
    quantity: int = Field(ge=1)
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


class CollectionUpsertRequest(BaseModel):
    account: ArchidektAccount | None = None
    entries: list[CollectionCardUpsert] = Field(min_length=1)


class CollectionCardUpsertResult(BaseModel):
    operation: Literal["created", "updated"]
    record_id: int | None = None
    card_id: int
    game: int
    result: dict[str, object] = Field(default_factory=dict)


class CollectionMutationResponse(BaseModel):
    action: Literal["upsert"]
    account_username: str | None = None
    affected_count: int
    processed_at: datetime
    notes: list[str] = Field(default_factory=list)
    results: list[CollectionCardUpsertResult] = Field(default_factory=list)


class ArchidektLoginResponse(BaseModel):
    account: AuthenticatedAccount
    collection: CollectionLocator
    notes: list[str] = Field(default_factory=list)
    personal_decks: PersonalDecksResponse | None = None


CardResult.model_rebuild()


class SearchResponse(BaseModel):
    source: Literal["collection", "scryfall"]
    ownership_scope: Literal["owned", "unowned"]
    applied_filters: dict
    query_used: str | None = None
    page: int
    limit: int
    returned_count: int
    total_matches: int | None = None
    has_more: bool | None = None
    notes: list[str] = Field(default_factory=list)
    results: list[CardResult]
