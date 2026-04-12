# archidekt-mcp-server

Stateless MCP server for Commander deckbuilding against Archidekt collections, personal decks, and Scryfall.

The server is designed for LLM-driven workflows:

- optional authenticated Archidekt access through explicit request payloads
- optional MCP OAuth auth for ChatGPT and other remote MCP clients
- no server-side deckbuilding session persistence
- no per-user environment variables
- every request passes the collection locator explicitly
- private deck access can use either an explicit `account` object or the active MCP auth session
- collection snapshots are cached in Redis for 24 hours by default
- authenticated collection snapshots, personal deck overlap data, and optional MCP OAuth state are also cached in Redis by default

## What It Exposes

MCP tools:

- `login_archidekt([account])`
- `list_personal_decks([account])`
- `search_archidekt_cards(filters)`
- `get_personal_deck_cards(deck_id, include_deleted, [account])`
- `create_personal_deck(deck, [account])`
- `update_personal_deck(deck_id, deck, [account])`
- `delete_personal_deck(deck_id, [account])`
- `modify_personal_deck_cards(deck_id, cards, [account])`
- `upsert_collection_entries(entries, [account])`
- `get_collection_overview(collection)`
- `refresh_collection_cache(collection)`
- `search_owned_cards(collection, filters)`
- `search_unowned_cards(collection, filters)`

HTTP routes:

- `/` English Web UI with copy buttons for generated code blocks
- `/health` health check
- `/api/login` stateless HTTP test for `login_archidekt`
- `/api/personal-decks` stateless HTTP test for `list_personal_decks`
- `/api/cards/search` stateless HTTP test for `search_archidekt_cards`
- `/api/personal-deck-cards` stateless HTTP test for `get_personal_deck_cards`
- `/api/personal-decks/create` stateless HTTP test for `create_personal_deck`
- `/api/personal-decks/update` stateless HTTP test for `update_personal_deck`
- `/api/personal-decks/delete` stateless HTTP test for `delete_personal_deck`
- `/api/personal-decks/modify-cards` stateless HTTP test for `modify_personal_deck_cards`
- `/api/collection/upsert` stateless HTTP test for `upsert_collection_entries`
- `/api/overview` stateless HTTP test for `get_collection_overview`
- `/api/search-owned` stateless HTTP test for `search_owned_cards`
- `/api/search-unowned` stateless HTTP test for `search_unowned_cards`
- `/auth/archidekt-login` OAuth authorization page used when MCP auth is enabled
- `/mcp` streamable HTTP MCP endpoint

## Request Model

Every tool call must include `collection` with one of:

- `collection_id`
- `collection_url`
- `username`

Optional fields:

- `game` where `1 = Paper`, `2 = MTGO`, `3 = Arena`

Authenticated requests may also include optional `account` with either:

- `token`
- `username` or `email`, plus `password`

If the MCP client connected through OAuth, private tools may omit `account` and use the active MCP auth session instead.
That same session-scoped identity can be reused for private deck and collection writes too.

Recommended auth flow:

1. If MCP OAuth is enabled, connect the app through ChatGPT and sign in on `/auth/archidekt-login`.
2. Call `login_archidekt()` without an `account` payload, or call `login_archidekt(account)` when you are not using MCP OAuth.
3. Read the `personal_decks` block from the login response so the model immediately knows which decks already exist on the account.
4. Reuse the returned `account` object in later tool calls only when you are not relying on MCP auth.
5. For the logged-in user's collection, reuse the returned `collection.collection_id`.

Example:

```json
{
  "collection": {
    "username": "your_archidekt_username",
    "game": 1
  }
}
```

Authenticated example:

```json
{
  "collection": {
    "collection_id": 123456,
    "game": 1
  },
  "account": {
    "token": "your_archidekt_token",
    "username": "your_archidekt_username",
    "user_id": 123456
  }
}
```

When `search_owned_cards` is called with `account`, or when an MCP auth session is active, the response may include `personal_deck_usage`,
`personal_deck_count`, and `personal_deck_total_quantity` on each owned result so the LLM can warn
that a candidate card is already committed to other personal decks.

