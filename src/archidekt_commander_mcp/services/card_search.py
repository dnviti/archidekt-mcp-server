from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..filtering import (
    aggregate_owned_results,
    paginate_results,
    record_matches_filters,
    sort_card_results,
)
from ..integrations.scryfall import card_matches_scryfall_filters, scryfall_price_key
from ..schemas.accounts import ArchidektAccount, CollectionLocator
from ..schemas.cards import ArchidektCardSearchResponse, CardResult
from ..schemas.collections import CollectionOverview
from ..schemas.search import ArchidektCardSearchFilters, CardSearchFilters, SearchResponse
from .account_resolution import describe_collection_locator
from .serialization import _extract_face_image, _safe_float, _safe_int

if TYPE_CHECKING:
    from .deckbuilding import DeckbuildingService


class CardSearchWorkflow:
    def __init__(self, service: DeckbuildingService) -> None:
        self._service = service

    async def search_archidekt_cards(
        self,
        filters: ArchidektCardSearchFilters,
    ) -> ArchidektCardSearchResponse:
        results, total_matches, has_more = await self._service.auth_client.search_cards(filters)
        notes = [
            "Use `card_id` values from this response when creating deck entries or collection entries.",
            "For cards already in the user's collection, `search_owned_cards` may already expose matching `archidekt_card_ids`.",
        ]
        if filters.exact_name:
            notes.append(
                "Use `requested_exact_name` on each result to match returned printings back to the requested card names."
            )
            if len(filters.exact_name) > 1:
                notes.append(
                    "This response aggregates multiple exact-name Archidekt lookups into one batch for the model."
                )
                matched_names = {
                    (result.requested_exact_name or "").casefold()
                    for result in results
                    if result.requested_exact_name
                }
                missing_names = [
                    name for name in filters.exact_name if name.casefold() not in matched_names
                ]
                if missing_names:
                    notes.append(
                        "No exact Archidekt match was returned for: " + ", ".join(missing_names) + "."
                    )
        return ArchidektCardSearchResponse(
            page=filters.page,
            returned_count=len(results),
            total_matches=total_matches,
            has_more=has_more,
            notes=notes,
            results=results,
        )

    async def get_collection_overview(
        self,
        collection: CollectionLocator,
        account: ArchidektAccount | None = None,
    ) -> CollectionOverview:
        resolved_account = await self._service._resolve_optional_account(account)
        snapshot = await self._service.get_snapshot(collection, account=resolved_account)
        return CollectionOverview(
            collection_id=snapshot.collection_id,
            owner_id=snapshot.owner_id,
            owner_username=snapshot.owner_username,
            game=snapshot.game,
            total_records=snapshot.total_records,
            unique_oracle_cards=len(snapshot.owned_oracle_ids),
            total_owned_quantity=sum(record.quantity for record in snapshot.records),
            total_pages=snapshot.total_pages,
            page_size=snapshot.page_size,
            source_url=snapshot.source_url,
            fetched_at=snapshot.fetched_at,
        )

    async def search_owned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
        account: ArchidektAccount | None = None,
    ) -> SearchResponse:
        resolved_account = await self._service._resolve_optional_account(account)
        snapshot = await self._service.get_snapshot(collection, account=resolved_account)
        matching_records = [
            record for record in snapshot.records if record_matches_filters(record, filters)
        ]
        results = aggregate_owned_results(
            matching_records,
            filters,
            collection_id=snapshot.collection_id,
            base_url=self._service.settings.normalized_archidekt_base_url,
        )
        notes = [
            f"Collection snapshot fetched at {snapshot.fetched_at.isoformat()}",
            f"Collection locator: {describe_collection_locator(collection)}",
        ]
        if resolved_account is None:
            notes.append(
                "Deterministic search executed against the requested public Archidekt collection."
            )
        else:
            notes.append(
                "Deterministic search executed against the requested authenticated Archidekt collection."
            )
            usage_snapshot = await self._service._get_personal_deck_usage_snapshot(resolved_account)
            self._service._apply_personal_deck_usage(results, usage_snapshot)
            if any(result.personal_deck_count for result in results):
                notes.append(
                    "Some owned cards already appear in personal decks. Ask the user whether those cards may be reused before finalizing a new deck."
                )

        sorted_results = sort_card_results(results, filters)
        paged_results = paginate_results(sorted_results, filters.page, filters.limit)
        await self.ensure_archidekt_card_ids(paged_results, game=collection.game)
        total_matches = len(sorted_results)

        return SearchResponse(
            source="collection",
            ownership_scope="owned",
            applied_filters=filters.model_dump(mode="json"),
            page=filters.page,
            limit=filters.limit,
            returned_count=len(paged_results),
            total_matches=total_matches,
            has_more=filters.page * filters.limit < total_matches,
            notes=notes,
            results=paged_results,
        )

    async def search_unowned_cards(
        self,
        collection: CollectionLocator,
        filters: CardSearchFilters,
        account: ArchidektAccount | None = None,
    ) -> SearchResponse:
        resolved_account = await self._service._resolve_optional_account(account)
        snapshot = await self._service.get_snapshot(collection, account=resolved_account)
        raw_cards, query_used, has_more, notes = await self._service.scryfall_client.search_unowned_cards(
            filters=filters,
            owned_oracle_ids=snapshot.owned_oracle_ids,
            owned_names=snapshot.owned_names,
        )

        filtered_cards = [card for card in raw_cards if card_matches_scryfall_filters(card, filters)]
        mapped_results = [self.map_scryfall_card(card, filters) for card in filtered_cards]
        sorted_results = sort_card_results(mapped_results, filters)
        paged_results = paginate_results(sorted_results, filters.page, filters.limit)
        await self.ensure_archidekt_card_ids(paged_results, game=collection.game)

        return SearchResponse(
            source="scryfall",
            ownership_scope="unowned",
            applied_filters=filters.model_dump(mode="json"),
            query_used=query_used,
            page=filters.page,
            limit=filters.limit,
            returned_count=len(paged_results),
            total_matches=len(sorted_results) if not has_more else None,
            has_more=has_more,
            notes=notes
            + [
                f"Collection locator: {describe_collection_locator(collection)}",
                (
                    "Owned cards were excluded deterministically using the requested authenticated Archidekt collection."
                    if resolved_account is not None
                    else "Owned cards were excluded deterministically using the requested Archidekt collection."
                ),
            ],
            results=paged_results,
        )

    async def ensure_archidekt_card_ids(
        self,
        results: list[CardResult],
        *,
        game: int,
    ) -> None:
        missing_results = [
            result
            for result in results
            if not result.archidekt_card_ids and result.name
        ]
        if not missing_results:
            return

        exact_names = list(dict.fromkeys(result.name for result in missing_results if result.name))
        if not exact_names:
            return

        catalog_results, _, _ = await self._service.auth_client.search_cards(
            ArchidektCardSearchFilters(
                exact_name=exact_names,
                game=game,
                include_tokens=True,
                all_editions=True,
            )
        )
        candidates_by_name: dict[str, list[Any]] = {}
        for candidate in catalog_results:
            lookup_name = (candidate.requested_exact_name or candidate.name).casefold()
            if lookup_name:
                candidates_by_name.setdefault(lookup_name, []).append(candidate)

        for result in missing_results:
            candidates = candidates_by_name.get(result.name.casefold(), [])
            if not candidates:
                continue

            matching_set_candidates = [
                candidate
                for candidate in candidates
                if result.set_code
                and candidate.set_code
                and candidate.set_code.casefold() == result.set_code.casefold()
            ]
            selected = matching_set_candidates or candidates
            result.archidekt_card_ids = sorted(
                {
                    candidate.card_id
                    for candidate in selected
                    if candidate.card_id is not None
                }
            )

    def map_scryfall_card(self, card: dict[str, Any], filters: CardSearchFilters) -> CardResult:
        prices = card.get("prices") or {}
        price_field = scryfall_price_key(filters.price_source)
        unit_price = _safe_float(prices.get(price_field))
        image_uri = (
            ((card.get("image_uris") or {}).get("normal"))
            or ((card.get("image_uris") or {}).get("large"))
            or _extract_face_image(card.get("card_faces") or [])
        )

        return CardResult(
            source="scryfall",
            ownership_scope="unowned",
            name=str(card.get("name") or ""),
            mana_cost=card.get("mana_cost"),
            cmc=_safe_float(card.get("cmc")),
            type_line=card.get("type_line"),
            oracle_text=card.get("oracle_text"),
            colors=[str(value) for value in (card.get("colors") or [])],
            color_identity=[str(value) for value in (card.get("color_identity") or [])],
            keywords=[str(value) for value in (card.get("keywords") or []) if value],
            rarity=card.get("rarity"),
            set_code=card.get("set"),
            set_name=card.get("set_name"),
            finishes=list(card.get("finishes") or []),
            commander_legal=((card.get("legalities") or {}).get("commander") == "legal"),
            edhrec_rank=_safe_int(card.get("edhrec_rank")),
            unit_price=unit_price,
            price_source=price_field,
            oracle_id=card.get("oracle_id"),
            source_uri=card.get("scryfall_uri"),
            image_uri=image_uri,
        )
