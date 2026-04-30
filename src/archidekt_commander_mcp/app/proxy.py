from __future__ import annotations

import ipaddress
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


class TrustedProxyMatcher:
    def __init__(self, trusted_hosts: str) -> None:
        self._trust_all = False
        self._hosts: set[str] = set()
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw_entry in trusted_hosts.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            if entry == "*":
                self._trust_all = True
                continue
            try:
                self._networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                self._hosts.add(entry)

    def is_trusted(self, host: str | None) -> bool:
        if host is None:
            return False
        if self._trust_all or host in self._hosts:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self._networks)


class RealIPHeaderMiddleware:
    def __init__(self, app: ASGIApp, trusted_hosts: str) -> None:
        self.app = app
        self.trusted_hosts = TrustedProxyMatcher(trusted_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        client_addr = scope.get("client")
        client_host = client_addr[0] if client_addr else None
        if self.trusted_hosts.is_trusted(client_host):
            headers = dict(scope["headers"])
            if b"x-forwarded-for" not in headers and b"x-real-ip" in headers:
                real_ip = headers[b"x-real-ip"].decode("latin1").strip()
                if real_ip:
                    scope["client"] = (real_ip, 0)

        await self.app(scope, receive, send)


class ProxyHeaderFastMCP(FastMCP):
    def __init__(
        self,
        *args: Any,
        forwarded_allow_ips: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._forwarded_allow_ips = forwarded_allow_ips

    def streamable_http_app(self) -> Starlette:
        return self._apply_proxy_header_middleware(super().streamable_http_app())

    def sse_app(self, mount_path: str | None = None) -> Starlette:
        return self._apply_proxy_header_middleware(super().sse_app(mount_path))

    def _apply_proxy_header_middleware(self, app: Starlette) -> Starlette:
        app.add_middleware(
            ProxyHeadersMiddleware,
            trusted_hosts=self._forwarded_allow_ips,
        )
        app.add_middleware(
            RealIPHeaderMiddleware,
            trusted_hosts=self._forwarded_allow_ips,
        )
        return app
