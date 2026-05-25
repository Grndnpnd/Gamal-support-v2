"""
admin_routes.py
---------------
Admin panel mounted onto the FastAPI `app` from api_server.py.

What lives here:
  - /admin/login           login form (GET) and submit (POST)
  - /admin/logout          clears the session cookie
  - /admin                 main dashboard (currently: list of overrides)
  - /admin/overrides       CRUD endpoints for doc overrides

Auth model (v1, deliberately simple):
  - Single ADMIN_PASSWORD env var, shared among the team via 1Password.
  - On successful login we set a signed session cookie using itsdangerous.
  - All /admin/* routes (except /admin/login itself) require that cookie.
  - No per-user audit trail — when more than one person needs to know
    "who set this override," graduate to a Postgres users table.

CSRF protection: we check the Origin / Referer header on POSTs. Cheap and
sufficient for a single-admin internal tool. If you ever expose this to
the public internet, add a proper CSRF token.
"""

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from redis_overrides import (
    create_override,
    delete_override,
    get_override,
    get_override_hits,
    list_overrides,
    update_override,
)
from redis_settings import get_settings, update_settings
from redis_pubsub import request_reindex, get_reindex_statuses, KNOWN_SERVICES
import db

log = logging.getLogger(__name__)


# ─── Config ───────────────────────────────────────────────────────────────────

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Session cookie signing secret. If not set we generate a random one at boot,
# which means cookies invalidate on every restart — fine for a small team,
# annoying if it bothers you. Set ADMIN_SESSION_SECRET explicitly to persist.
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET") or secrets.token_hex(32)
SESSION_COOKIE_NAME = "gamal_admin_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

_serializer = URLSafeTimedSerializer(ADMIN_SESSION_SECRET, salt="gamal-admin")

if not ADMIN_PASSWORD:
    log.warning(
        "ADMIN_PASSWORD not set — admin panel will REJECT all logins. "
        "Set it in your environment before deploying."
    )

router = APIRouter(prefix="/admin", tags=["admin"])


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def _make_session_token() -> str:
    """Create a fresh signed token for a successful login."""
    return _serializer.dumps({"login_at": datetime.now(timezone.utc).isoformat()})


def _verify_session(token: Optional[str]) -> bool:
    """Return True if the token is valid and not expired."""
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _check_csrf(request: Request) -> None:
    """
    Light CSRF protection — confirm the request originated from our own host.
    Browsers always send Origin on cross-origin POSTs and same-origin POSTs
    from form submits. Server-to-server posts (curl, attackers) typically
    don't bother to forge it.

    We don't enforce a specific value — we just require that one of Origin
    or Referer is present. This blocks the simplest CSRF vector (an image
    tag or auto-submitting form on another site) without needing token
    plumbing.
    """
    if not (request.headers.get("origin") or request.headers.get("referer")):
        raise HTTPException(status_code=403, detail="Missing Origin/Referer header")


def require_admin(
    request: Request,
    session: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
) -> None:
    """
    Dependency that gates every authenticated admin route.
    Redirects to /admin/login on browser requests, returns 401 for others.
    """
    if _verify_session(session):
        return

    # If this is a normal browser navigation, redirect to login.
    # If it's an XHR / API call, fail with 401 so the caller can react.
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        raise HTTPException(
            status_code=303,
            detail="Login required",
            headers={"Location": "/admin/login"},
        )
    raise HTTPException(status_code=401, detail="Not authenticated")


# ─── HTML helpers ─────────────────────────────────────────────────────────────
# Single-file HTML, no external templates. If this grows past ~3 pages, swap
# to Jinja2 templates in a /templates dir. For one page it's overkill.

# Bankr logo — embedded as a base64 data URI (72px PNG, ~6KB) so the panel
# needs no static-file route. Displayed at 34px in the header.
_LOGO_DATA_URI = "data:image/png;base64,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"

