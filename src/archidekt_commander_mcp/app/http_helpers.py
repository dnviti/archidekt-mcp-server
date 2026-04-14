# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..schemas.search import CardSearchFilters


ModelT = TypeVar("ModelT", bound=BaseModel)
LOGGER = logging.getLogger(__name__)


async def _handle_api_request(
    request: Request,
    model_cls: type[ModelT],
    handler: Callable[[ModelT], Awaitable[BaseModel | dict[str, Any]]],
) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return _json_error(400, "Invalid JSON body.")

    try:
        parsed = model_cls.model_validate(payload)
    except ValidationError as error:
        return _json_error(422, "Invalid payload.", error.errors())

    try:
        result = await handler(parsed)
    except httpx.HTTPStatusError as error:
        return _json_error(502, "Remote HTTP error from Archidekt or Scryfall.", str(error))
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        return _json_error(400, str(error))
    except Exception as error:  # pragma: no cover
        LOGGER.exception("Unhandled API error")
        return _json_error(500, "Internal server error.", str(error))

    if isinstance(result, BaseModel):
        return JSONResponse(result.model_dump(mode="json"))
    return JSONResponse(result)


def _json_error(status_code: int, message: str, details: Any | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(payload, status_code=status_code)


def _coerce_filters(filters: CardSearchFilters | None) -> CardSearchFilters:
    return filters if filters is not None else CardSearchFilters()


def _cap_limit(filters: CardSearchFilters, max_limit: int) -> CardSearchFilters:
    if filters.limit <= max_limit:
        return filters
    return filters.model_copy(update={"limit": max_limit})


def _compact_optional_text(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    compact = " ".join(str(raw_value).strip().split())
    return compact or None
