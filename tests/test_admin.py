"""
The admin panel's gate, under the real web framework and a real Postgres and Redis:
who may sign in, who may change what the bot says, that a change must come from the panel's own pages, that every
change is written down and raises an alert, and that the panel can be tied to its own hostname.

    python tests/test_admin.py          (DATABASE_URL and REDIS_URL set; the stack checks run it inside the api container)

It builds its own small app around the real admin router, so the docs index is not loaded. Sessions are minted with
the test's own signing secret, exactly as a Google sign-in mints them. Everything it writes it removes.
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

os.environ.setdefault("ADMIN_SESSION_SECRET", "test-session-secret")
os.environ.update({"GOOGLE_OAUTH_CLIENT_ID": "x", "GOOGLE_OAUTH_CLIENT_SECRET": "y", "ADMIN_PASSWORD_LOGIN": "", "ALERT_WEBHOOK_URL": ""})
for k in ("ADMIN_ALLOWED_EMAILS", "ADMIN_EDITORS", "ADMIN_ALLOWED_HOSTS"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi import FastAPI                    # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402
import admin_routes as ar                      # noqa: E402
import alerts                                  # noqa: E402
import db                                      # noqa: E402

sent: list[str] = []
alerts.alert = lambda text, key="", every=0: (sent.append(text), True)[1]   # capture instead of sending


@asynccontextmanager
async def lifespan(app):
    await db.init_db()
    yield
    await db.close_db()

app = FastAPI(lifespan=lifespan)
app.include_router(ar.router)
failed = 0


def check(name, ok, detail=""):
    global failed
    print(("ok    " if ok else "FAIL  ") + name + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        failed += 1


def cookie(email, via="google"):
    return {ar.SESSION_COOKIE_NAME: ar._serializer.dumps({"login_at": datetime.now(timezone.utc).isoformat(), "email": email, "via": via})}


HOST = "gamal.cloud"
OWN = {"origin": f"https://{HOST}", "accept": "application/json"}
ALICE, BOB, EVE = "alice@bankr.bot", "bob@bankr.bot", "eve@bankr.bot"
OVERRIDE = {"name": "test-admin-override", "keywords": "zzqx-test", "message": "This is a TEST override text."}


def actions(c, who):
    return [(a["action"], a["detail"] or "") for a in asyncio.run(_actions()) if a["email"] == who]


async def _actions():
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return [dict(r) for r in await conn.fetch("select email, action, detail from admin_actions where email like '%@bankr.bot' order by id")]
    finally:
        await conn.close()


async def _cleanup():
    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute("delete from admin_actions where email = any($1::text[])", [ALICE, BOB, EVE])
    finally:
        await conn.close()


with TestClient(app, base_url=f"https://{HOST}", follow_redirects=False) as c:
    # ── no session ──
    r = c.get("/admin", headers={"accept": "application/json"})
    check("without a session the panel refuses", r.status_code == 401)
    r = c.post("/admin/overrides", data=OVERRIDE, headers=OWN)
    check("without a session a change is refused", r.status_code == 401)
    r = c.get("/admin", headers={"accept": "application/json"}, cookies=cookie(ALICE, via="password"))
    check("a session from the shared password is refused while password sign-in is off", r.status_code == 401)

    # ── no access list: as before, any company account (and the panel says so) ──
    r = c.get("/admin/actions", headers={"accept": "application/json"}, cookies=cookie(ALICE))
    check("with no access list any company account gets in, and the Actions page warns about it", r.status_code == 200 and "no access list is set" in r.text)

    # ── access list ──
    ar.ADMIN_ALLOWED_EMAILS = [ALICE, BOB]
    r = c.get("/admin/actions", headers={"accept": "application/json"}, cookies=cookie(EVE))
    check("an account not on the access list is refused, though it is on the company domain", r.status_code == 401)
    r = c.get("/admin/actions", headers={"accept": "application/json"}, cookies=cookie(ALICE))
    check("an account on the list gets in", r.status_code == 200 and "2 named accounts may sign in" in r.text, r.text[:200])
    ar.ADMIN_ALLOWED_EMAILS = [ALICE]
    r = c.get("/admin/actions", headers={"accept": "application/json"}, cookies=cookie(BOB))
    check("taking someone off the list ends their session at their next request", r.status_code == 401)
    ar.ADMIN_ALLOWED_EMAILS = [ALICE, BOB]

    # ── a change must come from the panel's own pages ──
    r = c.post("/admin/overrides", data=OVERRIDE, headers={"accept": "application/json"}, cookies=cookie(ALICE))
    check("a change with no Origin or Referer is refused", r.status_code == 403)
    r = c.post("/admin/overrides", data=OVERRIDE, headers={"origin": "https://evil.example", "accept": "application/json"}, cookies=cookie(ALICE))
    check("a change sent from another site is refused", r.status_code == 403)
    r = c.post("/admin/article-sync/pin", data={}, headers={"origin": "https://evil.example", "accept": "application/json"}, cookies=cookie(ALICE))
    check("so is one to a route that never checked before (article sync)", r.status_code == 403)
    r = c.post("/admin/overrides", data=OVERRIDE, headers={"referer": f"https://{HOST}/admin", "accept": "application/json"}, cookies=cookie(ALICE))
    check("a change from the panel's own page goes through (Referer)", r.status_code == 303, f"{r.status_code} {r.text[:200]}")

    # ── it is written down, with its text, and an alert goes out ──
    mine = actions(c, ALICE)
    check("the change is on record under the person's name, with what the bot will now say", any(a == "override.create" and OVERRIDE["message"] in d and "zzqx-test" in d for a, d in mine), str(mine))
    check("and an alert went out with the same", any(ALICE in s and "override.create" in s and OVERRIDE["message"] in s for s in sent), str(sent))
    check("a refused change left no record", not any(a == "override.create" for a, _ in actions(c, EVE)) and len([1 for a, _ in mine if a == "override.create"]) == 1)

    # ── editors ──
    ar.ADMIN_EDITORS = [ALICE]
    # The override's id, read from the panel's own page (the row that carries its name).
    import re
    page = c.get("/admin", headers={"accept": "text/html"}, cookies=cookie(ALICE)).text
    at = page.find(OVERRIDE["name"])
    found = re.search(r"/admin/overrides/([A-Za-z0-9_-]+)/", page[at:]) if at >= 0 else None
    check("the new override is listed on the panel", bool(found), page[max(0, at - 200):at + 300] if at >= 0 else "name not on the page")
    oid = found.group(1) if found else "missing"
    r = c.post(f"/admin/overrides/{oid}/toggle", headers=OWN, cookies=cookie(BOB))
    check("someone who may sign in but is not an editor cannot change an override", r.status_code == 403 and "view the panel" in r.text, f"{r.status_code} {r.text[:120]}")
    r = c.post("/admin/settings/busy-mode", data={"busy_mode_roles": "Moderator"}, headers=OWN, cookies=cookie(BOB))
    check("nor a setting", r.status_code == 403)
    r = c.get("/admin/actions", headers={"accept": "application/json"}, cookies=cookie(BOB))
    check("but can still look", r.status_code == 200)
    sent.clear()
    r = c.post(f"/admin/overrides/{oid}/toggle", headers=OWN, cookies=cookie(ALICE))
    check("an editor can", r.status_code == 303)
    r = c.post(f"/admin/overrides/{oid}/delete", headers=OWN, cookies=cookie(ALICE))
    mine = actions(c, ALICE)
    check("turning an override off and deleting it are both on record with its text", [a for a, _ in mine][-2:] == ["override.toggle", "override.delete"] and OVERRIDE["message"] in mine[-1][1], str(mine[-2:]))
    check("and both raised an alert", len(sent) == 2 and "override.toggle" in sent[0] and "override.delete" in sent[1], str(sent))
    check("the non-editor's refused attempts are not on record as done", not actions(c, BOB))

    # ── opening a conversation is recorded; looking at a list is not ──
    before = len(actions(c, ALICE))
    c.get("/admin/conversations/some-session-id", headers={"accept": "text/html"}, cookies=cookie(ALICE))
    c.get("/admin/stats", headers={"accept": "text/html"}, cookies=cookie(ALICE))
    mine = actions(c, ALICE)
    check("opening one conversation is recorded; a stats page is not", len(mine) == before + 1 and mine[-1] == ("conversation.view", "some-session-id"), str(mine[before:]))

    # ── the panel tied to its hostname ──
    ar.ADMIN_ALLOWED_HOSTS = [HOST]
    r = c.get("/admin/login", headers={"host": "api-production-7c8f.up.railway.app"})
    check("on any other hostname the panel is a 404, sign-in page included", r.status_code == 404)
    r = c.get("/admin/actions", headers={"host": "api-production-7c8f.up.railway.app", "accept": "application/json"}, cookies=cookie(ALICE))
    check("even with a good session", r.status_code == 404)
    r = c.get("/admin/login")
    check("on its own hostname it answers", r.status_code == 200)
    check("the sign-in page offers no password field while password sign-in is off", 'type="password"' not in r.text)
    ar.ADMIN_ALLOWED_HOSTS = []

asyncio.run(_cleanup())
print("\n" + (f"{failed} FAILED" if failed else "all passed"))
sys.exit(1 if failed else 0)
