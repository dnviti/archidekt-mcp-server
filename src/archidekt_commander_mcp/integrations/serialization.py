from __future__ import annotations

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from ..schemas.cards import ArchidektCardReference
from ..schemas.collections import CollectionCardRecord, CollectionSnapshot
from ..schemas.decks import PersonalDeckSummary
from ..schemas.search import ArchidektCardSearchFilters


def build_archidekt_exact_name_filters(
    exact_names: list[str],
    *,
    game: int,
    page: int = 1,
    edition_code: str | None = None,
    include_tokens: bool = False,
    include_digital: bool = False,
    all_editions: bool = False,
) -> ArchidektCardSearchFilters:
    return ArchidektCardSearchFilters(
        exact_name=exact_names,
        game=game,
        page=page,
        edition_code=edition_code,
        include_tokens=include_tokens,
        include_digital=include_digital,
        all_editions=all_editions,
    )


def _serialize_archidekt_card_reference(ref: ArchidektCardReference) -> dict[str, Any]:
    payload = ref.model_dump(mode="json")
    if ref.released_at is not None:
        payload["released_at"] = ref.released_at.isoformat()
    return payload


def _deserialize_archidekt_card_reference(payload: dict[str, Any]) -> ArchidektCardReference:
    raw_released_at = payload.get("released_at")
    if isinstance(raw_released_at, str):
        payload["released_at"] = _parse_datetime(raw_released_at)
    return ArchidektCardReference.model_validate(payload)


def _compact_text(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    return compact or None


def _ensure_mapping(payload: Any, context: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"{context} endpoint returned an invalid payload.")


def _normalize_next_url(raw_url: Any, base_url: str) -> str | None:
    if not raw_url:
        return None
    value = str(raw_url).replace("http://", "https://", 1)
    if value.startswith("/"):
        return base_url + value
    return value


def _dedupe_personal_decks(decks: list[PersonalDeckSummary]) -> list[PersonalDeckSummary]:
    deduped: dict[int, PersonalDeckSummary] = {}
    default_timestamp = datetime(1970, 1, 1, tzinfo=UTC).timestamp()
    for deck in decks:
        deduped[deck.id] = deck
    return sorted(
        deduped.values(),
        key=lambda item: (
            -(item.updated_at.timestamp() if item.updated_at else default_timestamp),
            item.name.casefold(),
        ),
    )


def _parse_datetime(raw_value: Any) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_legality(raw_value: Any) -> bool | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip().casefold()
    if value == "legal":
        return True
    if value in {"not_legal", "banned", "restricted"}:
        return False
    return None


def _safe_int(raw_value: Any) -> int | None:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _safe_float(raw_value: Any) -> float | None:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _serialize_snapshot(snapshot: CollectionSnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload["fetched_at"] = snapshot.fetched_at.isoformat()
    payload["records"] = [_serialize_record(record) for record in snapshot.records]
    return payload


def _serialize_record(record: CollectionCardRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["created_at"] = record.created_at.isoformat() if record.created_at else None
    payload["updated_at"] = record.updated_at.isoformat() if record.updated_at else None
    payload["tags"] = list(record.tags)
    payload["colors"] = list(record.colors)
    payload["color_identity"] = list(record.color_identity)
    payload["supertypes"] = list(record.supertypes)
    payload["types"] = list(record.types)
    payload["subtypes"] = list(record.subtypes)
    payload["keywords"] = list(record.keywords)
    return payload


def _deserialize_snapshot(payload: dict[str, Any]) -> CollectionSnapshot:
    return CollectionSnapshot(
        collection_id=int(payload["collection_id"]),
        owner_id=_safe_int(payload.get("owner_id")),
        owner_username=payload.get("owner_username"),
        game=int(payload["game"]),
        page_size=int(payload["page_size"]),
        total_pages=int(payload["total_pages"]),
        total_records=int(payload["total_records"]),
        fetched_at=_require_datetime(payload.get("fetched_at")),
        source_url=str(payload["source_url"]),
        records=[_deserialize_record(record_payload) for record_payload in payload.get("records", [])],
    )


def _deserialize_record(payload: dict[str, Any]) -> CollectionCardRecord:
    return CollectionCardRecord(
        record_id=int(payload["record_id"]),
        created_at=_parse_datetime(payload.get("created_at")),
        updated_at=_parse_datetime(payload.get("updated_at")),
        quantity=int(payload["quantity"]),
        foil=bool(payload["foil"]),
        modifier=payload.get("modifier"),
        tags=tuple(str(item) for item in payload.get("tags", [])),
        condition_code=_safe_int(payload.get("condition_code")),
        language_code=_safe_int(payload.get("language_code")),
        name=str(payload.get("name") or ""),
        display_name=payload.get("display_name"),
        oracle_text=str(payload.get("oracle_text") or ""),
        mana_cost=payload.get("mana_cost"),
        cmc=_safe_float(payload.get("cmc")),
        colors=tuple(str(item) for item in payload.get("colors", [])),
        color_identity=tuple(str(item) for item in payload.get("color_identity", [])),
        supertypes=tuple(str(item) for item in payload.get("supertypes", [])),
        types=tuple(str(item) for item in payload.get("types", [])),
        subtypes=tuple(str(item) for item in payload.get("subtypes", [])),
        type_line=str(payload.get("type_line") or ""),
        keywords=tuple(str(item) for item in payload.get("keywords", [])),
        rarity=payload.get("rarity"),
        set_code=payload.get("set_code"),
        set_name=payload.get("set_name"),
        commander_legal=payload.get("commander_legal"),
        oracle_id=payload.get("oracle_id"),
        card_id=_safe_int(payload.get("card_id")),
        printing_id=payload.get("printing_id"),
        edhrec_rank=_safe_int(payload.get("edhrec_rank")),
        image_uri=payload.get("image_uri"),
        prices={str(key): _safe_float(value) for key, value in (payload.get("prices") or {}).items()},
    )


def _require_datetime(raw_value: Any) -> datetime:
    parsed = _parse_datetime(raw_value)
    if parsed is None:
        raise ValueError("invalid cached timestamp")
    return parsed


def serialize_collection_snapshot(snapshot: CollectionSnapshot) -> dict[str, Any]:
    return _serialize_snapshot(snapshot)


def deserialize_collection_snapshot(payload: dict[str, Any]) -> CollectionSnapshot:
    return _deserialize_snapshot(payload)
