from __future__ import annotations

import json

from .config import RuntimeSettings


def render_home_page(settings: RuntimeSettings) -> str:
    default_filters = json.dumps(
        {
            "type_includes": ["Instant"],
            "limit": 10,
            "page": 1,
        },
        indent=2,
    )

    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Archidekt MCP Server</title>
  <style>
    :root {{
      --ink: #14281d;
      --forest: #29524a;
      --sand: #f7f3e8;
      --paper: #fffdf8;
      --gold: #d9a441;
      --line: rgba(20, 40, 29, 0.14);
      --muted: #5f6e63;
      --shadow: 0 22px 60px rgba(20, 40, 29, 0.12);
      --radius: 22px;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(217, 164, 65, 0.18), transparent 28rem),
        linear-gradient(135deg, #efe8d4 0%, #f8f4ea 42%, #eef5ee 100%);
      min-height: 100vh;
    }}

    .shell {{
      width: min(1180px, calc(100vw - 2rem));
      margin: 1rem auto 2rem;
    }}

    .hero {{
      background: linear-gradient(145deg, rgba(20, 40, 29, 0.96), rgba(41, 82, 74, 0.96));
      color: #f8f4ea;
      padding: 1.5rem;
      border-radius: calc(var(--radius) + 4px);
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}

    .hero::after {{
      content: "";
      position: absolute;
      inset: auto -5rem -5rem auto;
      width: 18rem;
      height: 18rem;
      background: radial-gradient(circle, rgba(217, 164, 65, 0.28), transparent 65%);
      pointer-events: none;
    }}

    .eyebrow {{
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-size: 0.74rem;
      color: rgba(248, 244, 234, 0.76);
      margin-bottom: 0.7rem;
    }}

    h1 {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.5rem);
      line-height: 0.98;
      max-width: 11ch;
    }}

    .hero p {{
      margin: 1rem 0 0;
      max-width: 68ch;
      color: rgba(248, 244, 234, 0.84);
      line-height: 1.55;
    }}

    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.7rem;
      margin-top: 1.1rem;
    }}

    .badge {{
      border: 1px solid rgba(248, 244, 234, 0.18);
      background: rgba(255, 255, 255, 0.08);
      color: #fff8e7;
      padding: 0.55rem 0.8rem;
      border-radius: 999px;
      font-size: 0.92rem;
    }}

    .grid {{
      display: grid;
      grid-template-columns: 1.05fr 0.95fr;
      gap: 1rem;
      margin-top: 1rem;
    }}

    .panel {{
      background: rgba(255, 253, 248, 0.92);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 1.1rem;
      backdrop-filter: blur(8px);
    }}

    .panel h2 {{
      margin: 0 0 0.7rem;
      font-size: 1.2rem;
    }}

    .panel p,
    .hint,
    .meta {{
      color: var(--muted);
      line-height: 1.5;
    }}

    .stack {{
      display: grid;
      gap: 0.9rem;
    }}

    .two-col {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.8rem;
    }}

    label {{
      display: grid;
      gap: 0.35rem;
      font-size: 0.92rem;
      font-weight: 600;
    }}

    input,
    textarea,
    select,
    button {{
      font: inherit;
    }}

    input,
    textarea,
    select {{
      width: 100%;
      padding: 0.82rem 0.92rem;
      border-radius: 14px;
      border: 1px solid rgba(20, 40, 29, 0.16);
      background: rgba(255, 255, 255, 0.88);
      color: var(--ink);
    }}

    textarea {{
      min-height: 8rem;
      resize: vertical;
    }}

    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
    }}

    button {{
      border: 0;
      border-radius: 999px;
      padding: 0.82rem 1rem;
      cursor: pointer;
      transition: transform 140ms ease, opacity 140ms ease, box-shadow 140ms ease;
    }}

    button:hover {{
      transform: translateY(-1px);
      box-shadow: 0 10px 28px rgba(20, 40, 29, 0.12);
    }}

    .primary {{
      background: var(--forest);
      color: #fffaf0;
    }}

    .secondary {{
      background: rgba(20, 40, 29, 0.08);
      color: var(--ink);
    }}

    .accent {{
      background: var(--gold);
      color: #2a1e08;
    }}

    .copy-button {{
      padding: 0.55rem 0.8rem;
      background: rgba(20, 40, 29, 0.08);
      color: var(--ink);
      font-size: 0.84rem;
    }}

    .code-card {{
      display: grid;
      gap: 0.55rem;
    }}

    .code-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.8rem;
    }}

    .code-header h2 {{
      margin: 0;
    }}

    pre {{
      margin: 0;
      padding: 1rem;
      border-radius: 18px;
      background: #191f1b;
      color: #f5f6ef;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.45;
    }}

    .small {{
      font-size: 0.88rem;
    }}

    .status {{
      min-height: 1.4rem;
      color: var(--forest);
      font-weight: 600;
    }}

    @media (max-width: 920px) {{
      .grid,
      .two-col {{
        grid-template-columns: 1fr;
      }}

      .shell {{
        width: min(100vw - 1rem, 100%);
      }}

      .code-header {{
        flex-direction: column;
        align-items: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">Stateless Public MCP</div>
      <h1>Archidekt Commander Web UI</h1>
      <p>
        No login, no user persistence, and no user-specific environment variables.
        Every request must carry the Archidekt collection locator, and the server handles
        the request deterministically from that context.
      </p>
      <div class="badges">
        <div class="badge">MCP Endpoint: {mcp_path}</div>
        <div class="badge">Transport: {transport}</div>
        <div class="badge">Redis TTL: {cache_ttl}s</div>
        <div class="badge">Stateless HTTP: {stateless_http}</div>
      </div>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>Collection Context</h2>
        <p class="hint">
          Provide exactly one of collection ID, public collection URL, or Archidekt username.
          This object must be passed in every MCP tool call.
        </p>
        <div class="stack">
          <div class="two-col">
            <label>
              Collection ID
              <input id="collection-id" placeholder="548188" />
            </label>
            <label>
              Game
              <select id="game">
                <option value="1">1 · Paper</option>
                <option value="2">2 · MTGO</option>
                <option value="3">3 · Arena</option>
              </select>
            </label>
          </div>

          <label>
            Collection URL
            <input id="collection-url" placeholder="https://archidekt.com/collection/v2/548188" />
          </label>

          <label>
            Archidekt Username
            <input id="username" placeholder="your_username" />
          </label>

          <label>
            User Prompt To Send To The LLM
            <textarea id="prompt" placeholder="Build me an owned blue instant package for a Commander control deck."></textarea>
          </label>

          <label>
            Filters JSON For Quick API Testing
            <textarea id="filters">{default_filters}</textarea>
          </label>

          <div class="actions">
            <button class="primary" id="build-context">Generate LLM Context</button>
            <button class="secondary" id="test-overview">Test Overview</button>
            <button class="secondary" id="test-owned">Test Owned</button>
            <button class="accent" id="test-unowned">Test Unowned</button>
          </div>
          <div class="status" id="status"></div>
        </div>
      </article>

      <article class="panel stack">
        <div class="code-card">
          <div class="code-header">
            <div>
              <h2>Collection JSON</h2>
              <p class="meta small">Pass this in every MCP call instead of relying on previous state.</p>
            </div>
            <button class="copy-button" data-copy-target="collection-json">Copy JSON</button>
          </div>
          <pre id="collection-json"></pre>
        </div>

        <div class="code-card">
          <div class="code-header">
            <div>
              <h2>LLM Instructions</h2>
              <p class="meta small">Copy this block into the active prompt for the current request.</p>
            </div>
            <button class="copy-button" data-copy-target="llm-instructions">Copy Instructions</button>
          </div>
          <pre id="llm-instructions"></pre>
        </div>

        <div class="code-card">
          <div class="code-header">
            <div>
              <h2>API Test Result</h2>
              <p class="meta small">These HTTP endpoints run the same logic used by the MCP tools.</p>
            </div>
            <button class="copy-button" data-copy-target="result">Copy Result</button>
          </div>
          <pre id="result">Use one of the test buttons to run a stateless request.</pre>
        </div>
      </article>
    </section>
  </main>

  <script>
    const collectionJsonEl = document.getElementById("collection-json");
    const instructionsEl = document.getElementById("llm-instructions");
    const resultEl = document.getElementById("result");
    const statusEl = document.getElementById("status");

    function cleanValue(value) {{
      const trimmed = value.trim();
      return trimmed.length ? trimmed : null;
    }}

    function readCollection() {{
      const collection = {{}};
      const collectionId = cleanValue(document.getElementById("collection-id").value);
      const collectionUrl = cleanValue(document.getElementById("collection-url").value);
      const username = cleanValue(document.getElementById("username").value);
      const game = Number(document.getElementById("game").value || "1");

      if (collectionId) {{
        collection.collection_id = Number(collectionId);
      }}
      if (collectionUrl) {{
        collection.collection_url = collectionUrl;
      }}
      if (username) {{
        collection.username = username;
      }}
      collection.game = game;
      return collection;
    }}

    function readFilters() {{
      const raw = document.getElementById("filters").value.trim();
      if (!raw) {{
        return {{}};
      }}
      return JSON.parse(raw);
    }}

    function buildInstructions(collection, prompt) {{
      const promptLine = prompt.trim()
        ? "\\nUser request: " + prompt.trim()
        : "";

      return [
        "Use this MCP server in a stateless way.",
        "Pass the following `collection` object in every tool call:",
        JSON.stringify(collection, null, 2),
        "Rules:",
        "- Never assume the collection remains in memory between requests.",
        "- Use `get_collection_overview` whenever you need context before making recommendations.",
        "- Use `search_owned_cards` only for owned cards.",
        "- Use `search_unowned_cards` only for missing cards.",
        "Default response format:",
        "- Use this structure unless the user explicitly asks for a different format.",
        "- Start with a short strategy guide that explains the deck plan, key synergies, pacing, and win conditions.",
        "- Then group cards by category.",
        "- Inside each category, write one card per line as `N Card Name`.",
        "- `N` is the exact quantity of that card to add to the deck.",
        "- Do not use bullets or numbering on card lines.",
        "Example:",
        "Strategy Guide",
        "Use early ramp to fix mana, stabilize through efficient interaction, then snowball advantage with the deck's main engines and finishers.",
        "",
        "Ramp",
        "1 Sol Ring",
        "1 Arcane Signet",
        "",
        "Removal",
        "1 Swords to Plowshares",
        promptLine,
      ].filter(Boolean).join("\\n");
    }}

    function updateOutputs() {{
      const collection = readCollection();
      const prompt = document.getElementById("prompt").value;
      collectionJsonEl.textContent = JSON.stringify(collection, null, 2);
      instructionsEl.textContent = buildInstructions(collection, prompt);
    }}

    async function runRequest(endpoint, body) {{
      statusEl.textContent = "Request in progress...";
      resultEl.textContent = "";
      try {{
        const response = await fetch(endpoint, {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json"
          }},
          body: JSON.stringify(body)
        }});

        const payload = await response.json();
        resultEl.textContent = JSON.stringify(payload, null, 2);
        statusEl.textContent = response.ok ? "Request completed." : "Request failed.";
      }} catch (error) {{
        statusEl.textContent = "Network error.";
        resultEl.textContent = String(error);
      }}
    }}

    async function copyFromElement(targetId, button) {{
      const target = document.getElementById(targetId);
      try {{
        await navigator.clipboard.writeText(target.textContent || "");
        const previous = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => {{
          button.textContent = previous;
        }}, 1200);
      }} catch (error) {{
        statusEl.textContent = "Clipboard copy failed.";
        resultEl.textContent = String(error);
      }}
    }}

    document.getElementById("build-context").addEventListener("click", () => {{
      try {{
        updateOutputs();
        readFilters();
        statusEl.textContent = "LLM context updated.";
      }} catch (error) {{
        statusEl.textContent = "Filters JSON is invalid.";
        resultEl.textContent = String(error);
      }}
    }});

    document.getElementById("test-overview").addEventListener("click", async () => {{
      try {{
        const collection = readCollection();
        updateOutputs();
        await runRequest("/api/overview", {{ collection }});
      }} catch (error) {{
        statusEl.textContent = "Collection input is invalid.";
        resultEl.textContent = String(error);
      }}
    }});

    document.getElementById("test-owned").addEventListener("click", async () => {{
      try {{
        const collection = readCollection();
        const filters = readFilters();
        updateOutputs();
        await runRequest("/api/search-owned", {{ collection, filters }});
      }} catch (error) {{
        statusEl.textContent = "Request input is invalid.";
        resultEl.textContent = String(error);
      }}
    }});

    document.getElementById("test-unowned").addEventListener("click", async () => {{
      try {{
        const collection = readCollection();
        const filters = readFilters();
        updateOutputs();
        await runRequest("/api/search-unowned", {{ collection, filters }});
      }} catch (error) {{
        statusEl.textContent = "Request input is invalid.";
        resultEl.textContent = String(error);
      }}
    }});

    document.querySelectorAll("[data-copy-target]").forEach((button) => {{
      button.addEventListener("click", () => copyFromElement(button.dataset.copyTarget, button));
    }});

    document.querySelectorAll("input, textarea, select").forEach((element) => {{
      element.addEventListener("input", updateOutputs);
    }});

    updateOutputs();
  </script>
</body>
</html>
""".format(
        cache_ttl=settings.cache_ttl_seconds,
        default_filters=default_filters,
        mcp_path=settings.streamable_http_path,
        stateless_http="yes" if settings.stateless_http else "no",
        transport=settings.transport,
    )
