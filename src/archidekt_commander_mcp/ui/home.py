from __future__ import annotations

from importlib.resources import files
import json

from starlette.responses import Response

from ..config import RuntimeSettings


_HOME_TEMPLATE = files("archidekt_commander_mcp.ui").joinpath("templates").joinpath("home.html")
_UI_STATIC = files("archidekt_commander_mcp.ui").joinpath("static")
_ASSET_MEDIA_TYPES = {
    "favicon.ico": "image/x-icon",
    "favicon-16.png": "image/png",
    "favicon-32.png": "image/png",
    "favicon-192.png": "image/png",
    "favicon-512.png": "image/png",
    "logo-generated.png": "image/png",
}


def render_home_page(settings: RuntimeSettings) -> str:
    template = _HOME_TEMPLATE.read_text(encoding="utf-8")
    return template.format(
        auth_enabled=str(settings.auth_enabled).lower(),
        cache_ttl=settings.cache_ttl_seconds,
        mcp_path=settings.streamable_http_path,
        mcp_path_json=json.dumps(settings.streamable_http_path),
        oauth_scope=json.dumps("archidekt.account"),
        stateless_http="yes" if settings.stateless_http else "no",
        transport=settings.transport,
    )


def ui_asset_response(asset_name: str) -> Response:
    media_type = _ASSET_MEDIA_TYPES.get(asset_name)
    if media_type is None:
        return Response("Not found", status_code=404, media_type="text/plain")

    asset = _UI_STATIC.joinpath(asset_name)
    try:
        content = asset.read_bytes()
    except FileNotFoundError:
        return Response("Not found", status_code=404, media_type="text/plain")

    return Response(
        content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
