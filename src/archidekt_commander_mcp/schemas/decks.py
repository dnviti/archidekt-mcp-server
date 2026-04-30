from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .cards import PersonalDeckCardUsage as PersonalDeckCardUsage
from .search import _normalize_optional_text, _normalize_string_list


_DECK_FORMAT_DESCRIPTION = (
    "Archidekt numeric deck format id. Quantity rules depend on this format: "
    "Commander decks are singleton except for basic lands, while most other formats "
    "normally allow up to 4 copies of a non-basic card and unlimited basic lands."
)

_DECK_MUTATION_QUANTITY_DESCRIPTION = (
    "Exact quantity for this deck card after the mutation. Values greater than 1 are allowed. "
    "On `modify`, a quantity of 0 means remove the card from the deck. "
    "For Commander decks, only basic lands should normally exceed 1 copy. "
    "For non-Commander formats, non-basic cards should normally stay at 4 copies or fewer, "
    "while basic lands may be unlimited."
)


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


class PersonalDecksResponse(BaseModel):
    owner_username: str | None = None
    total_decks: int
    fetched_at: datetime
    notes: list[str] = Field(default_factory=list)
    decks: list[PersonalDeckSummary]


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
    deck_format: int = Field(ge=1, description=_DECK_FORMAT_DESCRIPTION)
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
    deck_format: int | None = Field(default=None, ge=1, description=_DECK_FORMAT_DESCRIPTION)
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
    quantity: int | None = Field(
        default=None,
        ge=0,
        description=_DECK_MUTATION_QUANTITY_DESCRIPTION,
    )
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


class PersonalDeckMutationResponse(BaseModel):
    action: Literal["created", "updated", "deleted", "modified-cards"]
    deck_id: int
    account_username: str | None = None
    affected_count: int | None = None
    processed_at: datetime
    notes: list[str] = Field(default_factory=list)
    deck: PersonalDeckSummary | None = None
    result: dict[str, object] | None = None
