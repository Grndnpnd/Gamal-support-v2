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
</style>
"""


def _page(title: str, body: str, show_nav: bool = True) -> str:
    nav = ""
    if show_nav:
        nav = """
        <nav>
          <a href="/admin">Overrides</a>
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
