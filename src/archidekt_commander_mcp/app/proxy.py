from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


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
        return app
