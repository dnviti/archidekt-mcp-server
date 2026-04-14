from __future__ import annotations

import json
from pathlib import Path

from ..config import RuntimeSettings


_HOME_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "home.html"


def render_home_page(settings: RuntimeSettings) -> str:
    default_filters = json.dumps(
        {
            "type_includes": ["Instant"],
            "limit": 10,
            "page": 1,
        },
        indent=2,
    )

    template = _HOME_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.format(
        auth_enabled=str(settings.auth_enabled).lower(),
        cache_ttl=settings.cache_ttl_seconds,
        default_filters=default_filters,
        mcp_path=settings.streamable_http_path,
        mcp_path_json=json.dumps(settings.streamable_http_path),
        oauth_scope=json.dumps("archidekt.account"),
        stateless_http="yes" if settings.stateless_http else "no",
        transport=settings.transport,
    )