_BASE_STYLE = """
<style>
  :root {
    /* Bankr brand */
    --brand-purple: #805dee;
    --brand-orange: #ff673c;
    --brand-yellow: #ffe143;
    /* Dark surfaces */
    --bg:       #0d0c14;
    --bg-2:     #13111d;
    --panel:    #1a1824;
    --panel-2:  #221f30;
    --border:   #2d2940;
    --border-2: #3a3552;
    /* Text */
    --text:     #ece9f5;
    --text-dim: #9690ad;
    --text-faint: #6b6680;
    /* Accents (semantic) */
    --accent:   #805dee;
    --accent-2: #9d80f5;
    --good:     #ffe143;
    --warn:     #ff673c;
    --danger:   #ff4d6a;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background:
      radial-gradient(900px 500px at 85% -5%, rgba(128,93,238,0.16), transparent 60%),
      radial-gradient(700px 500px at 5% 105%, rgba(255,103,60,0.10), transparent 55%),
      var(--bg);
    background-attachment: fixed;
    color: var(--text);
    line-height: 1.55;
    font-size: 14px;
  }

  /* ── Header ── */
  header {
    background: rgba(19,17,29,0.85);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
    padding: 14px 28px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand img { width: 34px; height: 34px; border-radius: 8px; display: block; }
  .brand h1 {
    margin: 0; font-size: 16px; font-weight: 700; letter-spacing: 0.3px;
  }
  .brand .tag {
    font-size: 11px; color: var(--text-faint); font-weight: 500;
    text-transform: uppercase; letter-spacing: 1px;
  }
  header nav { display: flex; gap: 6px; }
  header nav a {
    color: var(--text-dim);
    text-decoration: none;
    font-size: 13px;
    font-weight: 500;
    padding: 7px 13px;
    border-radius: 7px;
    transition: background 0.12s, color 0.12s;
  }
  header nav a:hover { color: var(--text); background: var(--panel-2); }

  main { max-width: 1240px; margin: 28px auto; padding: 0 28px; }
  h2 { font-size: 21px; margin: 0 0 4px; font-weight: 700; }
  h3 { font-size: 14px; margin: 0 0 14px; font-weight: 600; letter-spacing: 0.2px; }
  p.dim { color: var(--text-dim); margin: 0 0 20px; font-size: 13px; }

  /* ── Panels ── */
  .panel {
    background: linear-gradient(180deg, var(--panel) 0%, var(--bg-2) 100%);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 22px;
    margin-bottom: 18px;
  }
  .panel h3 { display: flex; align-items: center; gap: 8px; }
  .panel h3::before {
    content: "";
    width: 3px; height: 14px;
    background: var(--brand-purple);
    border-radius: 2px;
  }
  .panel.accent-orange h3::before { background: var(--brand-orange); }
  .panel.accent-yellow h3::before { background: var(--brand-yellow); }

  /* ── Grid system ── */
  .grid { display: grid; gap: 18px; }
  .grid.cols-2 { grid-template-columns: 1fr 1fr; }
  .grid.cols-3 { grid-template-columns: 1fr 1fr 1fr; }
  @media (max-width: 880px) {
    .grid.cols-2, .grid.cols-3 { grid-template-columns: 1fr; }
  }

  /* ── Tables ── */
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: 10px 8px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  th {
    color: var(--text-faint); font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.6px;
  }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: rgba(128,93,238,0.05); }

  /* ── Pills ── */
  .pill {
    display: inline-block;
    padding: 2px 9px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
  }
  .pill.on   { background: rgba(255,225,67,0.16);  color: var(--brand-yellow); }
  .pill.off  { background: rgba(150,144,173,0.16); color: var(--text-dim); }
  .pill.warn { background: rgba(255,103,60,0.16);  color: var(--brand-orange); }
  .pill.danger-pill { background: rgba(255,77,106,0.18); color: var(--danger); }
  .pill.purple { background: rgba(128,93,238,0.20); color: var(--accent-2); }

  /* ── Forms ── */
  input[type=text], input[type=password], textarea, input[type=datetime-local] {
    width: 100%;
    background: var(--bg-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    color: var(--text);
    font-family: inherit;
    font-size: 13px;
  }
  input:focus, textarea:focus {
    outline: none;
    border-color: var(--brand-purple);
    box-shadow: 0 0 0 3px rgba(128,93,238,0.15);
  }
  textarea { min-height: 96px; resize: vertical; }
  label {
    display: block; font-size: 12px; color: var(--text-dim);
    margin: 12px 0 6px; font-weight: 500;
  }
  label.inline {
    display: flex; align-items: center; margin: 14px 0;
    color: var(--text); font-size: 13px;
  }
  label.inline input { margin-right: 8px; width: auto; }
  small.hint { color: var(--text-faint); font-size: 11px; display: block; margin-top: 4px; }

  /* ── Buttons ── */
  button, .btn {
    background: var(--brand-purple);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
    transition: filter 0.12s;
  }
  button:hover, .btn:hover { filter: brightness(1.12); }
  button.danger, .btn.danger { background: var(--danger); }
  button.secondary, .btn.secondary {
    background: transparent; color: var(--text);
    border: 1px solid var(--border-2);
  }
  button.secondary:hover, .btn.secondary:hover { background: var(--panel-2); filter: none; }

  .row-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .row-actions form { display: inline; }

  .keywords-list { display: flex; flex-wrap: wrap; gap: 4px; }
  .kw-chip {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 2px 8px;
    font-size: 11px;
    color: var(--text-dim);
    font-family: "SF Mono", Consolas, monospace;
  }

  .flash {
    padding: 11px 15px; border-radius: 9px; margin-bottom: 18px; font-size: 13px;
  }
  .flash.error {
    background: rgba(255,77,106,0.13);
    border: 1px solid rgba(255,77,106,0.4);
    color: #ffb3c0;
  }
  .empty {
    text-align: center; padding: 38px; color: var(--text-faint); font-size: 13px;
  }
  code {
    background: var(--bg-2); padding: 2px 6px;
    border-radius: 4px; font-size: 12px;
    font-family: "SF Mono", Consolas, monospace;
  }

  /* ── Stat cards ── */
  .stat-grid {
    display: grid;
    /* Fixed 4 columns -> 8 cards land as a clean 4x2 block, no orphan row.
       Steps down to 2, then 1, on narrower screens. */
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
    margin-bottom: 20px;
  }
  @media (max-width: 920px) {
    .stat-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 480px) {
    .stat-grid { grid-template-columns: 1fr; }
  }
  .stat-card {
    background: linear-gradient(180deg, var(--panel) 0%, var(--bg-2) 100%);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 18px;
    position: relative;
    overflow: hidden;
  }
  .stat-card::after {
    content: "";
    position: absolute; top: 0; left: 0;
    width: 100%; height: 2px;
    background: var(--brand-purple);
    opacity: 0.7;
  }
  .stat-card.c-orange::after { background: var(--brand-orange); }
  .stat-card.c-yellow::after { background: var(--brand-yellow); }
  .stat-value {
    font-size: 26px; font-weight: 800; color: var(--text);
    letter-spacing: -0.5px;
  }
  .stat-label {
    font-size: 12px; color: var(--text-dim); margin-top: 1px; font-weight: 500;
  }
  .stat-sub { font-size: 10px; color: var(--text-faint); margin-top: 3px; }

  /* ── Charts ── */
  .chart-wrap { position: relative; height: 260px; margin-top: 6px; }
  .chart-wrap.short { height: 200px; }

  /* ── Time controls ── */
  .controls {
    display: flex; flex-wrap: wrap; gap: 14px;
    align-items: center; justify-content: space-between;
    margin-bottom: 22px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 16px;
  }
  .preset-row { display: flex; gap: 6px; }
  .rangebtn {
    padding: 7px 14px; border-radius: 7px;
    border: 1px solid var(--border-2);
    color: var(--text-dim); text-decoration: none; font-size: 12px;
    font-weight: 600;
  }
  .rangebtn:hover { color: var(--text); }
  .rangebtn.active {
    background: var(--brand-purple); color: #fff; border-color: var(--brand-purple);
  }
  .range-form { display: flex; gap: 8px; align-items: center; }
  .range-form input[type=datetime-local] { width: auto; }

  /* ── Mini bar list (busiest channels / users) ── */
  .barlist { display: flex; flex-direction: column; gap: 9px; }
  .barlist-row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
  .barlist-label {
    flex: 0 0 38%; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
  }
  .barlist-track {
    flex: 1; height: 8px; background: var(--bg-2);
    border-radius: 5px; overflow: hidden;
  }
  .barlist-fill {
    height: 100%; border-radius: 5px;
    background: linear-gradient(90deg, var(--brand-purple), var(--accent-2));
  }
  .barlist-val {
    flex: 0 0 auto; font-variant-numeric: tabular-nums;
    color: var(--text-dim); font-size: 12px; min-width: 32px; text-align: right;
  }
</style>
"""


