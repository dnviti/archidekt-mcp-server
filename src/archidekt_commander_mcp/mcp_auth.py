from __future__ import annotations

import html
import json
import secrets
import time
from typing import Any
from urllib.parse import quote_plus

from pydantic import AnyUrl, BaseModel, Field
from redis.asyncio import Redis

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .models import AuthenticatedAccount


AUTH_SCOPE = "archidekt.account"


class PendingAuthorizationRequest(BaseModel):
    request_id: str
    client_id: str
    state: str | None = None
    scopes: list[str] = Field(default_factory=lambda: [AUTH_SCOPE])
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
    scopes: list[str] = Field(default_factory=lambda: [AUTH_SCOPE])
    resource: str | None = None
    access_token: str
    refresh_token: str
    access_expires_at: int | None = None
    refresh_expires_at: int | None = None
    created_at: int
    archidekt_token: str
    archidekt_username: str | None = None
    archidekt_user_id: int | None = None


class RedisArchidektOAuthProvider(
    OAuthAuthorizationServerProvider[
        ArchidektAuthorizationCode,
        ArchidektRefreshToken,
        ArchidektAccessToken,
    ]
):
    def __init__(
        self,
        redis: Redis,
        *,
        key_prefix: str,
        issuer_url: str,
        auth_code_ttl_seconds: int = 600,
        access_token_ttl_seconds: int | None = None,
        refresh_token_ttl_seconds: int | None = None,
    ) -> None:
        self.redis = redis
        self.key_prefix = key_prefix
        self.issuer_url = issuer_url.rstrip("/")
        self.auth_code_ttl_seconds = auth_code_ttl_seconds
        self.access_token_ttl_seconds = access_token_ttl_seconds
        self.refresh_token_ttl_seconds = refresh_token_ttl_seconds

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        payload = await self._load_json(self._key("client", client_id))
        if payload is None:
            return None
        return OAuthClientInformationFull.model_validate(payload)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("OAuth client registration requires a client_id.")
        await self._store_json(self._key("client", client_info.client_id), client_info.model_dump(mode="json"))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        scopes = params.scopes or [AUTH_SCOPE]
        pending = PendingAuthorizationRequest(
            request_id=request_id,
            client_id=client.client_id or "",
            state=params.state,
            scopes=scopes,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            expires_at=int(time.time()) + self.auth_code_ttl_seconds,
        )
        await self._store_json(
            self._key("pending", request_id),
            pending.model_dump(mode="json"),
            ex=self.auth_code_ttl_seconds,
        )
        return f"{self.issuer_url}/auth/archidekt-login?request_id={quote_plus(request_id)}"

    async def complete_authorization(
        self,
        request_id: str,
        account: AuthenticatedAccount,
    ) -> str:
        pending = await self.get_pending_request(request_id)
        if pending is None:
            raise RuntimeError("The pending MCP authorization request is missing or expired.")

        if not account.token:
            raise RuntimeError("Authenticated Archidekt login did not return a reusable token.")

        code_value = secrets.token_urlsafe(32)
        code = ArchidektAuthorizationCode(
            code=code_value,
            scopes=pending.scopes,
            expires_at=time.time() + self.auth_code_ttl_seconds,
            client_id=pending.client_id,
            code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource,
            archidekt_token=account.token,
            archidekt_username=account.username,
            archidekt_user_id=account.user_id,
        )
        await self._store_json(
            self._key("auth-code", code_value),
            code.model_dump(mode="json"),
            ex=self.auth_code_ttl_seconds,
        )
        await self.redis.delete(self._key("pending", request_id))

        redirect_url = str(pending.redirect_uri)
        separator = "&" if "?" in redirect_url else "?"
        redirect_url = f"{redirect_url}{separator}code={quote_plus(code_value)}"
        if pending.state:
            redirect_url += f"&state={quote_plus(pending.state)}"
        return redirect_url

    async def get_pending_request(self, request_id: str) -> PendingAuthorizationRequest | None:
        payload = await self._load_json(self._key("pending", request_id))
        if payload is None:
            return None
        pending = PendingAuthorizationRequest.model_validate(payload)
        if pending.expires_at <= int(time.time()):
            await self.redis.delete(self._key("pending", request_id))
            return None
        return pending

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> ArchidektAuthorizationCode | None:
        del client
        payload = await self._load_json(self._key("auth-code", authorization_code))
        if payload is None:
            return None
        return ArchidektAuthorizationCode.model_validate(payload)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: ArchidektAuthorizationCode,
    ) -> OAuthToken:
        session = self._build_session(
            client_id=client.client_id or authorization_code.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            archidekt_token=authorization_code.archidekt_token,
            archidekt_username=authorization_code.archidekt_username,
            archidekt_user_id=authorization_code.archidekt_user_id,
        )
        await self.redis.delete(self._key("auth-code", authorization_code.code))
        await self._store_session(session["access"], session["refresh"], session["record"])
        return OAuthToken(
            access_token=session["access"].token,
            expires_in=self.access_token_ttl_seconds,
            refresh_token=session["refresh"].token,
            scope=" ".join(session["access"].scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> ArchidektRefreshToken | None:
        del client
        payload = await self._load_json(self._key("refresh-token", refresh_token))
        if payload is None:
            return None
        loaded_refresh_token = ArchidektRefreshToken.model_validate(payload)
        _, normalized_refresh_token, _ = await self._migrate_session_to_non_expiring(
            refresh_token=loaded_refresh_token
        )
        return normalized_refresh_token or loaded_refresh_token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: ArchidektRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if self._tokens_never_expire():
            # Keep long-lived MCP sessions stable even if the client refreshes proactively mid-run.
            _, normalized_refresh_token, session = await self._migrate_session_to_non_expiring(
                refresh_token=refresh_token
            )
            active_refresh_token = normalized_refresh_token or refresh_token
            if session is not None:
                access_token = await self.load_access_token(session.access_token)
                if access_token is not None:
                    return OAuthToken(
                        access_token=access_token.token,
                        expires_in=None,
                        refresh_token=active_refresh_token.token,
                        scope=" ".join(scopes),
                    )

        await self.revoke_token(refresh_token)
        rotated = self._build_session(
            client_id=client.client_id or refresh_token.client_id,
            scopes=scopes,
            resource=None,
            archidekt_token=refresh_token.archidekt_token,
            archidekt_username=refresh_token.archidekt_username,
            archidekt_user_id=refresh_token.archidekt_user_id,
        )
        await self._store_session(rotated["access"], rotated["refresh"], rotated["record"])
        return OAuthToken(
            access_token=rotated["access"].token,
            expires_in=self.access_token_ttl_seconds,
            refresh_token=rotated["refresh"].token,
            scope=" ".join(rotated["access"].scopes),
        )

    async def load_access_token(self, token: str) -> ArchidektAccessToken | None:
        payload = await self._load_json(self._key("access-token", token))
        if payload is None:
            return None
        access_token = ArchidektAccessToken.model_validate(payload)
        normalized_access_token, _, _ = await self._migrate_session_to_non_expiring(
            access_token=access_token
        )
        if normalized_access_token is not None:
            access_token = normalized_access_token
        if access_token.expires_at and access_token.expires_at <= int(time.time()):
            await self.revoke_token(access_token)
            return None
        return access_token

    async def revoke_token(
        self,
        token: ArchidektAccessToken | ArchidektRefreshToken,
    ) -> None:
        session_id = getattr(token, "session_id", None)
        if not session_id:
            return
        payload = await self._load_json(self._key("session", session_id))
        if payload is None:
            return
        session = AuthSessionRecord.model_validate(payload)
        await self.redis.delete(
            self._key("access-token", session.access_token),
            self._key("refresh-token", session.refresh_token),
            self._key("session", session.session_id),
        )

    async def load_session(self, session_id: str) -> AuthSessionRecord | None:
        payload = await self._load_json(self._key("session", session_id))
        if payload is None:
            return None
        session = AuthSessionRecord.model_validate(payload)
        _, _, normalized_session = await self._migrate_session_to_non_expiring(session=session)
        return normalized_session or session

    def _build_session(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        archidekt_token: str,
        archidekt_username: str | None,
        archidekt_user_id: int | None,
    ) -> dict[str, Any]:
        session_id = secrets.token_urlsafe(24)
        access_token_value = secrets.token_urlsafe(32)
        refresh_token_value = secrets.token_urlsafe(32)
        issued_at = int(time.time())
        access_expires_at = self._expires_at(issued_at, self.access_token_ttl_seconds)
        refresh_expires_at = self._expires_at(issued_at, self.refresh_token_ttl_seconds)
        access = ArchidektAccessToken(
            token=access_token_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=access_expires_at,
            resource=resource,
            archidekt_token=archidekt_token,
            archidekt_username=archidekt_username,
            archidekt_user_id=archidekt_user_id,
            session_id=session_id,
        )
        refresh = ArchidektRefreshToken(
            token=refresh_token_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=refresh_expires_at,
            archidekt_token=archidekt_token,
            archidekt_username=archidekt_username,
            archidekt_user_id=archidekt_user_id,
            session_id=session_id,
        )
        record = AuthSessionRecord(
            session_id=session_id,
            client_id=client_id,
            scopes=scopes,
            resource=resource,
            access_token=access.token,
            refresh_token=refresh.token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            created_at=issued_at,
            archidekt_token=archidekt_token,
            archidekt_username=archidekt_username,
            archidekt_user_id=archidekt_user_id,
        )
        return {"access": access, "refresh": refresh, "record": record}

    async def _store_session(
        self,
        access: ArchidektAccessToken,
        refresh: ArchidektRefreshToken,
        record: AuthSessionRecord,
    ) -> None:
        await self._store_json(
            self._key("access-token", access.token),
            access.model_dump(mode="json"),
            ex=self.access_token_ttl_seconds,
        )
        await self._store_json(
            self._key("refresh-token", refresh.token),
            refresh.model_dump(mode="json"),
            ex=self.refresh_token_ttl_seconds,
        )
        session_ttl_seconds = self._session_ttl_seconds()
        await self._store_json(
            self._key("session", record.session_id),
            record.model_dump(mode="json"),
            ex=session_ttl_seconds,
        )

    def _expires_at(self, issued_at: int, ttl_seconds: int | None) -> int | None:
        if ttl_seconds is None:
            return None
        return issued_at + ttl_seconds

    def _session_ttl_seconds(self) -> int | None:
        ttl_values = [
            ttl_seconds
            for ttl_seconds in (self.access_token_ttl_seconds, self.refresh_token_ttl_seconds)
            if ttl_seconds is not None
        ]
        if not ttl_values:
            return None
        return max(ttl_values)

    def _tokens_never_expire(self) -> bool:
        return self.access_token_ttl_seconds is None and self.refresh_token_ttl_seconds is None

    async def _migrate_session_to_non_expiring(
        self,
        *,
        access_token: ArchidektAccessToken | None = None,
        refresh_token: ArchidektRefreshToken | None = None,
        session: AuthSessionRecord | None = None,
    ) -> tuple[ArchidektAccessToken | None, ArchidektRefreshToken | None, AuthSessionRecord | None]:
        if not self._tokens_never_expire():
            return access_token, refresh_token, session

        session_id = (
            getattr(access_token, "session_id", None)
            or getattr(refresh_token, "session_id", None)
            or getattr(session, "session_id", None)
        )
        if session is None and session_id:
            session_payload = await self._load_json(self._key("session", session_id))
            if session_payload is not None:
                session = AuthSessionRecord.model_validate(session_payload)

        if access_token is None and session is not None:
            access_payload = await self._load_json(self._key("access-token", session.access_token))
            if access_payload is not None:
                access_token = ArchidektAccessToken.model_validate(access_payload)

        if refresh_token is None and session is not None:
            refresh_payload = await self._load_json(self._key("refresh-token", session.refresh_token))
            if refresh_payload is not None:
                refresh_token = ArchidektRefreshToken.model_validate(refresh_payload)

        if access_token is not None and access_token.expires_at is not None:
            access_token = access_token.model_copy(update={"expires_at": None})
            await self._store_json(
                self._key("access-token", access_token.token),
                access_token.model_dump(mode="json"),
            )

        if refresh_token is not None and refresh_token.expires_at is not None:
            refresh_token = refresh_token.model_copy(update={"expires_at": None})
            await self._store_json(
                self._key("refresh-token", refresh_token.token),
                refresh_token.model_dump(mode="json"),
            )

        if session is not None and (
            session.access_expires_at is not None or session.refresh_expires_at is not None
        ):
            session = session.model_copy(
                update={
                    "access_expires_at": None,
                    "refresh_expires_at": None,
                }
            )
            await self._store_json(
                self._key("session", session.session_id),
                session.model_dump(mode="json"),
            )

        return access_token, refresh_token, session

    def _key(self, namespace: str, value: str) -> str:
        return f"{self.key_prefix}:oauth:{namespace}:{value}"

    async def _load_json(self, key: str) -> dict[str, Any] | None:
        payload = await self.redis.get(key)
        if not payload:
            return None
        return json.loads(payload)

    async def _store_json(self, key: str, payload: dict[str, Any], ex: int | None = None) -> None:
        await self.redis.set(
            key,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            ex=ex,
        )


def render_archidekt_authorize_page(
    *,
    request_id: str,
    error_message: str | None = None,
) -> str:
    error_block = ""
    if error_message:
        error_block = (
            '<p style="margin:0 0 1rem;color:#a11f1f;background:#fff2f2;'
            'border:1px solid #f2c3c3;border-radius:12px;padding:0.85rem 1rem;">'
            f"{html.escape(error_message)}</p>"
        )
    escaped_request_id = html.escape(request_id)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Connect Archidekt</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: linear-gradient(145deg, #f7f3e8 0%, #eef5ee 100%);
      color: #14281d;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
    }}
    .card {{
      width: min(28rem, calc(100vw - 2rem));
      background: rgba(255,255,255,0.94);
      border: 1px solid rgba(20,40,29,0.12);
      border-radius: 22px;
      box-shadow: 0 22px 60px rgba(20,40,29,0.12);
      padding: 1.4rem;
    }}
    h1 {{
      margin: 0 0 0.6rem;
      font-size: 1.8rem;
    }}
    p {{
      line-height: 1.5;
      color: #415247;
    }}
    label {{
      display: grid;
      gap: 0.35rem;
      margin-top: 0.9rem;
      font-weight: 600;
      font-size: 0.95rem;
    }}
    input {{
      width: 100%;
      padding: 0.82rem 0.92rem;
      border-radius: 14px;
      border: 1px solid rgba(20,40,29,0.16);
      background: rgba(255,255,255,0.92);
      box-sizing: border-box;
      font: inherit;
    }}
    button {{
      margin-top: 1rem;
      width: 100%;
      border: 0;
      border-radius: 999px;
      padding: 0.9rem 1rem;
      background: #29524a;
      color: #fffaf0;
      font: inherit;
      cursor: pointer;
    }}
    .note {{
      font-size: 0.88rem;
      color: #5f6e63;
      margin-top: 0.8rem;
    }}
  </style>
</head>
<body>
  <main class="card">
    <h1>Connect Archidekt</h1>
    <p>Sign in with your Archidekt username and password so this MCP app can act on your decks and collection without asking the model to resend your credentials on every tool call.</p>
    {error_block}
    <form method="post">
      <input type="hidden" name="request_id" value="{escaped_request_id}" />
      <label>
        Archidekt Username Or Email
        <input type="text" name="identifier" autocomplete="username" required />
      </label>
      <label>
        Password
        <input type="password" name="password" autocomplete="current-password" required />
      </label>
      <button type="submit">Continue</button>
    </form>
    <p class="note">The password is used only to perform the Archidekt login during this authorization step. The MCP server stores the resulting Archidekt token and OAuth session in Redis, not the raw password.</p>
  </main>
</body>
</html>"""


def account_from_access_token(access_token: AccessToken | None) -> AuthenticatedAccount | None:
    if access_token is None:
        return None
    archidekt_token = getattr(access_token, "archidekt_token", None)
    if not archidekt_token:
        return None
    return AuthenticatedAccount(
        token=str(archidekt_token),
        username=getattr(access_token, "archidekt_username", None),
        user_id=getattr(access_token, "archidekt_user_id", None),
    )
