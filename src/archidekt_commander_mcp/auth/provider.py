# pyright: reportMissingImports=false
from __future__ import annotations

import json
import secrets
import time
from typing import Any
from urllib.parse import quote_plus

from redis.asyncio import Redis

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ..schemas.accounts import ArchidektAccount, AuthenticatedAccount
from .records import (
    ArchidektAccessToken,
    ArchidektAuthorizationCode,
    ArchidektRefreshToken,
    AuthSessionRecord,
    PendingAuthorizationRequest,
)


AUTH_SCOPE = "archidekt.account"


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
        login_account: ArchidektAccount | None = None,
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
            **self._login_credential_payload(login_account),
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
            archidekt_login_identifier=authorization_code.archidekt_login_identifier,
            archidekt_login_identifier_type=authorization_code.archidekt_login_identifier_type,
            archidekt_login_password=authorization_code.archidekt_login_password,
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
            archidekt_login_identifier=refresh_token.archidekt_login_identifier,
            archidekt_login_identifier_type=refresh_token.archidekt_login_identifier_type,
            archidekt_login_password=refresh_token.archidekt_login_password,
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

    async def replace_archidekt_session_token(
        self,
        session_id: str,
        *,
        archidekt_token: str,
        archidekt_username: str | None = None,
        archidekt_user_id: int | None = None,
    ) -> AuthSessionRecord | None:
        session_payload = await self._load_json(self._key("session", session_id))
        if session_payload is None:
            return None

        session = AuthSessionRecord.model_validate(session_payload)
        update = {
            "archidekt_token": archidekt_token,
            "archidekt_username": archidekt_username or session.archidekt_username,
            "archidekt_user_id": (
                archidekt_user_id if archidekt_user_id is not None else session.archidekt_user_id
            ),
        }
        session = session.model_copy(update=update)

        access_key = self._key("access-token", session.access_token)
        access_payload = await self._load_json(access_key)
        if access_payload is not None:
            access_token = ArchidektAccessToken.model_validate(access_payload).model_copy(
                update={
                    **update,
                    "archidekt_login_identifier": None,
                    "archidekt_login_identifier_type": None,
                    "archidekt_login_password": None,
                }
            )
            await self._store_json_preserving_ttl(access_key, access_token.model_dump(mode="json"))

        refresh_key = self._key("refresh-token", session.refresh_token)
        refresh_payload = await self._load_json(refresh_key)
        if refresh_payload is not None:
            refresh_token = ArchidektRefreshToken.model_validate(refresh_payload).model_copy(update=update)
            await self._store_json_preserving_ttl(refresh_key, refresh_token.model_dump(mode="json"))

        await self._store_json_preserving_ttl(
            self._key("session", session.session_id),
            session.model_dump(mode="json"),
        )
        return session

    def _build_session(
        self,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        archidekt_token: str,
        archidekt_username: str | None,
        archidekt_user_id: int | None,
        archidekt_login_identifier: str | None = None,
        archidekt_login_identifier_type: str | None = None,
        archidekt_login_password: str | None = None,
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
            archidekt_login_identifier=archidekt_login_identifier,
            archidekt_login_identifier_type=archidekt_login_identifier_type,
            archidekt_login_password=archidekt_login_password,
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
            archidekt_login_identifier=archidekt_login_identifier,
            archidekt_login_identifier_type=archidekt_login_identifier_type,
            archidekt_login_password=archidekt_login_password,
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
        loaded = json.loads(payload)
        if not isinstance(loaded, dict):
            return None
        return {str(name): value for name, value in loaded.items()}

    async def _store_json(self, key: str, payload: dict[str, Any], ex: int | None = None) -> None:
        await self.redis.set(
            key,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            ex=ex,
        )

    async def _store_json_preserving_ttl(self, key: str, payload: dict[str, Any]) -> None:
        ttl_seconds: int | None = None
        ttl = await self.redis.ttl(key)
        if ttl > 0:
            ttl_seconds = ttl
        await self._store_json(key, payload, ex=ttl_seconds)

    def _login_credential_payload(self, account: ArchidektAccount | None) -> dict[str, str | None]:
        if account is None or not account.password:
            return {
                "archidekt_login_identifier": None,
                "archidekt_login_identifier_type": None,
                "archidekt_login_password": None,
            }
        if account.email:
            return {
                "archidekt_login_identifier": account.email,
                "archidekt_login_identifier_type": "email",
                "archidekt_login_password": account.password,
            }
        if account.username:
            return {
                "archidekt_login_identifier": account.username,
                "archidekt_login_identifier_type": "username",
                "archidekt_login_password": account.password,
            }
        return {
            "archidekt_login_identifier": None,
            "archidekt_login_identifier_type": None,
            "archidekt_login_password": None,
        }


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
        auth_session_id=getattr(access_token, "session_id", None),
    )
