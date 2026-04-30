from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..filtering import build_type_line
from ..schemas.accounts import ArchidektAccount, ArchidektLoginResponse, AuthenticatedAccount, CollectionLocator
from ..schemas.decks import (
    PersonalDeckCardMutation,
    PersonalDeckCardRecord,
    PersonalDeckCardsResponse,
    PersonalDeckCreateInput,
    PersonalDeckMutationResponse,
    PersonalDeckSummary,
    PersonalDeckUpdateInput,
    PersonalDecksResponse,
)
from .serialization import _extract_deck_id, _parse_datetime, _safe_float, _safe_int
from .value_helpers import _coerce_optional_bool, _compact_optional_text

if TYPE_CHECKING:
    from .deckbuilding import DeckbuildingService


class PersonalDeckWorkflow:
    def __init__(self, service: DeckbuildingService) -> None:
        self._service = service

    async def login_archidekt(self, account: ArchidektAccount | None = None) -> ArchidektLoginResponse:
        resolved_account = await self._service._coerce_account(account)
        resolved_account, decks = await self._service._get_authenticated_deck_list(resolved_account)

        if resolved_account.user_id is None:
            raise RuntimeError("Archidekt authentication succeeded but did not resolve a user id.")

        personal_decks: PersonalDecksResponse | None = None
        notes = [
            "Reuse the returned `account` object in later authenticated tool calls so you do not have to resend the password.",
            "The returned `collection.collection_id` is inferred from Archidekt's current frontend, which links My Collection to `/collection/v2/{user_id}/`.",
        ]
        personal_decks = self.build_personal_decks_response(resolved_account, decks)
        notes.append(
            "The login response includes the current personal deck list so the model can reason about existing decks before proposing or creating another one."
        )
        if account is None:
            notes.append(
                "This login was resolved from the current MCP auth session instead of requiring credentials in the tool payload."
            )

        return ArchidektLoginResponse(
            account=resolved_account,
            collection=CollectionLocator(collection_id=resolved_account.user_id),
            notes=notes,
            personal_decks=personal_decks,
        )

    async def list_personal_decks(self, account: ArchidektAccount | None = None) -> PersonalDecksResponse:
        resolved_account = await self._service._coerce_account(account)
        resolved_account, decks = await self._service._get_authenticated_deck_list(resolved_account)
        return self.build_personal_decks_response(resolved_account, decks)

    def build_personal_decks_response(
        self,
        resolved_account: AuthenticatedAccount,
        decks: list[PersonalDeckSummary],
    ) -> PersonalDecksResponse:
        owner_username = resolved_account.username or (decks[0].owner_username if decks else None)
        private_count = sum(1 for deck in decks if deck.private)
        unlisted_count = sum(1 for deck in decks if deck.unlisted)

        notes = [
            "Authenticated deck listing executed against the current Archidekt account.",
            "Use this together with `search_owned_cards` to spot cards already committed to other decks.",
        ]
        if private_count or unlisted_count:
            notes.append(
                f"Returned {private_count} private deck(s) and {unlisted_count} unlisted deck(s)."
            )

        return PersonalDecksResponse(
            owner_username=owner_username,
            total_decks=len(decks),
            fetched_at=datetime.now(UTC),
            notes=notes,
            decks=decks,
        )

    async def get_personal_deck_cards(
        self,
        deck_id: int,
        include_deleted: bool = False,
        account: AuthenticatedAccount | ArchidektAccount | None = None,
    ) -> PersonalDeckCardsResponse:
        resolved_account = await self._service._coerce_account(account)
        payload = await self._service.auth_client.fetch_deck_cards(
            resolved_account,
            deck_id,
            include_deleted=include_deleted,
        )
        raw_cards = payload.get("cards") or payload.get("results") or []
        mapped_cards = [
            self.map_personal_deck_card_record(item)
            for item in raw_cards
            if isinstance(item, dict) and (include_deleted or not item.get("deletedAt"))
        ]
        return PersonalDeckCardsResponse(
            deck_id=deck_id,
            include_deleted=include_deleted,
            fetched_at=datetime.now(UTC),
            total_cards=len(mapped_cards),
            notes=[
                "Use `deck_relation_id` for future modify/remove deck card operations.",
                "Use `archidekt_card_id` when adding another copy of a known Archidekt card to a deck.",
            ],
            cards=mapped_cards,
        )

    async def create_personal_deck(
        self,
        deck: PersonalDeckCreateInput,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._service._coerce_account(account)
        payload, summary = await self._service.auth_client.create_deck(resolved_account, deck)
        deck_id = (summary.id if summary else None) or _extract_deck_id(payload)
        if deck_id is None:
            raise RuntimeError("Archidekt deck create succeeded but did not return a deck id.")
        await self._service._invalidate_personal_deck_caches(resolved_account)
        return PersonalDeckMutationResponse(
            action="created",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=1,
            processed_at=datetime.now(UTC),
            notes=[
                "Personal deck usage cache invalidated for this account.",
                "Use `modify_personal_deck_cards` next if the deck should be populated immediately.",
            ],
            deck=summary,
            result=payload,
        )

    async def update_personal_deck(
        self,
        deck_id: int,
        deck: PersonalDeckUpdateInput,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._service._coerce_account(account)
        payload, summary = await self._service.auth_client.update_deck(resolved_account, deck_id, deck)
        await self._service._invalidate_personal_deck_caches(resolved_account)
        return PersonalDeckMutationResponse(
            action="updated",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=1,
            processed_at=datetime.now(UTC),
            notes=["Personal deck usage cache invalidated for this account."],
            deck=summary,
            result=payload,
        )

    async def delete_personal_deck(
        self,
        deck_id: int,
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._service._coerce_account(account)
        await self._service.auth_client.delete_deck(resolved_account, deck_id)
        await self._service._invalidate_personal_deck_caches(resolved_account)
        return PersonalDeckMutationResponse(
            action="deleted",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=1,
            processed_at=datetime.now(UTC),
            notes=["Personal deck usage cache invalidated for this account."],
            result={"deleted": True},
        )

    async def modify_personal_deck_cards(
        self,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: ArchidektAccount | None = None,
    ) -> PersonalDeckMutationResponse:
        resolved_account = await self._service._coerce_account(account)
        cards, backfill_notes = await self.backfill_mutation_card_ids(
            deck_id=deck_id,
            cards=cards,
            account=resolved_account,
        )
        payload = await self._service.auth_client.modify_deck_cards(resolved_account, deck_id, cards)
        await self._service._invalidate_personal_deck_caches(resolved_account)
        successful_count = _safe_int(payload.get("successful_count")) if isinstance(payload, dict) else None
        failed_count = _safe_int(payload.get("failed_count")) if isinstance(payload, dict) else None
        affected_count = successful_count if successful_count is not None else len(cards)
        notes = [
            "Personal deck usage cache invalidated for this account.",
            "Re-run `get_personal_deck_cards` if you need fresh `deck_relation_id` values after this patch.",
            *backfill_notes,
        ]
        if failed_count:
            notes.append(
                f"Applied {affected_count} deck card mutation(s); {failed_count} mutation(s) were rejected by Archidekt. Inspect `result.failed_mutations` for the exact payloads and errors."
            )
        return PersonalDeckMutationResponse(
            action="modified-cards",
            deck_id=deck_id,
            account_username=resolved_account.username,
            affected_count=affected_count,
            processed_at=datetime.now(UTC),
            notes=notes,
            result=payload,
        )

    async def backfill_mutation_card_ids(
        self,
        deck_id: int,
        cards: list[PersonalDeckCardMutation],
        account: AuthenticatedAccount,
    ) -> tuple[list[PersonalDeckCardMutation], list[str]]:
        cards_needing_backfill = [
            card
            for card in cards
            if card.deck_relation_id is not None
            and card.card_id is None
            and card.custom_card_id is None
        ]
        if not cards_needing_backfill:
            return cards, []

        deck_state = await self.get_personal_deck_cards(
            deck_id=deck_id,
            include_deleted=True,
            account=account,
        )
        card_ids_by_relation = {
            deck_card.deck_relation_id: deck_card.archidekt_card_id
            for deck_card in deck_state.cards
            if deck_card.deck_relation_id is not None and deck_card.archidekt_card_id is not None
        }
        backfilled_count = 0
        updated_cards: list[PersonalDeckCardMutation] = []
        for card in cards:
            if (
                card.deck_relation_id is not None
                and card.card_id is None
                and card.custom_card_id is None
            ):
                backfilled_card_id = card_ids_by_relation.get(card.deck_relation_id)
                if backfilled_card_id is not None:
                    card = card.model_copy(update={"card_id": backfilled_card_id})
                    backfilled_count += 1
            updated_cards.append(card)

        if not backfilled_count:
            return updated_cards, []
        return (
            updated_cards,
            [
                f"Backfilled Archidekt `card_id` values for {backfilled_count} deck mutation(s) using the current deck state."
            ],
        )

    def map_personal_deck_card_record(self, raw_record: dict[str, Any]) -> PersonalDeckCardRecord:
        modifications = raw_record.get("modifications") or {}
        card_payload = raw_record.get("card") or {}
        oracle_card = card_payload.get("oracleCard") or {}
        supertypes = [str(item) for item in (oracle_card.get("superTypes") or []) if item]
        types = [str(item) for item in (oracle_card.get("types") or []) if item]
        subtypes = [str(item) for item in (oracle_card.get("subTypes") or []) if item]
        modifier = _compact_optional_text(modifications.get("modifier"))
        if modifier is None:
            modifier = _compact_optional_text(raw_record.get("modifier"))
        custom_cmc = _safe_float(modifications.get("customCmc"))
        if custom_cmc is None:
            custom_cmc = _safe_float(raw_record.get("customCmc"))
        label = _compact_optional_text(modifications.get("label"))
        if label is None:
            label = _compact_optional_text(raw_record.get("label"))

        return PersonalDeckCardRecord(
            deck_relation_id=_safe_int(raw_record.get("deckRelationId") or raw_record.get("id")),
            quantity=_safe_int(raw_record.get("quantity")) or 0,
            categories=[
                str(category)
                for category in (raw_record.get("categories") or [])
                if category
            ],
            deleted_at=_parse_datetime(raw_record.get("deletedAt")),
            archidekt_card_id=_safe_int(raw_record.get("cardId") or card_payload.get("id")),
            uid=_compact_optional_text(card_payload.get("uid")),
            oracle_card_id=_safe_int(oracle_card.get("id")),
            oracle_id=_compact_optional_text(oracle_card.get("uid")),
            name=str(
                oracle_card.get("name")
                or card_payload.get("displayName")
                or card_payload.get("name")
                or ""
            ),
            display_name=_compact_optional_text(card_payload.get("displayName")),
            mana_cost=_compact_optional_text(oracle_card.get("manaCost")),
            cmc=_safe_float(oracle_card.get("cmc")),
            type_line=build_type_line(supertypes, types, subtypes),
            oracle_text=_compact_optional_text(oracle_card.get("text")),
            modifier=modifier,
            custom_cmc=custom_cmc,
            companion=_coerce_optional_bool(
                modifications.get("companion") if modifications else None,
                raw_record.get("companion"),
            ),
            flipped_default=_coerce_optional_bool(
                modifications.get("flippedDefault") if modifications else None,
                raw_record.get("flippedDefault"),
            ),
            label=label,
        )
