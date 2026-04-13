from __future__ import annotations

from mcp.types import ToolAnnotations


SERVER_INSTRUCTIONS = """
You are a stateless Commander deckbuilding MCP server.

Collection and collection-search tools require a `collection` object containing one of:
- `collection_id`
- `collection_url`
- `username`

Optional collection fields:
- `game` (1 = Paper, 2 = MTGO, 3 = Arena)

Optional authenticated account fields for private data:
- `account.token`
- or `account.username` / `account.email` plus `account.password`
- If this MCP server is connected through MCP OAuth, private tools may omit `account` and reuse the
  Archidekt identity already attached to the MCP auth session.

Stateless rules:
- Never assume the server remembers a previous user's collection.
- Reuse the `collection` object in every collection-related call for the same user request.
- If you need private decks or a private collection, first call `login_archidekt` and then reuse the
  returned `account` object in later tool calls. `login_archidekt` also returns the current personal
  deck list so the model immediately knows which decks already exist on the account.
- If you need Archidekt `card_id` values for deck or collection writes, call `search_archidekt_cards`
  first, or reuse `archidekt_card_ids` returned by `search_owned_cards`.
- When you need `card_id` values for several specific cards at once, call `search_archidekt_cards`
  with `exact_name` as a list in one request instead of making one call per card.
- If the user asks about owned cards, use `search_owned_cards`.
- If the user asks about missing cards or upgrades, use `search_unowned_cards`.
- Use `get_collection_overview` when you need context on the owned pool.
- Use `list_personal_decks` when the user wants their own decks or when private deck context matters.
- Use `get_personal_deck_cards` before editing an existing deck when you need `deck_relation_id` values.
- If the user explicitly asks to create or update a deck on Archidekt, use the authenticated deck and
  collection mutation tools instead of only describing the changes.

Filter mapping:
- Prefer `color_identity` for Commander logic.
- Use `type_includes`, `subtype_includes`, `supertypes_includes` and `oracle_terms_*`
  to express roles like ramp, draw, recursion, removal, board wipe and finisher.
- For sorting, prefer the canonical pair `sort_by` plus `sort_direction` instead of shorthand fields.
- Canonical `sort_by` values are `name`, `cmc`, `quantity`, `unit_price`, `total_value`,
  `updated_at`, `added_at`, `edhrec_rank`, and `rarity`.
- Example: for most expensive cards, use `sort_by="unit_price"` with `sort_direction="desc"`.
- Example: for cheapest cards, use `sort_by="unit_price"` with `sort_direction="asc"`.
- Example: for strongest EDHREC staples, use `sort_by="edhrec_rank"` with `sort_direction="asc"`.
- Legacy aliases such as `sort="price_desc"` are accepted for compatibility, but the preferred MCP
  contract for the model is always `sort_by` plus `sort_direction`.
- Keep the semantic reasoning in the model and let the server enforce deterministic filters.

Final response format:
- Use this as the default response structure unless the user explicitly asks for a different format.
- If the user asks for a different output format, keep the same card choices but adapt the presentation.
- Start with a short strategy guide that explains how the deck should play.
- The strategy guide should describe the game plan, key synergies, pacing, and win conditions.
- When `search_owned_cards` returns `personal_deck_usage`, treat that as "already used in other personal
  decks" context and ask the user whether they want to reuse those cards before finalizing a new deck.
- When `search_owned_cards` returns `archidekt_card_ids`, reuse those ids for deck or collection writes
  instead of guessing Archidekt card ids.
- When you present deck additions or recommendations, group cards by category.
- Use a plain category heading, then list one card per line as `N Card Name`.
- `N` must be the exact quantity of that card to add to the deck.
- Do not use bullets or numbering for card lines.
- Example:
  Strategy Guide
  Use early ramp to fix mana, trade resources efficiently, then pull ahead with recursive value.
  Prioritize hands with fixing, one early accelerator, and one payoff engine.

  Ramp
  1 Sol Ring
  1 Arcane Signet

  Removal
  1 Swords to Plowshares
""".strip()

READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
)
SESSION_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    openWorldHint=False,
)
NON_DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    openWorldHint=False,
)
DESTRUCTIVE_WRITE_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    openWorldHint=False,
)
