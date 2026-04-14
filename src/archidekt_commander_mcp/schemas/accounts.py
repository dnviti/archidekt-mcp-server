from __future__ import annotations

import re

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from .collections import CollectionCardDelete, CollectionCardUpsert
from .decks import (
    PersonalDeckCardMutation,
    PersonalDeckCreateInput,
    PersonalDecksResponse,
    PersonalDeckUpdateInput,
)
from .search import CardSearchFilters, _normalize_optional_text


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def collection_id_from_url(self) -> int | None:
        if not self.collection_url:
            return None

        match = re.search(r"/collection(?:/v2)?/(\d+)", self.collection_url)
        if not match:
            return None
        return int(match.group(1))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def static_collection_id(self) -> int | None:
        return self.collection_id or self.collection_id_from_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_key(self) -> str:
        if self.static_collection_id is not None:
            return f"id:{self.static_collection_id}:game:{self.game}"
        return f"user:{(self.username or '').casefold()}:game:{self.game}"

    @computed_field  # type: ignore[prop-decorator]
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

    @computed_field  # type: ignore[prop-decorator]
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


class CollectionOverviewRequest(BaseModel):
    collection: CollectionLocator
    account: ArchidektAccount | None = None


class CollectionSearchRequest(BaseModel):
    collection: CollectionLocator
    filters: CardSearchFilters = Field(default_factory=CardSearchFilters)
    account: ArchidektAccount | None = None


class ArchidektLoginRequest(BaseModel):
    account: ArchidektAccount | None = None


class PersonalDecksRequest(BaseModel):
    account: ArchidektAccount | None = None


class PersonalDeckCardsRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)
    include_deleted: bool = False


class PersonalDeckCreateRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck: "PersonalDeckCreateInput"


class PersonalDeckUpdateRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)
    deck: "PersonalDeckUpdateInput"


class PersonalDeckDeleteRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)


class PersonalDeckCardsMutationRequest(BaseModel):
    account: ArchidektAccount | None = None
    deck_id: int = Field(ge=1)
    cards: list["PersonalDeckCardMutation"] = Field(min_length=1)


class CollectionUpsertRequest(BaseModel):
    account: ArchidektAccount | None = None
    entries: list["CollectionCardUpsert"] = Field(min_length=1)


class CollectionDeleteRequest(BaseModel):
    account: ArchidektAccount | None = None
    entries: list["CollectionCardDelete"] = Field(min_length=1)


class ArchidektLoginResponse(BaseModel):
    account: AuthenticatedAccount
    collection: CollectionLocator
    notes: list[str] = Field(default_factory=list)
    personal_decks: "PersonalDecksResponse | None" = None
