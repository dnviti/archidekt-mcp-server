# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from typing import TYPE_CHECKING

from ..schemas.accounts import ArchidektAccount, AuthenticatedAccount, CollectionLocator
from .account_identity import account_from_auth_context as account_from_auth_context

if TYPE_CHECKING:
    from .deckbuilding import DeckbuildingService


def describe_collection_locator(collection: CollectionLocator) -> str:
    return collection.display_locator


def describe_account(account: ArchidektAccount | AuthenticatedAccount | None) -> str:
    if account is None:
        return "none"
    if isinstance(account, AuthenticatedAccount):
        if account.username:
            return f"username={account.username}"
        if account.user_id is not None:
            return f"user_id={account.user_id}"
        return "token-provided"
    return account.display_identity


async def _resolve_optional_account(
    service: DeckbuildingService,
    account: AuthenticatedAccount | ArchidektAccount | None,
) -> AuthenticatedAccount | None:
    return await service.account_identity.resolve_optional_account(account)


async def _coerce_account(
    service: DeckbuildingService,
    account: AuthenticatedAccount | ArchidektAccount | None,
) -> AuthenticatedAccount:
    return await service.account_identity.coerce_account(account)


async def _ensure_account_identity(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> AuthenticatedAccount:
    return await service.account_identity.ensure_account_identity(account)
