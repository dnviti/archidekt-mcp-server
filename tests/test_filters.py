from __future__ import annotations

import unittest
from datetime import UTC, datetime

from archidekt_commander_mcp.clients import build_scryfall_query, card_matches_scryfall_filters
from archidekt_commander_mcp.filtering import normalize_color_symbols, record_matches_filters
from archidekt_commander_mcp.models import CardSearchFilters, CollectionCardRecord


class FilterTests(unittest.TestCase):
    def test_normalize_color_symbols(self) -> None:
        self.assertEqual(normalize_color_symbols(["White", "u", "Black"]), ("W", "U", "B"))

    def test_build_scryfall_query(self) -> None:
        filters = CardSearchFilters(
            type_includes=["Instant"],
            color_identity=["U"],
            color_identity_mode="subset",
            cmc_max=2,
            oracle_terms_any=["draw a card", "counter target spell"],
        )
        query = build_scryfall_query(filters)
        self.assertIn("t:Instant", query)
        self.assertIn("id<=u", query)
        self.assertIn("cmc<=2", query)

    def test_record_matches_filters(self) -> None:
        record = CollectionCardRecord(
            record_id=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            quantity=2,
            foil=False,
            modifier="Normal",
            tags=("draw",),
            condition_code=1,
            language_code=1,
            name="Brainstorm",
            display_name=None,
            oracle_text="Draw three cards, then put two cards from your hand on top of your library in any order.",
            mana_cost="{U}",
            cmc=1,
            colors=("U",),
            color_identity=("U",),
            supertypes=(),
            types=("Instant",),
            subtypes=(),
            type_line="Instant",
            keywords=(),
            rarity="common",
            set_code="ice",
            set_name="Ice Age",
            commander_legal=True,
            oracle_id="brainstorm-oracle",
            card_id=12345,
            printing_id="brainstorm-printing",
            edhrec_rank=123,
            image_uri=None,
            prices={"tcg": 1.0},
        )
        filters = CardSearchFilters(
            type_includes=["Instant"],
            oracle_terms_all=["draw three cards"],
            color_identity=["U"],
            color_identity_mode="subset",
        )
        self.assertTrue(record_matches_filters(record, filters))

    def test_card_matches_scryfall_filters(self) -> None:
        card = {
            "name": "Ponder",
            "oracle_text": "Look at the top three cards of your library, then put them back in any order. You may shuffle. Draw a card.",
            "type_line": "Sorcery",
            "keywords": [],
            "colors": ["U"],
            "color_identity": ["U"],
            "cmc": 1,
            "rarity": "common",
            "set": "lcc",
            "prices": {"usd": "0.30"},
            "legalities": {"commander": "legal"},
        }
        filters = CardSearchFilters(
            type_includes=["Sorcery"],
            oracle_terms_any=["draw a card"],
            color_identity=["U"],
            color_identity_mode="subset",
            max_price=1,
            price_source="usd",
        )
        self.assertTrue(card_matches_scryfall_filters(card, filters))


if __name__ == "__main__":
    unittest.main()
