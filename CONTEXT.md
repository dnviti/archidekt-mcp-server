# Archidekt Commander MCP Context

This context describes the domain language for the Archidekt Commander MCP server: authenticated Archidekt access, collection reads, personal deck workflows, and MCP-facing server composition.

## Language

**Authenticated Access**:
An MCP or direct request path that can use a verified Archidekt token to read private data or perform account writes.
_Avoid_: private mode, logged-in mode

**Archidekt Account Identity**:
The verified Archidekt user identity attached to an authenticated token.
_Avoid_: claimed user, caller identity

**Collection Snapshot**:
A point-in-time view of an Archidekt collection for one locator and game.
_Avoid_: collection cache entry, inventory dump

**Personal Deck List**:
The current set of personal Archidekt decks visible to an authenticated account.
_Avoid_: deck cache, user decks

**Personal Deck Usage**:
An index of which personal decks already contain a card and in what quantity.
_Avoid_: overlap cache, deck annotations

**Authenticated Cache**:
The private cache state derived from authenticated Archidekt data, including collection snapshots, personal deck lists, and personal deck usage.
_Avoid_: Redis cache, private cache

**MCP Server Assembly**:
The wiring that combines runtime settings, transports, auth, routes, resources, and MCP tools into the running server.
_Avoid_: app factory, bootstrap

## Relationships

- **Authenticated Access** requires an **Archidekt Account Identity**.
- An **Archidekt Account Identity** owns one **Personal Deck List**.
- A **Personal Deck List** produces one **Personal Deck Usage** index.
- A **Collection Snapshot** belongs to one collection locator and game.
- The **Authenticated Cache** stores **Collection Snapshot**, **Personal Deck List**, and **Personal Deck Usage** data.
- **MCP Server Assembly** exposes **Authenticated Access** through HTTP routes and MCP tools.

## Example Dialogue

> **Dev:** "After a deck write, should the **Personal Deck Usage** still come from the **Authenticated Cache**?"
> **Domain expert:** "No. A write changes the **Personal Deck List** or deck contents, so the next **Personal Deck Usage** must be refreshed for that **Archidekt Account Identity**."

## Flagged Ambiguities

- "Account" can mean request credentials, an OAuth session, or **Archidekt Account Identity**. Use **Archidekt Account Identity** when the user id or username must be verified by Archidekt.
- "Cache" can mean public Redis collection cache or **Authenticated Cache**. Use **Authenticated Cache** only for private data derived from **Authenticated Access**.