def _page(title: str, body: str, show_nav: bool = True, head_extra: str = "") -> str:
    nav = ""
    if show_nav:
        nav = """
        <nav>
          <a href="/admin">Controls</a>
          <a href="/admin/stats">Stats</a>
          <a href="/admin/logout">Sign out</a>
        </nav>
        """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Gamal Admin</title>
  {_BASE_STYLE}
  {head_extra}
</head>
<body>
  <header>
    <div class="brand">
      <img src="{_LOGO_DATA_URI}" alt="Bankr">
      <div>
        <h1>Gamal</h1>
        <div class="tag">Support Bot Admin</div>
      </div>
    </div>
    {nav}
  </header>
  <main>{body}</main>
</body>
</html>"""


def _esc(s: Optional[str]) -> str:
    """Minimal HTML escaping for user-supplied strings rendered into HTML."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _relative_time(iso_str: Optional[str]) -> str:
    """
    Turn an ISO-8601 timestamp into a human 'x minutes ago' string for the
    re-index status readout. Returns 'unknown' if the value can't be parsed.
    """
    if not iso_str:
        return "unknown"
    try:
        then = datetime.fromisoformat(iso_str)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - then
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return "unknown"


# ─── Login / Logout ───────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_form(error: Optional[str] = None):
    flash = f'<div class="flash error">{_esc(error)}</div>' if error else ""
    body = f"""
    <div style="max-width: 380px; margin: 80px auto;">
      <div class="panel">
        <h3>Sign in</h3>
        <p class="dim">Enter the admin password to manage overrides.</p>
        {flash}
        <form method="POST" action="/admin/login">
          <label>Password</label>
          <input type="password" name="password" autocomplete="current-password" required autofocus>
          <div style="margin-top: 16px;">
            <button type="submit">Sign in</button>
          </div>
        </form>
      </div>
    </div>
    """
    return HTMLResponse(_page("Sign in", body, show_nav=False))


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    _check_csrf(request)

    if not ADMIN_PASSWORD:
        # Configured wrong — refuse rather than authenticate anybody
        return RedirectResponse(
            "/admin/login?error=Admin+panel+is+not+configured+(ADMIN_PASSWORD+missing)",
            status_code=303,
        )

    # Constant-time comparison so we don't leak length/prefix info.
    if not secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        return RedirectResponse(
            "/admin/login?error=Incorrect+password",
            status_code=303,
        )

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _make_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # Set True if you put this behind HTTPS only (Railway gives you HTTPS by default)
    )
    log.info("Admin login successful")
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ─── Dashboard (override list) ────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(_: None = Depends(require_admin)):
    settings = await get_settings()
    overrides = await list_overrides(only_active=False)
    # Sort newest first
    overrides.sort(key=lambda o: o.get("created_at", ""), reverse=True)

    # Annotate with hit counts in parallel-ish — Redis pipeline would be tidier
    # but list is small. Keep it simple.
    rows_html = []
    for o in overrides:
        hits = await get_override_hits(o["id"])
        status_pill = (
            '<span class="pill on">enabled</span>'
            if o.get("enabled") else '<span class="pill off">disabled</span>'
        )
        ticket_pill = (
            '<span class="pill warn">ticket offer on</span>'
            if o.get("allow_ticket_offer") else ''
        )
        kw_chips = " ".join(
            f'<span class="kw-chip">{_esc(k)}</span>' for k in o.get("keywords", [])
        )
        toggle_label = "Disable" if o.get("enabled") else "Enable"
        rows_html.append(f"""
          <tr>
            <td>
              <strong>{_esc(o.get('name'))}</strong><br>
              <small class="hint">{status_pill} {ticket_pill}</small>
            </td>
            <td><div class="keywords-list">{kw_chips}</div></td>
            <td>{hits}</td>
            <td class="row-actions">
              <form method="POST" action="/admin/overrides/{_esc(o['id'])}/toggle">
                <button class="secondary" type="submit">{toggle_label}</button>
              </form>
              <a class="btn secondary" href="/admin/overrides/{_esc(o['id'])}/edit">Edit</a>
              <form method="POST" action="/admin/overrides/{_esc(o['id'])}/delete"
                    onsubmit="return confirm('Delete override &quot;{_esc(o.get('name'))}&quot;?');">
                <button class="danger" type="submit">Delete</button>
              </form>
            </td>
          </tr>
        """)

    table_html = (
        f"""
        <table>
          <thead>
            <tr><th>Name</th><th>Keywords</th><th>Lifetime hits</th><th>Actions</th></tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
        """
        if overrides else
        '<div class="empty">No overrides yet. Create one to take over the bot\'s responses for matching keywords.</div>'
    )

    # Busy-mode panel state
    busy_on = settings.get("busy_mode_enabled", False)
    busy_roles_value = ", ".join(settings.get("busy_mode_roles", []))
    busy_status_pill = (
        '<span class="pill warn">busy mode ON</span>'
        if busy_on else '<span class="pill off">busy mode off</span>'
    )

    # ── Docs re-index panel state ────────────────────────────────────────────
    reindex_statuses = await get_reindex_statuses()
    reindex_rows = []
    any_running = False
    for svc in KNOWN_SERVICES:
        st = reindex_statuses.get(svc)
        if not st:
            pill = '<span class="pill off">no data</span>'
            when = "never re-indexed via panel"
        else:
            state = st.get("state", "unknown")
            when = _relative_time(st.get("at"))
            if state == "running":
                any_running = True
                pill = '<span class="pill warn">re-indexing…</span>'
                when = f"started {when}"
            elif state == "done":
                pill = '<span class="pill on">up to date</span>'
                when = f"finished {when}"
            elif state == "failed":
                pill = '<span class="pill danger-pill">failed</span>'
                detail = st.get("detail", "")
                when = f"failed {when}" + (f" — {_esc(detail)}" if detail else "")
            else:
                pill = '<span class="pill off">unknown</span>'
        reindex_rows.append(f"""
          <tr>
            <td><strong>{_esc(svc)}</strong> service</td>
            <td>{pill}</td>
            <td class="hint">{when}</td>
          </tr>
        """)
    reindex_table = f"""
      <table>
        <thead><tr><th>Service</th><th>Status</th><th>Last re-index</th></tr></thead>
        <tbody>{''.join(reindex_rows)}</tbody>
      </table>
    """
    reindex_btn_label = "Re-indexing…" if any_running else "Re-pull &amp; re-index docs"
    reindex_btn_attr = "disabled" if any_running else ""

    body = f"""
    <h2>Bot Controls</h2>
    <p class="dim">
      Operational toggles and response overrides for the Gamal support bot.
    </p>

    <div class="grid cols-2">
      <div class="panel">
        <h3>Busy mode &nbsp; {busy_status_pill}</h3>
        <p class="dim">
          Suppresses the bot's passive proactive offers to the staff roles
          below — use during busy windows so it doesn't jump in on the team.
          Staff can still @mention the bot directly.
        </p>
        <form method="POST" action="/admin/settings/busy-mode">
          <label class="inline">
            <input type="checkbox" name="busy_mode_enabled" value="1" {"checked" if busy_on else ""}>
            Enable busy mode
          </label>
          <label>Staff roles to ignore on the passive path
            <small class="hint">Comma-separated Discord role names, case-insensitive.</small>
          </label>
          <input type="text" name="busy_mode_roles" value="{_esc(busy_roles_value)}"
                 placeholder="Moderator, Support">
          <div style="margin-top: 16px;">
            <button type="submit">Save busy mode</button>
          </div>
        </form>
      </div>

      <div class="panel accent-orange">
        <h3>Documentation index</h3>
        <p class="dim">
          Re-pull the docs and rebuild the search index on the bot and api
          services. Takes about a minute each; the old index keeps serving
          until the new one is ready, so there's no downtime.
        </p>
        {reindex_table}
        <form method="POST" action="/admin/reindex" style="margin-top:16px;">
          <button type="submit" {reindex_btn_attr}>{reindex_btn_label}</button>
        </form>
      </div>
    </div>

    <h2 style="margin-top:36px;">Doc Overrides</h2>
    <p class="dim">
      Overrides intercept incoming messages that match any keyword and reply
      with your message instead of the doc-based answer. Useful during outages
      or while you're tracking a known issue.
    </p>

    <div class="panel">
      <h3>Active &amp; configured overrides</h3>
      {table_html}
    </div>

    <div class="panel">
      <h3>New override</h3>
      <form method="POST" action="/admin/overrides">
        <label>Name <small class="hint">For your reference. Shown nowhere to users.</small></label>
        <input type="text" name="name" placeholder="e.g. Service paused — incident #4521" required>

        <label>Keywords <small class="hint">Comma-separated. Case-insensitive substring match against the user's message.</small></label>
        <input type="text" name="keywords" placeholder="401, can't sign in, login, locked out" required>

        <label>Message <small class="hint">The reply the bot will send when a keyword matches.</small></label>
        <textarea name="message" placeholder="We have temporarily paused the service. We'll announce as soon as it's back online." required></textarea>

        <label class="inline">
          <input type="checkbox" name="allow_ticket_offer" value="1">
          Also offer the user a support ticket
          <small class="hint" style="margin-left: 8px;">If on, the bot follows the override with the "want a ticket?" prompt.</small>
        </label>

        <label>Starts at <small class="hint">Optional. Leave blank for "active immediately." UTC.</small></label>
        <input type="datetime-local" name="starts_at">

        <label>Ends at <small class="hint">Optional. Leave blank for "no expiry." UTC.</small></label>
        <input type="datetime-local" name="ends_at">

        <div style="margin-top: 20px;">
          <button type="submit">Create override</button>
        </div>
      </form>
    </div>
    """
    return HTMLResponse(_page("Bot Controls", body))


