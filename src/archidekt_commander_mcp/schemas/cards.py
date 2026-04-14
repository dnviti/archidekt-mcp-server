from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .decks import PersonalDeckCardUsage


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


class ArchidektCardReference(BaseModel):
    card_id: int
    requested_exact_name: str | None = None
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