## MCP OAuth Auth

To let ChatGPT keep the Archidekt identity attached to the MCP session instead of passing credentials in tool arguments, enable MCP OAuth:

```powershell
$env:ARCHIDEKT_MCP_AUTH_ENABLED = "true"
$env:ARCHIDEKT_MCP_PUBLIC_BASE_URL = "https://your-public-domain"
```

Required notes:

- `ARCHIDEKT_MCP_PUBLIC_BASE_URL` must be the public base URL ChatGPT reaches, without the `/mcp` suffix
- when auth is enabled, the MCP endpoint stays at `/mcp`, but ChatGPT will also use `/.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register`, `/revoke`, and `/auth/archidekt-login`
- the authorization page asks for Archidekt username/email plus password once, exchanges that for an Archidekt token, and stores the resulting Archidekt token in Redis-backed OAuth state
- raw passwords are used only during the authorization step and are not persisted in Redis

After the app is connected through OAuth, private MCP tools can omit `account`, for example:

```text
login_archidekt()
list_personal_decks()
search_owned_cards(collection=..., filters=...)
```

Owned card results may also include `archidekt_card_ids`, which can be reused directly in
`modify_personal_deck_cards` and `upsert_collection_entries` without guessing Archidekt ids.

## Authenticated Management Flow

For fully automated deck/account management, the recommended sequence is:

1. Call `login_archidekt()` if MCP OAuth is active, otherwise call `login_archidekt(account)`.
2. Use the returned `personal_decks` list as the first source of truth for which decks already exist on the account.
3. Use `search_owned_cards` and/or `search_archidekt_cards` to resolve Archidekt `card_id` values.
4. Use `list_personal_decks` or `get_personal_deck_cards` only when you need a fresh deck listing or the contents of a specific deck.
5. Create or update the deck with `create_personal_deck`, `update_personal_deck`, and `modify_personal_deck_cards`.
6. Update the account collection with `upsert_collection_entries` when needed.

`get_personal_deck_cards` returns `deck_relation_id` values for cards already in a deck. Those ids
should be reused for `modify` and `remove` actions in `modify_personal_deck_cards`.

`search_archidekt_cards` returns the numeric Archidekt `card_id` used by both deck card mutations and
collection v2 upserts.

The current authenticated write surface is focused on the account's personal decks and collection v2
entries. It does not yet expose every Archidekt endpoint such as folders, tags, or text-import flows.

## Default Model Response Format

Unless the user explicitly asks for another format, the model should respond with:

1. A short strategy guide.
2. Card recommendations grouped by category.
3. One card per line in the exact format `N Card Name`.

Example:

```text
Strategy Guide
Use early ramp to fix mana, interact efficiently in the mid game, and convert your engine pieces into sustained card advantage and closing power.

Ramp
1 Sol Ring
1 Arcane Signet

Removal
1 Swords to Plowshares
```

## Web UI

The bundled Web UI is fully in English and is meant to help you:

- enter a public Archidekt collection locator
- paste optional authenticated `account` JSON
- test the same flows even when no MCP OAuth session is present
- generate the exact `collection` JSON for MCP tool calls
- generate an LLM instruction block for the current request
- test login and personal deck listing over HTTP
- test overview, owned, and unowned searches over HTTP
- inspect the authenticated write endpoints available for card lookup, deck edits, and collection upserts
- copy generated JSON, instructions, and API responses with one click

The UI does not store user state. Every interaction is rebuilt from the current request.

## Local Development

Create a virtual environment and install the project:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Run a local Redis instance before starting the server.

Start the server directly:

