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

_BASE_STYLE = """
<style>
  :root {
    --bg: #0f1117;
    --panel: #1a1d27;
    --panel-2: #252934;
    --border: #2d3340;
    --text: #e6e8ee;
    --text-dim: #8b92a3;
    --accent: #7c9eff;
    --accent-2: #5577dd;
    --danger: #e85555;
    --warning: #d4a44a;
    --success: #4caf78;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  header {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; }
  header nav a {
    color: var(--text-dim);
    text-decoration: none;
    margin-left: 20px;
    font-size: 14px;
  }
  header nav a:hover { color: var(--text); }
  main { max-width: 1100px; margin: 32px auto; padding: 0 32px; }
  h2 { font-size: 22px; margin: 0 0 8px; }
  p.dim { color: var(--text-dim); margin: 0 0 24px; }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 24px;
    margin-bottom: 20px;
  }
  .panel h3 { margin-top: 0; font-size: 16px; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    text-align: left;
    padding: 12px 8px;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
  }
  th { color: var(--text-dim); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  tr:last-child td { border-bottom: none; }
  .pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
  }
  .pill.on { background: rgba(76,175,120,0.18); color: var(--success); }
  .pill.off { background: rgba(139,146,163,0.18); color: var(--text-dim); }
  .pill.warn { background: rgba(212,164,74,0.18); color: var(--warning); }
  input[type=text], input[type=password], textarea, input[type=datetime-local] {
    width: 100%;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    color: var(--text);
    font-family: inherit;
    font-size: 14px;
  }
  textarea { min-height: 100px; resize: vertical; }
  label {
    display: block;
    font-size: 13px;
    color: var(--text-dim);
    margin: 12px 0 6px;
  }
  label.inline {
    display: flex;
    align-items: center;
    margin: 16px 0;
    color: var(--text);
    font-size: 14px;
  }
  label.inline input { margin-right: 8px; }
  small.hint { color: var(--text-dim); font-size: 12px; display: block; margin-top: 4px; }
  button, .btn {
    background: var(--accent);
    color: #0a0c12;
    border: none;
    border-radius: 6px;
    padding: 9px 18px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
  }
  button:hover, .btn:hover { background: var(--accent-2); }
  button.danger, .btn.danger { background: var(--danger); color: #fff; }
  button.secondary, .btn.secondary {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border);
  }
  .row-actions form { display: inline-block; margin-right: 6px; }
  .keywords-list {
    display: flex; flex-wrap: wrap; gap: 4px;
  }
  .kw-chip {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 12px;
    color: var(--text-dim);
    font-family: "SF Mono", Consolas, monospace;
  }
  .flash {
    padding: 12px 16px;
    border-radius: 6px;
    margin-bottom: 20px;
    font-size: 14px;
  }
  .flash.error { background: rgba(232,85,85,0.15); border: 1px solid rgba(232,85,85,0.4); color: #ffb0b0; }
  .empty { text-align: center; padding: 40px; color: var(--text-dim); }

  /* ── Stats dashboard ── */
  .pill.danger-pill { background: rgba(232,85,85,0.18); color: var(--danger); }
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }
  .stat-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 18px;
  }
  .stat-value { font-size: 28px; font-weight: 700; color: var(--text); }
  .stat-label { font-size: 13px; color: var(--text-dim); margin-top: 2px; }
  .stat-sub   { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
  .chart-wrap { position: relative; height: 280px; margin-top: 8px; }
  .controls {
    display: flex; flex-wrap: wrap; gap: 16px;
    align-items: center; justify-content: space-between;
    margin-bottom: 24px;
  }
  .preset-row { display: flex; gap: 8px; }
  .rangebtn {
    padding: 7px 14px;
    border-radius: 6px;
    border: 1px solid var(--border);
    color: var(--text-dim);
    text-decoration: none;
    font-size: 13px;
  }
  .rangebtn:hover { color: var(--text); }
  .rangebtn.active {
    background: var(--accent);
    color: #0a0c12;
    border-color: var(--accent);
    font-weight: 600;
  }
  .range-form { display: flex; gap: 8px; align-items: center; }
  .range-form input[type=datetime-local] { width: auto; }
  code {
    background: var(--panel-2);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
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
    <h1>Gamal Admin</h1>
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

    body = f"""
    <h2>Bot Controls</h2>
    <p class="dim">
      Operational toggles and response overrides for the Gamal support bot.
    </p>

    <div class="panel">
      <h3>Busy mode &nbsp; {busy_status_pill}</h3>
      <p class="dim">
        When busy mode is on, the bot stops sending <strong>passive proactive
        offers</strong> to members holding the staff roles below. Use it during
        high-traffic windows so the bot doesn't jump in on your support team's
        messages while they're helping customers live. Staff can still
        <strong>@mention</strong> the bot directly — only the uninvited
        proactive path is suppressed.
      </p>
      <form method="POST" action="/admin/settings/busy-mode">
        <label class="inline">
          <input type="checkbox" name="busy_mode_enabled" value="1" {"checked" if busy_on else ""}>
          Enable busy mode
        </label>

        <label>Staff roles to ignore on the passive path
          <small class="hint">Comma-separated Discord role names. Case-insensitive. e.g. Moderator, Support, Admin</small>
        </label>
        <input type="text" name="busy_mode_roles" value="{_esc(busy_roles_value)}"
               placeholder="Moderator, Support">

        <div style="margin-top: 16px;">
          <button type="submit">Save busy mode</button>
        </div>
      </form>
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