# ─── Bot Settings ─────────────────────────────────────────────────────────────

@router.post("/settings/busy-mode")
async def settings_busy_mode(
    request: Request,
    busy_mode_enabled: Optional[str] = Form(None),
    busy_mode_roles: str = Form(""),
    _: None = Depends(require_admin),
):
    """
    Save the busy-mode toggle and its staff role list.

    An unchecked checkbox submits no value at all, so busy_mode_enabled being
    None means "off" — that's the standard HTML form checkbox behavior.
    """
    _check_csrf(request)

    roles = [r.strip() for r in busy_mode_roles.split(",") if r.strip()]
    await update_settings(
        busy_mode_enabled=bool(busy_mode_enabled),
        busy_mode_roles=roles,
    )
    return RedirectResponse("/admin", status_code=303)


@router.post("/reindex")
async def trigger_reindex(request: Request, _: None = Depends(require_admin)):
    """
    Manually trigger a docs re-index across all services.

    Publishes a signal via Redis pub/sub; the bot and api services each pick
    it up and rebuild their own index, reporting status back. This route
    returns immediately — it does not wait for the re-index to finish. The
    Controls page status table reflects progress on refresh.
    """
    _check_csrf(request)
    await request_reindex(triggered_by="admin panel")
    return RedirectResponse("/admin", status_code=303)