```powershell
$env:ARCHIDEKT_MCP_HOST = "127.0.0.1"
$env:ARCHIDEKT_MCP_PORT = "8000"
$env:ARCHIDEKT_MCP_REDIS_URL = "redis://127.0.0.1:6379/0"
$env:ARCHIDEKT_MCP_CACHE_TTL_SECONDS = "86400"
$env:ARCHIDEKT_MCP_PERSONAL_DECK_CACHE_TTL_SECONDS = "300"
$env:ARCHIDEKT_MCP_USER_AGENT = "archidekt-mcp-server/0.3 (+mailto:you@example.com)"
# Optional MCP OAuth for ChatGPT / remote clients:
# $env:ARCHIDEKT_MCP_AUTH_ENABLED = "true"
# $env:ARCHIDEKT_MCP_PUBLIC_BASE_URL = "https://your-public-domain"
.venv\Scripts\python.exe -m archidekt_commander_mcp.server
```

Then open:

- Web UI: `http://127.0.0.1:8000/`
- MCP endpoint: `http://127.0.0.1:8000/mcp`

Run the test suite:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Docker

Build the image:

```powershell
docker build -t archidekt-mcp-server:latest .
```

For actual local runtime, prefer `docker compose` so the app and Redis start together with the correct network wiring.

## Podman

This repository also includes a `Containerfile`.

Build with Podman:

```powershell
podman build -f Containerfile -t archidekt-mcp-server:latest .
```

Run with Podman Compose using the same `compose.yml` file:

```powershell
podman compose up --build -d
```

## Compose Deployment

The repository ships with `compose.yml` for a two-service deployment:

- `app` for the MCP server
- `redis` for the shared 24-hour cache

Start the stack:

```powershell
docker compose up --build -d
```

Stop the stack:

```powershell
docker compose down
```

With the stack running:

- Web UI: `http://127.0.0.1:8000/`
- MCP endpoint: `http://127.0.0.1:8000/mcp`

The Redis service is configured with append-only persistence and a named volume.

The app service uses environment variables instead of a long command override:

- `ARCHIDEKT_MCP_REDIS_URL`
- `ARCHIDEKT_MCP_USER_AGENT`
- plus image defaults for host, port, transport, and cache TTL

## GitHub Actions

The workflow in `.github/workflows/docker.yml` does two things:

1. Installs the project and runs the Python unit tests.
2. Validates `compose.yml`, builds the Docker image with Buildx, and pushes it to GHCR on `main`.

It runs on:

- pushes to `main`
- pull requests targeting `main`
- manual dispatch

Published image:

- `ghcr.io/dnviti/archidekt-mcp-server:latest`

## MCP Client Configuration

Example Codex MCP configuration:

```toml
[mcp_servers.archidekt-commander]
url = "http://127.0.0.1:8000/mcp"
tool_timeout_sec = 60
```

Because the server is stateless, the model must pass `collection` on every call.

## CLI Options

- `--transport`
- `--host`
- `--port`
- `--log-level`
- `--cache-ttl-seconds`
- `--personal-deck-cache-ttl-seconds`
- `--redis-url`
- `--redis-key-prefix`
- `--http-timeout-seconds`
- `--max-search-results`
- `--scryfall-max-pages`
- `--user-agent`
- `--streamable-http-path`

The same runtime options can also be provided as environment variables with the `ARCHIDEKT_MCP_` prefix, for example:

- `ARCHIDEKT_MCP_TRANSPORT`
- `ARCHIDEKT_MCP_HOST`
- `ARCHIDEKT_MCP_PORT`
- `ARCHIDEKT_MCP_REDIS_URL`
- `ARCHIDEKT_MCP_CACHE_TTL_SECONDS`
- `ARCHIDEKT_MCP_PERSONAL_DECK_CACHE_TTL_SECONDS`
- `ARCHIDEKT_MCP_USER_AGENT`

## Notes

- Set a real contact in the `User-Agent` when exposing the server publicly.
- Redis is the cache backend. The server no longer uses local file-based collection snapshots.
- Authenticated collection snapshots and personal deck overlap data are cached in Redis with account-scoped keys.
- The cache stores fetched Archidekt data, OAuth session state, and Archidekt tokens, but not raw passwords. Reuse the returned `account.token` after login instead of resending credentials when you are not using MCP OAuth.
- The server is stateless with respect to user identity and collection context. Always pass the locator explicitly.
