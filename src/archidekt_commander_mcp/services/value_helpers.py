from __future__ import annotations

from typing import Any


def _normalize_lookup_value(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    if not compact:
        return None
    return compact.casefold()


def _compact_optional_text(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    return compact or None


def _coerce_optional_bool(*values: Any) -> bool | None:
    for value in values:
        if value is None:
            continue
        return bool(value)
    return None
