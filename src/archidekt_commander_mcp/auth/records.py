from __future__ import annotations

from pydantic import AnyUrl, BaseModel, Field

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken


class PendingAuthorizationRequest(BaseModel):
    request_id: str
    client_id: str
    state: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["archidekt.account"])
    code_challenge: str
    redirect_uri: AnyUrl
    redirect_uri_provided_explicitly: bool
    resource: str | None = None
    expires_at: int


class ArchidektAuthorizationCode(AuthorizationCode):
    archidekt_token: str
    archidekt_username: str | None = None
    archidekt_user_id: int | None = None


class ArchidektRefreshToken(RefreshToken):
    archidekt_token: str
    archidekt_username: str | None = None
    archidekt_user_id: int | None = None
    session_id: str


class ArchidektAccessToken(AccessToken):
    archidekt_token: str
    archidekt_username: str | None = None
    archidekt_user_id: int | None = None
    session_id: str


class AuthSessionRecord(BaseModel):
    session_id: str
    client_id: str
    scopes: list[str] = Field(default_factory=lambda: ["archidekt.account"])
    resource: str | None = None
    access_token: str
    refresh_token: str
    access_expires_at: int | None = None
    refresh_expires_at: int | None = None
    created_at: int
    archidekt_token: str
    archidekt_username: str | None = None
    archidekt_user_id: int | None = None