def _stat_card(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="stat-sub">{_esc(sub)}</div>' if sub else ""
    return f"""
      <div class="stat-card">
        <div class="stat-value">{_esc(value)}</div>
        <div class="stat-label">{_esc(label)}</div>
        {sub_html}
      </div>
    """


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

    # Fetch everything in parallel — four independent queries.
    summary, series, topics, recent = await asyncio.gather(
        db.get_summary(since, until),
        db.get_timeseries(since, until, bucket=bucket),
        db.get_top_topics(since, until, limit=25),
        db.get_recent(since, until, limit=50),
    )

    # ── Headline cards ───────────────────────────────────────────────────────
    total       = summary.get("total", 0)
    resolved    = summary.get("resolved", 0)
    rate        = summary.get("resolved_rate", 0.0)
    tickets     = summary.get("tickets", 0)
    doc_gaps    = summary.get("doc_gaps", 0)
    errors      = summary.get("errors", 0)
    tok_in      = summary.get("tokens_in", 0)
    tok_out     = summary.get("tokens_out", 0)
    tok_total   = tok_in + tok_out

    cards = (
        _stat_card("Messages handled", f"{total:,}")
        + _stat_card("Resolved by bot", f"{rate:.0f}%", f"{resolved:,} of {total:,}")
        + _stat_card("Escalated to ticket", f"{tickets:,}")
        + _stat_card("Doc gaps", f"{doc_gaps:,}", "answers with no doc match")
        + _stat_card("Errors", f"{errors:,}")
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
    })

    # ── Topics table (most-asked / doc-gap report) ──────────────────────────
    if topics:
        topic_rows = []
        for t in topics:
            asked     = t["asked"]
            gaps      = t["doc_gaps"]
            resolved_t = t["resolved"]
            escalated_t = t["escalated"]
            gap_rate  = (100 * gaps / asked) if asked else 0
            # Highlight topics with a high doc-gap rate — these are the
            # documentation holes worth filling.
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

    # ── Recent activity table ────────────────────────────────────────────────
    if recent:
        recent_rows = []
        for r in recent:
            ts = r["started_at"].strftime("%m-%d %H:%M")
            q = (r["question"] or "")[:80]
            rs = r["response_source"] or ""
            rs_class = {
                "docs": "on", "override": "warn", "escalated": "warn",
                "error": "danger-pill", "unresolved": "off", "fallback": "warn",
            }.get(rs, "off")
            err = f' <span class="hint">{_esc(r["error"])}</span>' if r.get("error") else ""
            recent_rows.append(f"""
              <tr>
                <td class="hint">{ts}</td>
                <td>{_esc(q)}</td>
                <td>{_esc(r.get('topic') or '—')}</td>
                <td><span class="pill {rs_class}">{_esc(rs)}</span>{err}</td>
                <td class="hint">{_esc(r.get('llm_provider') or '—')}</td>
                <td class="hint">{(r.get('tokens_in') or 0) + (r.get('tokens_out') or 0):,}</td>
              </tr>
            """)
        recent_table = f"""
          <table>
            <thead>
              <tr><th>Time</th><th>Question</th><th>Topic</th><th>Outcome</th><th>Provider</th><th>Tokens</th></tr>
            </thead>
            <tbody>{''.join(recent_rows)}</tbody>
          </table>
        """
    else:
        recent_table = '<div class="empty">No activity in this window yet.</div>'

    # ── Time-range controls ──────────────────────────────────────────────────
    def _preset_btn(key: str, text: str) -> str:
        active = "active" if (qp.get("range") == key and not (qp.get("from") and qp.get("to"))) else ""
        # default 7d is active when no params at all
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

    # ── Chart.js — loaded from CDN, init script at end ──────────────────────
    head_extra = '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>'

    chart_script = f"""
    <script>
      const D = {chart_data};
      const gridColor = 'rgba(255,255,255,0.06)';
      const tickColor = '#8b92a3';
      Chart.defaults.color = tickColor;
      Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

      // 1. Messages over time
      new Chart(document.getElementById('msgChart'), {{
        type: 'line',
        data: {{
          labels: D.labels,
          datasets: [{{
            label: 'Messages',
            data: D.msgTotals,
            borderColor: '#7c9eff',
            backgroundColor: 'rgba(124,158,255,0.12)',
            fill: true, tension: 0.3, pointRadius: 2,
          }}]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }} }},
          scales: {{
            x: {{ grid: {{ color: gridColor }} }},
            y: {{ grid: {{ color: gridColor }}, beginAtZero: true }}
          }}
        }}
      }});

      // 2. Resolution breakdown — stacked bar
      new Chart(document.getElementById('resChart'), {{
        type: 'bar',
        data: {{
          labels: D.labels,
          datasets: [
            {{ label: 'Docs',       data: D.docs,       backgroundColor: '#4caf78' }},
            {{ label: 'Override',   data: D.override,   backgroundColor: '#7c9eff' }},
            {{ label: 'Fallback',   data: D.fallback,   backgroundColor: '#5577dd' }},
            {{ label: 'Escalated',  data: D.escalated,  backgroundColor: '#d4a44a' }},
            {{ label: 'Unresolved', data: D.unresolved, backgroundColor: '#8b92a3' }},
            {{ label: 'Error',      data: D.error,      backgroundColor: '#e85555' }},
          ]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ position: 'bottom' }} }},
          scales: {{
            x: {{ stacked: true, grid: {{ color: gridColor }} }},
            y: {{ stacked: true, grid: {{ color: gridColor }}, beginAtZero: true }}
          }}
        }}
      }});

      // 3. Token usage over time
      new Chart(document.getElementById('tokChart'), {{
        type: 'line',
        data: {{
          labels: D.labels,
          datasets: [
            {{ label: 'Tokens in',  data: D.tokensIn,  borderColor: '#7c9eff',
               backgroundColor: 'rgba(124,158,255,0.10)', fill: true, tension: 0.3, pointRadius: 2 }},
            {{ label: 'Tokens out', data: D.tokensOut, borderColor: '#4caf78',
               backgroundColor: 'rgba(76,175,120,0.10)', fill: true, tension: 0.3, pointRadius: 2 }},
          ]
        }},
        options: {{
          responsive: true, maintainAspectRatio: false,
          plugins: {{ legend: {{ position: 'bottom' }} }},
          scales: {{
            x: {{ grid: {{ color: gridColor }} }},
            y: {{ grid: {{ color: gridColor }}, beginAtZero: true }}
          }}
        }}
      }});
    </script>
    """

    body = f"""
    <h2>Stats</h2>
    <p class="dim">Showing: <strong>{_esc(label)}</strong></p>

    {controls}

    <div class="stat-grid">
      {cards}
    </div>

    <div class="panel">
      <h3>Messages over time</h3>
      <div class="chart-wrap"><canvas id="msgChart"></canvas></div>
    </div>

    <div class="panel">
      <h3>Resolution breakdown</h3>
      <p class="dim">How each message was handled. A growing escalated/error
      slice is the early warning that something needs attention.</p>
      <div class="chart-wrap"><canvas id="resChart"></canvas></div>
    </div>

    <div class="panel">
      <h3>Token usage</h3>
      <div class="chart-wrap"><canvas id="tokChart"></canvas></div>
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