# ─── Override CRUD ────────────────────────────────────────────────────────────

def _parse_dt(form_value: Optional[str]) -> Optional[str]:
    """
    Convert a browser <input type=datetime-local> value (e.g. '2026-05-19T14:30')
    into an ISO-8601 UTC string. Browsers send naive local time so we treat the
    value as UTC by appending +00:00 — admin notes in the form make this explicit.
    """
    if not form_value:
        return None
    try:
        # Treat as UTC (form label says so)
        dt = datetime.fromisoformat(form_value).replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


@router.post("/overrides")
async def overrides_create(
    request: Request,
    name: str = Form(...),
    keywords: str = Form(...),
    message: str = Form(...),
    allow_ticket_offer: Optional[str] = Form(None),
    starts_at: Optional[str] = Form(None),
    ends_at: Optional[str] = Form(None),
    _: None = Depends(require_admin),
):
    _check_csrf(request)

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        # Form-validated server side as a safety net for the required-attr.
        raise HTTPException(status_code=400, detail="At least one keyword is required")

    await create_override(
        name=name,
        keywords=kw_list,
        message=message,
        allow_ticket_offer=bool(allow_ticket_offer),
        starts_at=_parse_dt(starts_at),
        ends_at=_parse_dt(ends_at),
    )
    return RedirectResponse("/admin", status_code=303)


@router.get("/overrides/{oid}/edit", response_class=HTMLResponse)
async def override_edit_form(oid: str, _: None = Depends(require_admin)):
    o = await get_override(oid)
    if not o:
        raise HTTPException(status_code=404, detail="Override not found")

    keywords_value = ", ".join(o.get("keywords", []))

    # datetime-local expects YYYY-MM-DDTHH:MM (no seconds, no tz). Strip if present.
    def _dt_value(iso: Optional[str]) -> str:
        if not iso:
            return ""
        try:
            return datetime.fromisoformat(iso).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            return ""

    body = f"""
    <h2>Edit override</h2>
    <p class="dim">{_esc(o.get('name'))}</p>

    <div class="panel">
      <form method="POST" action="/admin/overrides/{_esc(oid)}">
        <label>Name</label>
        <input type="text" name="name" value="{_esc(o.get('name'))}" required>

        <label>Keywords <small class="hint">Comma-separated. Case-insensitive substring match.</small></label>
        <input type="text" name="keywords" value="{_esc(keywords_value)}" required>

        <label>Message</label>
        <textarea name="message" required>{_esc(o.get('message'))}</textarea>

        <label class="inline">
          <input type="checkbox" name="allow_ticket_offer" value="1" {"checked" if o.get('allow_ticket_offer') else ""}>
          Also offer the user a support ticket
        </label>

        <label>Starts at <small class="hint">UTC. Blank = active immediately.</small></label>
        <input type="datetime-local" name="starts_at" value="{_dt_value(o.get('starts_at'))}">

        <label>Ends at <small class="hint">UTC. Blank = no expiry.</small></label>
        <input type="datetime-local" name="ends_at" value="{_dt_value(o.get('ends_at'))}">

        <div style="margin-top: 20px;">
          <button type="submit">Save changes</button>
          <a class="btn secondary" href="/admin">Cancel</a>
        </div>
      </form>
    </div>
    """
    return HTMLResponse(_page("Edit override", body))


