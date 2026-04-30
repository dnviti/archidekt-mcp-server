from __future__ import annotations

import html


def render_archidekt_authorize_page(
    *,
    request_id: str,
    error_message: str | None = None,
    persist_login_credentials: bool = True,
) -> str:
    error_block = ""
    if error_message:
        error_block = (
            '<p style="margin:0 0 1rem;color:#a11f1f;background:#fff2f2;'
            'border:1px solid #f2c3c3;border-radius:12px;padding:0.85rem 1rem;">'
            f"{html.escape(error_message)}</p>"
        )
    escaped_request_id = html.escape(request_id)
    credential_note = (
        "The MCP server stores the resulting Archidekt token, OAuth session, and Archidekt "
        "login credential in Redis so it can renew the Archidekt token later. Keep Redis "
        "private and persistent, and disconnect the app to revoke this session."
        if persist_login_credentials
        else "The password is used only during this authorization step. The MCP server "
        "stores the resulting Archidekt token and OAuth session in Redis, but login renewal "
        "is disabled on this deployment."
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Connect Archidekt</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: linear-gradient(145deg, #f7f3e8 0%, #eef5ee 100%);
      color: #14281d;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
    }}
    .card {{
      width: min(28rem, calc(100vw - 2rem));
      background: rgba(255,255,255,0.94);
      border: 1px solid rgba(20,40,29,0.12);
      border-radius: 22px;
      box-shadow: 0 22px 60px rgba(20,40,29,0.12);
      padding: 1.4rem;
    }}
    h1 {{
      margin: 0 0 0.6rem;
      font-size: 1.8rem;
    }}
    p {{
      line-height: 1.5;
      color: #415247;
    }}
    label {{
      display: grid;
      gap: 0.35rem;
      margin-top: 0.9rem;
      font-weight: 600;
      font-size: 0.95rem;
    }}
    input {{
      width: 100%;
      padding: 0.82rem 0.92rem;
      border-radius: 14px;
      border: 1px solid rgba(20,40,29,0.16);
      background: rgba(255,255,255,0.92);
      box-sizing: border-box;
      font: inherit;
    }}
    button {{
      margin-top: 1rem;
      width: 100%;
      border: 0;
      border-radius: 999px;
      padding: 0.9rem 1rem;
      background: #29524a;
      color: #fffaf0;
      font: inherit;
      cursor: pointer;
    }}
    .note {{
      font-size: 0.88rem;
      color: #5f6e63;
      margin-top: 0.8rem;
    }}
  </style>
</head>
<body>
  <main class="card">
    <h1>Connect Archidekt</h1>
    <p>Sign in with your Archidekt username and password so this MCP app can act on your decks and collection without asking the model to resend your credentials on every tool call.</p>
    {error_block}
    <form method="post">
      <input type="hidden" name="request_id" value="{escaped_request_id}" />
      <label>
        Archidekt Username Or Email
        <input type="text" name="identifier" autocomplete="username" required />
      </label>
      <label>
        Password
        <input type="password" name="password" autocomplete="current-password" required />
      </label>
      <button type="submit">Continue</button>
    </form>
    <p class="note">{credential_note}</p>
  </main>
</body>
</html>"""
