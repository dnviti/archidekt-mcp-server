# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import get_access_token

from ..auth.provider import account_from_access_token
from ..schemas.accounts import ArchidektAccount, AuthenticatedAccount, CollectionLocator

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


def account_from_auth_context() -> AuthenticatedAccount | None:
    return account_from_access_token(get_access_token())


async def _resolve_optional_account(
    service: DeckbuildingService,
    account: AuthenticatedAccount | ArchidektAccount | None,
) -> AuthenticatedAccount | None:
    if account is None:
        return account_from_auth_context()
    if isinstance(account, AuthenticatedAccount):
        return account
    return await service.auth_client.resolve_account(account)


async def _coerce_account(
    service: DeckbuildingService,
    account: AuthenticatedAccount | ArchidektAccount | None,
) -> AuthenticatedAccount:
    if account is None:
        context_account = account_from_auth_context()
        if context_account is None:
            raise RuntimeError(
                "Authenticated Archidekt access requires either an `account` payload or an MCP-authenticated session."
            )
        return context_account
    if isinstance(account, AuthenticatedAccount):
        return account
    return await service.auth_client.resolve_account(account)


async def _ensure_account_identity(
    service: DeckbuildingService,
    account: AuthenticatedAccount,
) -> AuthenticatedAccount:
    if account.username and account.user_id is not None:
        return account
    resolved_account, _ = await service._get_authenticated_deck_list(account)
    return resolved_account
