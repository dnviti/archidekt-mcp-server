from __future__ import annotations

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from ..filtering import build_type_line, normalize_color_symbols
from ..schemas.accounts import CollectionLocator
from ..schemas.collections import CollectionCardRecord, CollectionSnapshot
from .http_base import _ArchidektHttpClientBase, _auth_headers
from .serialization import _normalize_legality, _parse_datetime, _safe_float, _safe_int


NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(?P<payload>.*?)</script>',
    re.DOTALL,
)
COLLECTION_LINK_PATTERN = re.compile(r"/collection/v2/(\d+)")
LOGGER = logging.getLogger("archidekt_commander_mcp.clients")


class ArchidektPublicCollectionClient(_ArchidektHttpClientBase):
    async def resolve_collection_id(self, collection: CollectionLocator) -> int:
        if collection.static_collection_id is not None:
            LOGGER.info(
                "Using requested Archidekt collection id %s",
                collection.static_collection_id,
            )
            return collection.static_collection_id

        if not collection.username:
            raise RuntimeError("No valid Archidekt locator was provided.")

        LOGGER.info(
            "Resolving public Archidekt collection id from username '%s'",
            collection.username,
        )
        response = await self._request_archidekt(
            "GET",
            f"{self.settings.normalized_archidekt_base_url}/u/{collection.username}",
        )
        response.raise_for_status()

        match = COLLECTION_LINK_PATTERN.search(response.text)
        if not match:
            raise RuntimeError(
                "Unable to resolve a public collection from the Archidekt profile page."
            )
        LOGGER.info("Resolved Archidekt collection id %s from profile", match.group(1))
        return int(match.group(1))

    async def fetch_snapshot(
        self,
        collection: CollectionLocator,
        auth_token: str | None = None,
    ) -> CollectionSnapshot:
        collection_id = await self.resolve_collection_id(collection)
        LOGGER.info(
            "Starting Archidekt collection sync for locator=%s game=%s",
            collection.display_locator,
            collection.game,
        )
        first_page = await self._fetch_collection_page(
            collection_id,
            game=collection.game,
            page=1,
            auth_token=auth_token,
        )
        page_props = first_page["pageProps"]

        total_pages = int(page_props["totalPages"])
        all_records = self._extract_records(first_page)
        LOGGER.info(
            "Fetched Archidekt collection page 1/%s with %s records in page 1",
            total_pages,
            len(all_records),
        )
        for page_number in range(2, total_pages + 1):
            page_payload = await self._fetch_collection_page(
                collection_id,
                game=collection.game,
                page=page_number,
                auth_token=auth_token,
            )
            page_records = self._extract_records(page_payload)
            all_records.extend(page_records)
            LOGGER.info(
                "Fetched Archidekt collection page %s/%s with %s records",
                page_number,
                total_pages,
                len(page_records),
            )

        owner = page_props.get("owner") or {}
        redux = (page_props.get("redux") or {}).get("collectionV2") or {}

        snapshot = CollectionSnapshot(
            collection_id=collection_id,
            owner_id=owner.get("id"),
            owner_username=owner.get("username"),
            game=int(page_props.get("game") or collection.game),
            page_size=int(redux.get("preferredPageSize") or 100),
            total_pages=total_pages,
            total_records=int(page_props["count"]),
            fetched_at=datetime.now(UTC),
            source_url=f"{self.settings.normalized_archidekt_base_url}/collection/v2/{collection_id}",
            records=all_records,
        )
        LOGGER.info(
            "Completed Archidekt collection sync: owner=%s total_records=%s unique_records_loaded=%s",
            snapshot.owner_username,
            snapshot.total_records,
            len(snapshot.records),
        )
        return snapshot

    async def _fetch_collection_page(
        self,
        collection_id: int,
        game: int,
        page: int,
        auth_token: str | None = None,
    ) -> dict[str, Any]:
        LOGGER.info(
            "Requesting Archidekt collection page collection_id=%s page=%s game=%s",
            collection_id,
            page,
            game,
        )
        response = await self._request_archidekt(
            "GET",
            f"{self.settings.normalized_archidekt_base_url}/collection/v2/{collection_id}",
            params={"game": game, "page": page},
            headers=_auth_headers(auth_token),
        )
        response.raise_for_status()

        payload_match = NEXT_DATA_PATTERN.search(response.text)
        if not payload_match:
            raise RuntimeError("The __NEXT_DATA__ payload was not found on the collection page.")

        payload = json.loads(payload_match.group("payload"))
        page_props = (payload.get("props") or {}).get("pageProps") or {}
        if page_props.get("__N_REDIRECT"):
            raise RuntimeError("Archidekt returned a server-side redirect for the collection.")
        return {"pageProps": page_props}

    def _extract_records(self, page_payload: dict[str, Any]) -> list[CollectionCardRecord]:
        page_props = page_payload["pageProps"]
        redux = (page_props.get("redux") or {}).get("collectionV2") or {}
        collection_cards = redux.get("collectionCards") or {}
        ordered_ids = redux.get("serverCollectionData") or []

        results: list[CollectionCardRecord] = []
        for record_id in ordered_ids:
            raw_record = collection_cards.get(str(record_id)) or collection_cards.get(record_id)
            if not raw_record:
                continue

            card = raw_record.get("card") or {}
            prices = card.get("prices") or {}
            legalities = card.get("legalities") or {}
            casting_cost = card.get("castingCost") or []

            supertypes = tuple(str(item) for item in (card.get("superTypes") or []) if item)
            types = tuple(str(item) for item in (card.get("types") or []) if item)
            subtypes = tuple(str(item) for item in (card.get("subTypes") or []) if item)
            mana_cost = (
                "".join(f"{{{symbol}}}" for symbol in casting_cost)
                if isinstance(casting_cost, list)
                else None
            )

            results.append(
                CollectionCardRecord(
                    record_id=int(raw_record["id"]),
                    created_at=_parse_datetime(raw_record.get("createdAt")),
                    updated_at=_parse_datetime(raw_record.get("modifiedAt")),
                    quantity=int(raw_record.get("quantity") or 0),
                    foil=bool(raw_record.get("foil")),
                    modifier=raw_record.get("modifier"),
                    tags=tuple(str(item) for item in (raw_record.get("tags") or []) if item),
                    condition_code=_safe_int(raw_record.get("condition")),
                    language_code=_safe_int(raw_record.get("language")),
                    name=str(card.get("name") or ""),
                    display_name=card.get("displayName"),
                    oracle_text=str(card.get("text") or ""),
                    mana_cost=mana_cost,
                    cmc=_safe_float(card.get("cmc")),
                    colors=normalize_color_symbols(card.get("colors") or []),
                    color_identity=normalize_color_symbols(card.get("colorIdentity") or []),
                    supertypes=supertypes,
                    types=types,
                    subtypes=subtypes,
                    type_line=build_type_line(supertypes, types, subtypes),
                    keywords=tuple(str(item) for item in (card.get("keywords") or []) if item),
                    rarity=(str(card.get("rarity")).casefold() if card.get("rarity") else None),
                    set_code=(str(card.get("setCode")).casefold() if card.get("setCode") else None),
                    set_name=card.get("set"),
                    commander_legal=_normalize_legality(legalities.get("commander")),
                    oracle_id=card.get("oracleCardUid"),
                    card_id=_safe_int(card.get("id")),
                    printing_id=card.get("uid"),
                    edhrec_rank=_safe_int(card.get("edhrecRank")),
                    image_uri=card.get("imgurl") or None,
                    prices={key: _safe_float(value) for key, value in prices.items()},
                )
            )

        return results
