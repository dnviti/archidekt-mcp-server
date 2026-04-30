from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .cards import CardResult


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
    "rarity",
]
Rarity = Literal["common", "uncommon", "rare", "mythic", "special", "bonus"]

_SORT_FIELD_ALIASES: dict[str, tuple[str, str | None]] = {
    "name": ("name", None),
    "alphabetical": ("name", None),
    "cmc": ("cmc", None),
    "mana_value": ("cmc", None),
    "mv": ("cmc", None),
    "quantity": ("quantity", None),
    "qty": ("quantity", None),
    "price": ("unit_price", None),
    "unit_price": ("unit_price", None),
    "card_price": ("unit_price", None),
    "value": ("total_value", None),
    "total_value": ("total_value", None),
    "updated": ("updated_at", None),
    "updated_at": ("updated_at", None),
    "modified": ("updated_at", None),
    "modified_at": ("updated_at", None),
    "added": ("added_at", None),
    "added_at": ("added_at", None),
    "latest": ("added_at", "desc"),
    "newest": ("added_at", "desc"),
    "recent": ("added_at", "desc"),
    "oldest": ("added_at", "asc"),
    "edhrec": ("edhrec_rank", None),
    "edhrec_rank": ("edhrec_rank", None),
    "rarity": ("rarity", None),
}

_SORT_DIRECTION_ALIASES = {
    "asc": "asc",
    "ascending": "asc",
    "desc": "desc",
    "descending": "desc",
}


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


def _normalize_sort_token(value: object) -> str | None:
    compact = _normalize_optional_text(value)
    if compact is None:
        return None
    return compact.casefold().replace("-", "_").replace(" ", "_")


def _normalize_sort_field_and_direction(
    sort_by: object,
    sort_direction: object,
) -> tuple[str | object, str | object]:
    normalized_direction = _SORT_DIRECTION_ALIASES.get(
        _normalize_sort_token(sort_direction) or "",
        sort_direction,
    )
    normalized_sort_by = _normalize_sort_token(sort_by)
    if normalized_sort_by is None:
        return sort_by, normalized_direction

    implied_direction: str | None = None
    for suffix, direction in (
        ("_descending", "desc"),
        ("_ascending", "asc"),
        ("_desc", "desc"),
        ("_asc", "asc"),
    ):
        if normalized_sort_by.endswith(suffix):
            normalized_sort_by = normalized_sort_by[: -len(suffix)]
            implied_direction = direction
            break

    alias = _SORT_FIELD_ALIASES.get(normalized_sort_by)
    if alias is None:
        return normalized_sort_by, normalized_direction

    canonical_field, alias_direction = alias
    if normalized_direction in {"asc", "desc"}:
        return canonical_field, normalized_direction
    return canonical_field, implied_direction or alias_direction or normalized_direction


def _extract_sort_alias(
    payload: dict[str, object],
) -> tuple[object, object]:
    raw_sort = payload.get("sort")
    if isinstance(raw_sort, dict):
        return raw_sort.get("by"), raw_sort.get("direction")
    if raw_sort is not None:
        return raw_sort, payload.get("direction")

    raw_order = payload.get("order")
    if isinstance(raw_order, dict):
        return raw_order.get("by"), raw_order.get("direction")
    if raw_order is not None:
        return raw_order, payload.get("direction")

    raw_order_by = payload.get("order_by")
    if raw_order_by is not None:
        return raw_order_by, payload.get("direction")

    return None, None


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

    @model_validator(mode="before")
    @classmethod
    def normalize_sorting(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        alias_sort_by, alias_sort_direction = _extract_sort_alias(normalized)
        had_sort_by = "sort_by" in normalized
        had_sort_direction = "sort_direction" in normalized
        sort_by, sort_direction = _normalize_sort_field_and_direction(
            normalized.get("sort_by", alias_sort_by),
            normalized.get("sort_direction", alias_sort_direction),
        )
        if sort_by is None and had_sort_by:
            normalized.pop("sort_by", None)
        elif sort_by is not None:
            normalized["sort_by"] = sort_by

        if sort_direction is None and had_sort_direction:
            normalized.pop("sort_direction", None)
        elif sort_direction is not None:
            normalized["sort_direction"] = sort_direction
        normalized.pop("sort", None)
        normalized.pop("order", None)
        normalized.pop("order_by", None)
        normalized.pop("direction", None)
        return normalized

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


class ArchidektCardSearchFilters(BaseModel):
    query: str | None = None
    exact_name: list[str] = Field(default_factory=list)
    edition_code: str | None = None
    game: int = Field(default=1, ge=1, le=3)
    include_tokens: bool = False
    include_digital: bool = False
    all_editions: bool = False
    page: int = Field(default=1, ge=1, le=1000)

    @field_validator("query", "edition_code", mode="before")
    @classmethod
    def normalize_optional_search_terms(cls, value: object) -> str | None:
        return _normalize_optional_text(value)

    @field_validator("exact_name", mode="before")
    @classmethod
    def normalize_exact_names(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return _normalize_string_list([value])
        if isinstance(value, list):
            return _normalize_string_list([str(item) for item in value])
        return value

    @model_validator(mode="after")
    def validate_lookup(self) -> "ArchidektCardSearchFilters":
        if not self.query and not self.exact_name:
            raise ValueError("Provide either `query` or `exact_name` for Archidekt card lookup.")
        return self


class ArchidektCardSearchRequest(BaseModel):
    filters: ArchidektCardSearchFilters


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