@router.post("/overrides/{oid}")
async def override_update(
    oid: str,
    request: Request,
    name: str = Form(...),
    keywords: str = Form(...),
    message: str = Form(...),
    allow_ticket_offer: Optional[str] = Form(None),
    starts_at: Optional[str] = Form(None),
    ends_at: Optional[str] = Form(None),
    _: None = Depends(require_admin),
):
    _check_csrf(request)

    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kw_list:
        raise HTTPException(status_code=400, detail="At least one keyword is required")

    updated = await update_override(
        oid,
        name=name,
        keywords=kw_list,
        message=message,
        allow_ticket_offer=bool(allow_ticket_offer),
        starts_at=_parse_dt(starts_at),
        ends_at=_parse_dt(ends_at),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Override not found")
    return RedirectResponse("/admin", status_code=303)


@router.post("/overrides/{oid}/toggle")
async def override_toggle(
    oid: str,
    request: Request,
    _: None = Depends(require_admin),
):
    _check_csrf(request)
    o = await get_override(oid)
    if not o:
        raise HTTPException(status_code=404, detail="Override not found")
    await update_override(oid, enabled=not o.get("enabled", True))
    return RedirectResponse("/admin", status_code=303)


@router.post("/overrides/{oid}/delete")
async def override_delete(
    oid: str,
    request: Request,
    _: None = Depends(require_admin),
):
    _check_csrf(request)
    await delete_override(oid)
    return RedirectResponse("/admin", status_code=303)


# ─── Stats Dashboard ──────────────────────────────────────────────────────────
# Reads the conversations table (db.py) and renders headline cards, three
# Chart.js graphs, and the most-asked / documentation-gap topic table.
#
# Time window: supports both fixed presets (24h/7d/30d) via ?range= and an
# arbitrary date range via ?from=&to=. The arbitrary range takes precedence
# when both from and to are supplied.

import json as _json


def _resolve_window(
    range_preset: Optional[str],
    from_str: Optional[str],
    to_str: Optional[str],
) -> tuple[datetime, datetime, str, str]:
    """
    Work out the [since, until] UTC window for the dashboard.

    Returns (since, until, bucket, label) where:
      bucket - 'hour' or 'day', chosen so charts have a sensible resolution
      label  - human description of the active window, shown in the UI

    Precedence: an explicit from+to wins; otherwise the preset; default 7d.
    """
    now = datetime.now(timezone.utc)

    # Arbitrary range — both bounds required
    if from_str and to_str:
        try:
            since = datetime.fromisoformat(from_str).replace(tzinfo=timezone.utc)
            until = datetime.fromisoformat(to_str).replace(tzinfo=timezone.utc)
            if until <= since:
                raise ValueError("end before start")
            span_hours = (until - since).total_seconds() / 3600
            bucket = "hour" if span_hours <= 48 else "day"
            label = f"{since:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC"
            return since, until, bucket, label
        except ValueError:
            # fall through to preset on a bad custom range
            pass

    preset = (range_preset or "7d").lower()
    if preset == "24h":
        return now - timedelta(hours=24), now, "hour", "Last 24 hours"
    if preset == "30d":
        return now - timedelta(days=30), now, "day", "Last 30 days"
    # default / "7d"
    return now - timedelta(days=7), now, "day", "Last 7 days"


def _stat_card(label: str, value: str, sub: str = "", tone: str = "") -> str:
    """tone: '' (purple, default), 'orange', or 'yellow' — sets the top accent bar."""
    sub_html = f'<div class="stat-sub">{_esc(sub)}</div>' if sub else ""
    tone_cls = {"orange": " c-orange", "yellow": " c-yellow"}.get(tone, "")
    return f"""
      <div class="stat-card{tone_cls}">
        <div class="stat-value">{_esc(value)}</div>
        <div class="stat-label">{_esc(label)}</div>
        {sub_html}
      </div>
    """


def _barlist(rows: list[tuple[str, int]]) -> str:
    """
    Render a compact horizontal-bar list — used for busiest channels and
    top users. rows is a list of (label, count); bars scale to the max.
    """
    if not rows:
        return '<div class="empty">No data in this window.</div>'
    peak = max((c for _, c in rows), default=1) or 1
    out = []
    for label, count in rows:
        pct = max(3, round(100 * count / peak))
        out.append(f"""
          <div class="barlist-row">
            <div class="barlist-label">{_esc(label)}</div>
            <div class="barlist-track"><div class="barlist-fill" style="width:{pct}%"></div></div>
            <div class="barlist-val">{count:,}</div>
          </div>
        """)
    return f'<div class="barlist">{"".join(out)}</div>'


@router.get("/stats", response_class=HTMLResponse)
async def stats_dashboard(request: Request, _: None = Depends(require_admin)):
    """
    The analytics dashboard. Query params:
      ?range=24h|7d|30d         fixed preset
      ?from=ISO&to=ISO          arbitrary UTC range (overrides preset)

    'from' is a Python keyword so we can't bind it as a function parameter —
    query params are read directly off the Request instead.
    """
    qp = request.query_params
    since, until, bucket, label = _resolve_window(
        qp.get("range"), qp.get("from"), qp.get("to")
    )

    # If stats logging isn't even enabled, say so plainly rather than showing
    # a dashboard full of zeroes that looks like a bug.
    if not db.is_enabled():
        body = """
        <h2>Stats</h2>
        <div class="panel">
          <div class="empty">
            Stats database not connected. Set <code>DATABASE_URL</code> on the
            api service and redeploy — see deploy notes.
          </div>
        </div>
        """
        return HTMLResponse(_page("Stats", body))

    # Fetch everything in parallel — nine independent queries.
    (summary, series, topics, recent,
     top_users, provider_split, latency, busy_hours, busy_channels) = await asyncio.gather(
        db.get_summary(since, until),
        db.get_timeseries(since, until, bucket=bucket),
        db.get_top_topics(since, until, limit=25),
        db.get_recent(since, until, limit=50),
        db.get_top_users(since, until, limit=10),
        db.get_provider_split(since, until),
        db.get_latency_stats(since, until),
        db.get_busiest_hours(since, until),
        db.get_busiest_channels(since, until, limit=8),
    )

    # ── Headline cards ───────────────────────────────────────────────────────
    total       = summary.get("total", 0)
    resolved    = summary.get("resolved", 0)
    rate        = summary.get("resolved_rate", 0.0)
    escalated   = summary.get("escalated", 0)
    tickets     = summary.get("tickets", 0)
    doc_gaps    = summary.get("doc_gaps", 0)
    errors      = summary.get("errors", 0)
    tok_in      = summary.get("tokens_in", 0)
    tok_out     = summary.get("tokens_out", 0)
    tok_total   = tok_in + tok_out
    avg_ms      = latency.get("avg_ms", 0)
    p95_ms      = latency.get("p95_ms", 0)

    def _fmt_ms(ms: int) -> str:
        return f"{ms/1000:.1f}s" if ms >= 1000 else f"{ms}ms"

    cards = (
        _stat_card("Messages handled", f"{total:,}")
        + _stat_card("Resolved by bot", f"{rate:.0f}%", f"{resolved:,} of {total:,}", tone="yellow")
        + _stat_card("Escalated", f"{escalated:,}", "ticket offered to user", tone="orange")
        + _stat_card("Tickets created", f"{tickets:,}", "user accepted the offer", tone="orange")
        + _stat_card("Doc gaps", f"{doc_gaps:,}", "answers with no doc match", tone="orange")
        + _stat_card("Errors", f"{errors:,}", tone="orange" if errors else "")
        + _stat_card("Avg response", _fmt_ms(avg_ms), f"p95 {_fmt_ms(p95_ms)}")
        + _stat_card("Tokens used", f"{tok_total:,}", f"{tok_in:,} in / {tok_out:,} out")
    )

    # ── Chart data — serialize series for Chart.js ──────────────────────────
    # Labels: hour buckets show time, day buckets show date.
    def _fmt_bucket(dt: datetime) -> str:
        return dt.strftime("%m-%d %H:%M") if bucket == "hour" else dt.strftime("%Y-%m-%d")

    chart_labels   = [_fmt_bucket(r["bucket"]) for r in series]
    msg_totals     = [r["total"] for r in series]
    docs_series    = [r["docs"] for r in series]
    override_ser   = [r["override"] for r in series]
    fallback_ser   = [r["fallback"] for r in series]
    escalated_ser  = [r["escalated"] for r in series]
    unresolved_ser = [r["unresolved"] for r in series]
    error_ser      = [r["error"] for r in series]
    tokin_series   = [r["tokens_in"] for r in series]
    tokout_series  = [r["tokens_out"] for r in series]

    chart_data = _json.dumps({
        "labels": chart_labels,
        "msgTotals": msg_totals,
        "docs": docs_series,
        "override": override_ser,
        "fallback": fallback_ser,
        "escalated": escalated_ser,
        "unresolved": unresolved_ser,
        "error": error_ser,
        "tokensIn": tokin_series,
        "tokensOut": tokout_series,
        "hours": [r["count"] for r in busy_hours],
        "providerLabels": [p["provider"] for p in provider_split],
        "providerCounts": [p["count"] for p in provider_split],
    })

    # ── Topics table (most-asked / doc-gap report) ──────────────────────────
    if topics:
        topic_rows = []
        for t in topics:
            asked      = t["asked"]
            gaps       = t["doc_gaps"]
            resolved_t = t["resolved"]
            escalated_t = t["escalated"]
            gap_rate   = (100 * gaps / asked) if asked else 0
            gap_pill = ""
            if gaps > 0 and gap_rate >= 25:
                gap_pill = f'<span class="pill warn">{gap_rate:.0f}% gap</span>'
            elif gaps > 0:
                gap_pill = f'<span class="pill off">{gap_rate:.0f}% gap</span>'
            topic_rows.append(f"""
              <tr>
                <td><strong>{_esc(t['topic'])}</strong></td>
                <td>{asked:,}</td>
                <td>{resolved_t:,}</td>
                <td>{escalated_t:,}</td>
                <td>{gaps:,} {gap_pill}</td>
              </tr>
            """)
        topics_table = f"""
          <table>
            <thead>
              <tr><th>Topic</th><th>Asked</th><th>Resolved</th><th>Escalated</th><th>Doc gaps</th></tr>
            </thead>
            <tbody>{''.join(topic_rows)}</tbody>
          </table>
        """
    else:
        topics_table = '<div class="empty">No conversations recorded in this window yet.</div>'

    # ── Top users (who needed the most help) ────────────────────────────────
    if top_users:
        user_rows = []
        for u in top_users:
            esc_pill = f'<span class="pill warn">{u["escalated"]} escalated</span>' if u["escalated"] else ""
            gap_pill = f'<span class="pill off">{u["doc_gaps"]} gaps</span>' if u["doc_gaps"] else ""
            user_rows.append(f"""
              <tr>
                <td><strong>{_esc(u['who'])}</strong></td>
                <td>{u['interactions']:,}</td>
                <td>{esc_pill} {gap_pill}</td>
              </tr>
            """)
        users_table = f"""
          <table>
            <thead><tr><th>User</th><th>Interactions</th><th>Flags</th></tr></thead>
            <tbody>{''.join(user_rows)}</tbody>
          </table>
        """
    else:
        users_table = '<div class="empty">No user activity in this window.</div>'

    # ── Busiest channels (bar list) ─────────────────────────────────────────
    channel_bars = _barlist([
        (f"#{c['channel_id'][-6:]}", c["count"]) for c in busy_channels
    ])

    # ── Recent activity table ────────────────────────────────────────────────
    if recent:
        recent_rows = []
        for r in recent:
            ts = r["started_at"].strftime("%m-%d %H:%M")
            q = (r["question"] or "")[:70]
            rs = r["response_source"] or ""
            rs_class = {
                "docs": "on", "override": "purple", "escalated": "warn",
                "error": "danger-pill", "unresolved": "off", "fallback": "warn",
            }.get(rs, "off")
            err = f' <span class="hint">{_esc(r["error"])}</span>' if r.get("error") else ""
            recent_rows.append(f"""
              <tr>
                <td class="hint">{ts}</td>
                <td>{_esc(r.get('username') or '—')}</td>
                <td>{_esc(q)}</td>
                <td>{_esc(r.get('topic') or '—')}</td>
                <td><span class="pill {rs_class}">{_esc(rs)}</span>{err}</td>
                <td class="hint">{_esc(r.get('llm_provider') or '—')}</td>
              </tr>
            """)
        recent_table = f"""
          <table>
            <thead>
              <tr><th>Time</th><th>User</th><th>Question</th><th>Topic</th><th>Outcome</th><th>Provider</th></tr>
            </thead>
            <tbody>{''.join(recent_rows)}</tbody>
          </table>
        """
    else:
        recent_table = '<div class="empty">No activity in this window yet.</div>'

    # ── Time-range controls ──────────────────────────────────────────────────
    def _preset_btn(key: str, text: str) -> str:
        active = "active" if (qp.get("range") == key and not (qp.get("from") and qp.get("to"))) else ""
        if not qp.get("range") and not (qp.get("from") and qp.get("to")) and key == "7d":
            active = "active"
        return f'<a class="rangebtn {active}" href="/admin/stats?range={key}">{text}</a>'

    controls = f"""
      <div class="controls">
        <div class="preset-row">
          {_preset_btn("24h", "24 hours")}
          {_preset_btn("7d", "7 days")}
          {_preset_btn("30d", "30 days")}
        </div>
        <form method="GET" action="/admin/stats" class="range-form">
          <input type="datetime-local" name="from" required>
          <span class="hint">to</span>
          <input type="datetime-local" name="to" required>
          <button type="submit" class="secondary">Apply range</button>
        </form>
      </div>
    """

    # ── Chart.js — loaded from CDN ──────────────────────────────────────────
    head_extra = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'

    # Brand palette for charts
    chart_script = f"""
    <script>
      const D = {chart_data};
      const C = {{
        purple: '#805dee', purpleSoft: 'rgba(128,93,238,0.14)',
        orange: '#ff673c', orangeSoft: 'rgba(255,103,60,0.14)',
        yellow: '#ffe143', yellowSoft: 'rgba(255,225,67,0.14)',
        dim: '#9690ad', grid: 'rgba(255,255,255,0.05)',
      }};
      Chart.defaults.color = C.dim;
      Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
      const baseScales = {{
        x: {{ grid: {{ color: C.grid }} }},
        y: {{ grid: {{ color: C.grid }}, beginAtZero: true }}
      }};

      // 1. Messages over time
      new Chart(document.getElementById('msgChart'), {{
        type: 'line',
        data: {{ labels: D.labels, datasets: [{{
          label: 'Messages', data: D.msgTotals,
          borderColor: C.purple, backgroundColor: C.purpleSoft,
          fill: true, tension: 0.35, pointRadius: 2,
        }}] }},
        options: {{ responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }} }}, scales: baseScales }}
      }});

      // 2. Resolution breakdown — stacked bar
      new Chart(document.getElementById('resChart'), {{
        type: 'bar',
        data: {{ labels: D.labels, datasets: [
          {{ label: 'Docs',       data: D.docs,       backgroundColor: '#805dee' }},
          {{ label: 'Override',   data: D.override,   backgroundColor: '#9d80f5' }},
          {{ label: 'Fallback',   data: D.fallback,   backgroundColor: '#ffe143' }},
          {{ label: 'Escalated',  data: D.escalated,  backgroundColor: '#ff673c' }},
          {{ label: 'Unresolved', data: D.unresolved, backgroundColor: '#6b6680' }},
          {{ label: 'Error',      data: D.error,      backgroundColor: '#ff4d6a' }},
        ] }},
        options: {{ responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ position: 'bottom' }} }},
          scales: {{
            x: {{ stacked: true, grid: {{ color: C.grid }} }},
            y: {{ stacked: true, grid: {{ color: C.grid }}, beginAtZero: true }}
          }} }}
      }});

      // 3. Token usage over time
      new Chart(document.getElementById('tokChart'), {{
        type: 'line',
        data: {{ labels: D.labels, datasets: [
          {{ label: 'Tokens in',  data: D.tokensIn,  borderColor: C.purple,
             backgroundColor: C.purpleSoft, fill: true, tension: 0.35, pointRadius: 2 }},
          {{ label: 'Tokens out', data: D.tokensOut, borderColor: C.orange,
             backgroundColor: C.orangeSoft, fill: true, tension: 0.35, pointRadius: 2 }},
        ] }},
        options: {{ responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ position: 'bottom' }} }}, scales: baseScales }}
      }});

      // 4. Busiest hours — bar, 0-23 UTC
      new Chart(document.getElementById('hourChart'), {{
        type: 'bar',
        data: {{
          labels: Array.from({{length: 24}}, (_, h) => h + ':00'),
          datasets: [{{ label: 'Messages', data: D.hours,
            backgroundColor: C.yellow, borderRadius: 3 }}]
        }},
        options: {{ responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }} }}, scales: baseScales }}
      }});

      // 5. Provider split — doughnut
      if (D.providerLabels.length) {{
        new Chart(document.getElementById('provChart'), {{
          type: 'doughnut',
          data: {{
            labels: D.providerLabels,
            datasets: [{{
              data: D.providerCounts,
              backgroundColor: ['#805dee', '#ffe143', '#ff673c', '#6b6680'],
              borderColor: '#1a1824', borderWidth: 2,
            }}]
          }},
          options: {{ responsive: true, maintainAspectRatio: false,
            plugins: {{ legend: {{ position: 'bottom' }} }}, cutout: '62%' }}
        }});
      }}
    </script>
    """

    body = f"""
    <h2>Stats</h2>
    <p class="dim">Showing: <strong>{_esc(label)}</strong></p>

    {controls}

    <div class="stat-grid">
      {cards}
    </div>

    <div class="grid cols-2">
      <div class="panel">
        <h3>Messages over time</h3>
        <div class="chart-wrap"><canvas id="msgChart"></canvas></div>
      </div>
      <div class="panel accent-orange">
        <h3>Resolution breakdown</h3>
        <div class="chart-wrap"><canvas id="resChart"></canvas></div>
      </div>
    </div>

    <div class="grid cols-2">
      <div class="panel">
        <h3>Token usage</h3>
        <div class="chart-wrap"><canvas id="tokChart"></canvas></div>
      </div>
      <div class="panel accent-yellow">
        <h3>Busiest hours (UTC)</h3>
        <div class="chart-wrap"><canvas id="hourChart"></canvas></div>
      </div>
    </div>

    <div class="grid cols-3">
      <div class="panel">
        <h3>LLM provider split</h3>
        <p class="dim">Bankr vs fallback. A big fallback slice means Bankr's been flaky.</p>
        <div class="chart-wrap short"><canvas id="provChart"></canvas></div>
      </div>
      <div class="panel accent-orange">
        <h3>Top users</h3>
        <p class="dim">Who's needed the most help.</p>
        {users_table}
      </div>
      <div class="panel accent-yellow">
        <h3>Busiest channels</h3>
        <p class="dim">Where support load lands.</p>
        {channel_bars}
      </div>
    </div>

    <div class="panel">
      <h3>Most-asked topics &amp; documentation gaps</h3>
      <p class="dim">Topics ranked by volume. A high doc-gap rate flags
      questions the docs don't answer well — the list of things worth writing.</p>
      {topics_table}
    </div>

    <div class="panel">
      <h3>Recent activity</h3>
      {recent_table}
    </div>

    {chart_script}
    """
    return HTMLResponse(_page("Stats", body, head_extra=head_extra))
