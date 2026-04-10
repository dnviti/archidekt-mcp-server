# archidekt-mcp-server

Stateless MCP server for Commander deckbuilding against public Archidekt collections and Scryfall.

The server is designed for LLM-driven workflows:

- no user accounts
- no user session persistence
- no per-user environment variables
- every request passes the collection locator explicitly
- collection snapshots are cached in Redis for 24 hours by default

## What It Exposes

MCP tools:

- `get_collection_overview(collection)`
- `refresh_collection_cache(collection)`
- `search_owned_cards(collection, filters)`
- `search_unowned_cards(collection, filters)`

HTTP routes:

- `/` English Web UI with copy buttons for generated code blocks
- `/health` health check
- `/api/overview` stateless HTTP test for `get_collection_overview`
- `/api/search-owned` stateless HTTP test for `search_owned_cards`
- `/api/search-unowned` stateless HTTP test for `search_unowned_cards`
- `/mcp` streamable HTTP MCP endpoint

## Request Model

Every tool call must include `collection` with one of:

- `collection_id`
- `collection_url`
- `username`

Optional fields:

- `game` where `1 = Paper`, `2 = MTGO`, `3 = Arena`

Example:

```json
{
  "collection": {
    "username": "your_archidekt_username",
    "game": 1
  }
}
```

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
- generate the exact `collection` JSON for MCP tool calls
- generate an LLM instruction block for the current request
- test overview, owned, and unowned searches over HTTP
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
$env:ARCHIDEKT_MCP_USER_AGENT = "archidekt-mcp-server/0.3 (+mailto:you@example.com)"
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
- `ARCHIDEKT_MCP_USER_AGENT`

## Notes

- Set a real contact in the `User-Agent` when exposing the server publicly.
- Redis is the cache backend. The server no longer uses local file-based collection snapshots.
- The server is stateless with respect to user identity and collection context. Always pass the locator explicitly.
