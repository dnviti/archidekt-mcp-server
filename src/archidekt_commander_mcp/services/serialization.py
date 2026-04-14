from __future__ import annotations

from datetime import datetime
from typing import Any


def _extract_face_image(card_faces: list[dict[str, Any]]) -> str | None:
    for face in card_faces:
        image_uris = face.get("image_uris") or {}
        normal_image = image_uris.get("normal")
        if isinstance(normal_image, str) and normal_image:
            return normal_image
        large_image = image_uris.get("large")
        if isinstance(large_image, str) and large_image:
            return large_image
    return None


def _parse_datetime(raw_value: Any) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_float(raw_value: Any) -> float | None:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _safe_int(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _extract_deck_id(payload: dict[str, Any]) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("id"),
        (payload.get("deck") or {}).get("id") if isinstance(payload.get("deck"), dict) else None,
        (payload.get("result") or {}).get("id") if isinstance(payload.get("result"), dict) else None,
    ]
    for candidate in candidates:
        parsed = _safe_int(candidate)
        if parsed is not None:
            return parsed
    return None
