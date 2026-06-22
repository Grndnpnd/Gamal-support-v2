"""
webhook_server.py
-----------------
A lightweight aiohttp HTTP server that:
  1. Receives Plain webhook POST requests (thread reply events)
  2. Looks up which Discord thread the Plain thread is linked to
  3. Posts the agent's reply into the Discord thread

Run this alongside bot.py — shares the Plain ↔ Discord thread map
via Redis (REDIS_URL env var). Falls back to in-memory dict for local dev
but in production both services must share the same Redis instance.

Plain webhook setup:
  - Go to Settings → Webhooks in Plain
  - Add a new webhook target pointing to:  https://YOUR_HOST/plain-webhook
  - Select event:  thread.thread_reply_created  (or all thread events)

ENV vars used (add to your .env):
  WEBHOOK_PORT          - port to listen on (default: 8080)
  DISCORD_TOKEN         - reused from main bot env
  PLAIN_WEBHOOK_SECRET  - shared HMAC secret from Plain workspace.
                          Set in Plain: Settings → Request signing.
                          If set, all incoming webhooks must be signed
                          with this secret or they are rejected (403).
                          If blank, signature verification is skipped
                          and a warning is logged on startup.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os

import aiohttp
import discord
from aiohttp import web
from dotenv import load_dotenv
from redis_map import (
    get_discord_thread_id,
    is_using_redis,
    get_user_for_thread,
    delete_active_ticket,
)
import db

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
WEBHOOK_PORT    = int(os.getenv("WEBHOOK_PORT", "8080"))
PLAIN_WEBHOOK_SECRET = os.getenv("PLAIN_WEBHOOK_SECRET", "")

# Thread map is shared via Redis — see redis_map.py


# ─── Discord Client (minimal, just for sending messages) ─────────────────────

discord_client: discord.Client = None


async def send_discord_message(discord_thread_id: int, content: str):
    """Post a message into a Discord thread channel."""
    try:
        channel = discord_client.get_channel(discord_thread_id)
        if channel is None:
            channel = await discord_client.fetch_channel(discord_thread_id)
        if channel:
            await channel.send(content)
            log.info(f"Posted Plain reply to Discord thread {discord_thread_id}")
        else:
            log.error(f"Discord thread {discord_thread_id} not found")
    except Exception as e:
        log.error(f"Failed to send Discord message: {e}")


# ─── Thread Deletion ─────────────────────────────────────────────────────────

# Tracks threads pending deletion so the bot process can cancel via !keep
# Key: discord_thread_id (int)  Value: asyncio.Task
_pending_deletions: dict[int, asyncio.Task] = {}


async def schedule_thread_deletion(discord_thread_id: int, triggered_by: str = "Plain"):
    """
    Posts a 10-second countdown in the Discord thread then deletes it.
    The bot.py process handles !keep cancellation via _pending_deletions —
    when bot.py sees !keep it calls cancel_thread_deletion() below.
    """
    async def _do_delete():
        try:
            channel = discord_client.get_channel(discord_thread_id)
            if channel is None:
                channel = await discord_client.fetch_channel(discord_thread_id)
            if not channel:
                log.warning(f"Thread {discord_thread_id} not found for deletion")
                return

            await channel.send(
                "✅ **Your support ticket has been resolved by the team.**\n\n"
                "🗑️ This thread will be **deleted in 10 seconds**. "
                "Reply `!keep` to cancel."
            )

            await asyncio.sleep(10)

            try:
                await channel.delete()
                log.info(f"Deleted ticket thread {discord_thread_id} (triggered by {triggered_by})")
            except discord.NotFound:
                log.info(f"Thread {discord_thread_id} already gone")
            except discord.Forbidden:
                log.warning(f"No permission to delete thread {discord_thread_id}")
            except asyncio.CancelledError:
                try:
                    await channel.send("✅ Deletion cancelled. This thread will stay open.")
                except Exception:
                    pass
        except asyncio.CancelledError:
            log.info(f"Thread {discord_thread_id} deletion cancelled via !keep")
            try:
                channel = discord_client.get_channel(discord_thread_id)
                if channel:
                    await channel.send("✅ Deletion cancelled. This thread will stay open.")
            except Exception:
                pass
        except Exception as e:
            log.error(f"Error during thread deletion for {discord_thread_id}: {e}")
        finally:
            _pending_deletions.pop(discord_thread_id, None)

    task = asyncio.ensure_future(_do_delete())
    _pending_deletions[discord_thread_id] = task


def cancel_thread_deletion(discord_thread_id: int) -> bool:
    """
    Called externally (or via a webhook endpoint) to cancel a pending deletion.
    Returns True if a pending deletion was found and cancelled.
    Note: In the Railway deployment, bot.py and webhook_server.py are separate
    processes so this only works within the webhook_server process itself.
    For cross-process cancellation, bot.py's own _deletion_tasks dict handles
    !keep for user-triggered closes; Plain-triggered closes use this.
    """
    task = _pending_deletions.get(discord_thread_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


# ─── Idempotency cache ───────────────────────────────────────────────────────
# ─── Webhook idempotency ──────────────────────────────────────────────────────
#
# Plain delivers webhooks "at least once" — it retries if it doesn't get a
# timely 200, and a retry can arrive long after the original (observed: ~90
# minutes later, across a service redeploy). Without dedupe, a retry of an
# already-handled event posts the agent's reply to Discord a second time.
#
# Dedupe MUST be backed by Redis, not an in-memory set: the webhook service
# restarts on every deploy, and an in-memory set is wiped on restart — so a
# retry arriving after a redeploy would not be recognized as a duplicate.
# Redis is a separate service and survives the redeploy. Keys carry a TTL so
# they self-expire; no manual eviction needed.
#
# If Redis is unavailable, dedupe degrades to "off" (returns not-a-duplicate)
# rather than blocking webhook processing — a rare double-post is better than
# dropping agent replies entirely.

import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "")
# How long a processed event ID is remembered. Must comfortably exceed Plain's
# longest retry window — 7 days is generous and the data (just IDs) is tiny.
_DEDUPE_TTL_SECONDS = 7 * 86400
_DEDUPE_KEY_PREFIX = "webhook:seen:"

_dedupe_redis = None


def _get_dedupe_redis():
    """Lazy-init Redis client for webhook dedupe. None if REDIS_URL unset."""
    global _dedupe_redis
    if _dedupe_redis is not None:
        return _dedupe_redis
    if not REDIS_URL:
        return None
    try:
        _dedupe_redis = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        return _dedupe_redis
    except Exception as e:
        log.error(f"Webhook dedupe: Redis connection failed: {e}")
        return None


async def _already_relayed(event_id: str) -> bool:
    """
    Return True if this Plain event has already been processed (read-only).

    This is a pure check — it does NOT claim the event. Claiming happens in
    _mark_relayed, called only once the event has actually been handled
    (posted to Discord, or deliberately skipped). Keeping check and claim
    separate matters: an event that bails early because no Discord thread is
    mapped yet must NOT be marked seen — Plain may retry it later once the
    thread exists, and that retry needs to go through.

    On any Redis error, returns False (treat as not-duplicate) so webhook
    processing is never blocked by a dedupe-store outage.
    """
    if not event_id:
        return False
    r = _get_dedupe_redis()
    if r is None:
        return False  # dedupe unavailable — process the event
    try:
        return await r.exists(f"{_DEDUPE_KEY_PREFIX}{event_id}") == 1
    except Exception as e:
        log.error(f"Webhook dedupe check failed for {event_id}: {e} — processing anyway")
        return False


async def _mark_relayed(event_id: str):
    """
    Record that this event has been fully handled, so a later Plain retry of
    the same event is recognized as a duplicate and skipped.

    Called only after a real outcome — a successful Discord post, or a
    deliberate skip (echo-loop / customer event). NOT called when the handler
    bails early on a missing thread mapping, so such events stay eligible for
    a future retry once the mapping exists.

    Best-effort: a Redis failure here just means a future retry might
    double-post, which is acceptable.
    """
    if not event_id:
        return
    r = _get_dedupe_redis()
    if r is None:
        return
    try:
        await r.set(
            f"{_DEDUPE_KEY_PREFIX}{event_id}", "1",
            ex=_DEDUPE_TTL_SECONDS,
        )
    except Exception as e:
        log.error(f"Webhook dedupe mark failed for {event_id}: {e}")


# ─── Content-level dedupe ───────────────────────────────────────────────────
#
# Separate from event-id dedupe above, and additive. Event-id dedupe catches
# Plain's own retries of the SAME event (Plain re-delivering pEv_X if it
# didn't get a timely 200). Content dedupe catches a DIFFERENT failure mode:
# Plain emitting two distinct events (different event IDs) that contain the
# same logical message.
#
# Observed in production 2026-06-05: Plain's AI-agent feature posting one
# reply emits two thread.email_sent events, ~6s apart, with different event
# IDs. Event-id dedupe correctly treats them as distinct events; content
# dedupe is what catches that they're semantically duplicates.
#
# Key: SHA-256 of (thread_id || message_text). We deliberately do NOT include
# the agent name in the key — if the same content arrives "from" different
# agents in quick succession, it's almost certainly the same logical event
# re-delivered with different metadata, not two agents genuinely typing the
# same thing.
#
# TTL: 30 minutes. Originally 60s, set against an observed ~6s double-emit
# pattern. Production logs later showed a SECOND duplication pattern: Plain's
# AI agent re-emits its previous reply verbatim ~20-22 minutes after the
# original (suspected inactivity follow-up flow on Plain's side that fires
# even when there's nothing new to say). 30 minutes gives ~8 min of margin
# over the worst observed gap. Tradeoff: an agent legitimately re-sending
# the byte-identical text within 30 minutes gets swallowed — in practice
# this doesn't happen, since humans paraphrase and the only thing we've
# seen send identical text is the misbehaving AI agent.

import hashlib as _hashlib

_CONTENT_DEDUPE_KEY_PREFIX = "webhook:content:"
_CONTENT_DEDUPE_TTL_SECONDS = 1800


def _content_dedupe_key(thread_id: str, message_text: str) -> str:
    """Stable key for a (thread, message) tuple. SHA-256 to keep keys short."""
    h = _hashlib.sha256()
    h.update(thread_id.encode("utf-8"))
    h.update(b"\x00")  # separator so different splits don't collide
    h.update(message_text.encode("utf-8"))
    return _CONTENT_DEDUPE_KEY_PREFIX + h.hexdigest()


async def _content_already_relayed(thread_id: str, message_text: str) -> bool:
    """
    Return True if a message with the same (thread, content) has been
    relayed in the last _CONTENT_DEDUPE_TTL_SECONDS. Read-only.

    Like _already_relayed, this does NOT claim — claiming happens in
    _mark_content_relayed, called only after the event is fully handled.

    On any Redis error, returns False (treat as not-duplicate) so a dedupe-
    store hiccup never blocks legitimate relays.
    """
    if not thread_id or not message_text:
        return False
    r = _get_dedupe_redis()
    if r is None:
        return False
    try:
        return await r.exists(_content_dedupe_key(thread_id, message_text)) == 1
    except Exception as e:
        log.error(f"Content dedupe check failed: {e} — processing anyway")
        return False


async def _mark_content_relayed(thread_id: str, message_text: str) -> None:
    """
    Record that a message with this (thread, content) was just relayed, so a
    subsequent event carrying the same content within the TTL window is
    recognized as a duplicate.

    Best-effort: a Redis failure here just means a possible double-post if
    the duplicate event arrives in the next few seconds — acceptable.
    """
    if not thread_id or not message_text:
        return
    r = _get_dedupe_redis()
    if r is None:
        return
    try:
        await r.set(
            _content_dedupe_key(thread_id, message_text),
            "1",
            ex=_CONTENT_DEDUPE_TTL_SECONDS,
        )
    except Exception as e:
        log.error(f"Content dedupe mark failed: {e}")


# ─── Signature Verification ──────────────────────────────────────────────────
#
# Plain signs outbound webhook requests with HMAC-SHA256 using a workspace-level
# shared secret. The signature is the hex digest of the raw request body. It
# arrives in the `Plain-Request-Signature` header.
#
# Reference: https://www.plain.com/docs/request-signing
#
# We compare using hmac.compare_digest to avoid timing attacks. If
# PLAIN_WEBHOOK_SECRET is not set, verification is skipped entirely — useful
# for local dev where you may be hitting the endpoint with curl, but a warning
# is logged on startup so it's obvious in production logs.

PLAIN_SIGNATURE_HEADER = "Plain-Request-Signature"


def _verify_plain_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Verify the Plain-Request-Signature header against the raw request body.

    Returns True if the signature is valid OR if no secret is configured
    (signature verification disabled).  Returns False only when a secret IS
    configured and the signature is missing or doesn't match.
    """
    if not PLAIN_WEBHOOK_SECRET:
        # Verification disabled — accept everything. Startup logs flag this.
        return True

    if not signature_header:
        log.warning("Webhook missing Plain-Request-Signature header — rejecting")
        return False

    expected = hmac.new(
        PLAIN_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        log.warning("Webhook signature mismatch — rejecting")
        return False

    return True


# ─── Webhook Handler ──────────────────────────────────────────────────────────

async def handle_plain_webhook(request: web.Request) -> web.Response:
    """
    Handle incoming Plain webhook events.
    Only relays messages from human agents (actorType: user).
    Skips customer and machineUser events to prevent echo loops.
    Uses event ID deduplication to prevent duplicate posts on Plain retries.
    """
    # Read the raw body bytes first — needed for HMAC signature verification.
    # Calling request.json() would consume the body and we'd lose the raw form.
    try:
        raw_body = await request.read()
    except Exception as e:
        log.warning(f"Failed to read webhook body: {e}")
        return web.Response(status=400, text="Bad Request")

    # Verify the Plain-Request-Signature header before doing any work.
    signature_header = request.headers.get(PLAIN_SIGNATURE_HEADER)
    if not _verify_plain_signature(raw_body, signature_header):
        return web.Response(status=403, text="Forbidden")

    try:
        body = json.loads(raw_body)
    except Exception:
        log.warning("Received non-JSON webhook body")
        return web.Response(status=400, text="Bad Request")

    event_type = body.get("type", "")
    event_id   = body.get("id", "")
    log.info(f"Plain webhook received: {event_type} (id={event_id})")

    # Deduplication — Plain retries webhooks if it doesn't get a timely 200,
    # and retries survive service redeploys. Redis-backed; see _already_relayed.
    if event_id and await _already_relayed(event_id):
        log.info(f"Duplicate event {event_id} — already relayed, skipping")
        return web.Response(status=200, text="OK")

    # Events we care about:
    #   thread.chat_sent       — agent sent a chat message in Plain
    #   thread.email_sent      — agent sent an email reply in Plain
    #   thread.thread_status_transitioned — ticket resolved/done
    if event_type not in (
        "thread.chat_sent",
        "thread.email_sent",
        "thread.thread_status_transitioned",
    ):
        return web.Response(status=200, text="OK")

    payload = body.get("payload", {})
    thread_id = payload.get("thread", {}).get("id")

    if not thread_id:
        log.warning(f"Webhook payload missing thread ID. Full payload: {payload}")
        return web.Response(status=200, text="OK")

    # ── Handle status transition (ticket resolved) ────────────────────────────
    if event_type == "thread.thread_status_transitioned":
        new_status = payload.get("nextStatus", "") or payload.get("status", "")
        if new_status.upper() in ("DONE", "RESOLVED"):
            discord_thread_id = await get_discord_thread_id(thread_id)
            if discord_thread_id:
                # Clean up the active-ticket Redis hashes before deleting the
                # Discord thread. This used to happen ONLY when the user typed
                # !close in Discord (bot.py owns that path and calls
                # close_ticket → delete_active_ticket). The Plain-side resolve
                # path was deleting the Discord thread but leaving the
                # user_tickets / active_tickets / ticket_customers hashes
                # populated forever, so when the same user came back later the
                # bot's user_has_open_ticket() returned a dead thread ID and
                # tried to forward to <#deleted>, rendering as ⁠unknown in
                # Discord. Fixed 2026-06-10 — clean state on this path too.
                #
                # We look up the user_id from the user_tickets hash (inverse
                # of the normal lookup) because the webhook only knows the
                # Plain thread id and the Discord thread id, not which user
                # owns the ticket.
                user_id = await get_user_for_thread(int(discord_thread_id))
                if user_id is not None:
                    await delete_active_ticket(int(discord_thread_id), user_id)
                    log.info(
                        f"Cleaned active-ticket state for resolved Plain thread "
                        f"{thread_id} (user={user_id}, discord_thread={discord_thread_id})"
                    )
                else:
                    # No user mapping — either already cleaned, or the user
                    # entry was lost somehow. Log it and continue; the
                    # Discord thread deletion below still proceeds.
                    log.warning(
                        f"Resolved Plain thread {thread_id} → Discord {discord_thread_id} "
                        f"but no user_tickets entry found; state may already be clean"
                    )
                # Schedule deletion with 10s countdown + !keep cancellation
                asyncio.ensure_future(
                    schedule_thread_deletion(int(discord_thread_id), triggered_by="Plain team")
                )
                log.info(f"Scheduled thread deletion for Plain thread {thread_id} -> Discord {discord_thread_id}")
            else:
                log.warning(f"No Discord thread mapped for resolved Plain thread {thread_id}")
        return web.Response(status=200, text="OK")

    # ── Handle chat_sent / email_sent ─────────────────────────────────────────

    # Extract message and actor based on event type
    # Plain schema: chat events wrap content in payload.chat, email in payload.email
    if event_type == "thread.chat_sent":
        chat_obj   = payload.get("chat", {}) or {}
        message_text = chat_obj.get("text", "")
        created_by = chat_obj.get("createdBy", {}) or {}
    else:  # thread.email_sent
        email_obj  = payload.get("email", {}) or {}
        message_text = (
            email_obj.get("textContent")
            or email_obj.get("text")
            or ""
        )
        created_by = email_obj.get("createdBy", {}) or {}

    # Skip if no text
    if not message_text:
        log.info(f"Webhook event {event_type} has no text content, skipping")
        return web.Response(status=200, text="OK")

    # Skip messages forwarded by our bot — they are prefixed with [discord-relay]
    # This prevents echo loops where forwarded messages get relayed back to Discord
    if message_text.startswith("[discord-relay]"):
        log.info("Skipping discord-relay prefixed message to prevent echo loop")
        return web.Response(status=200, text="OK")

    actor_type = (created_by.get("actorType") or "").lower()

    # Skip customer events — avoid echoing the user's own messages back
    if actor_type == "customer":
        log.info("Skipping customer event to prevent echo loop")
        return web.Response(status=200, text="OK")

    # Skip unknown actor types
    if actor_type not in ("user", "machineuser", "machine_user"):
        log.info(f"Skipping unknown actor type '{actor_type}'")
        return web.Response(status=200, text="OK")

    # For machineUser events: only allow email_sent through since that's how
    # human agent email replies arrive (sent by support email machine user).
    # All other machineUser events (chat forwarding etc) are skipped.
    if actor_type in ("machineuser", "machine_user") and event_type != "thread.email_sent":
        log.info("Skipping non-email machineUser event")
        return web.Response(status=200, text="OK")

    # Look up the Discord thread
    discord_thread_id = await get_discord_thread_id(thread_id)

    if not discord_thread_id:
        log.warning(f"No Discord thread mapped for Plain thread {thread_id}")
        return web.Response(status=200, text="OK")

    # Content-level dedupe — catches the case where Plain emits two distinct
    # events (different event IDs) with the same logical message. Observed
    # in production with Plain's AI-agent feature: a single reply produces two
    # thread.email_sent events with different event IDs. We've observed two
    # patterns — ~6s apart (fast double-emit) and ~20-22min apart (suspected
    # inactivity follow-up flow that re-emits the previous reply verbatim).
    # Event-id dedupe treats them as different events (which they are); this
    # check catches that they're semantically duplicates.
    if await _content_already_relayed(thread_id, message_text):
        log.info(
            f"Duplicate content for thread {thread_id} within "
            f"{_CONTENT_DEDUPE_TTL_SECONDS // 60}-minute window — "
            f"skipping (event {event_id})"
        )
        # Still mark the event_id so a Plain retry of THIS specific event
        # doesn't get re-processed and re-checked.
        if event_id:
            await _mark_relayed(event_id)
        return web.Response(status=200, text="OK")

    # Mark as relayed before posting so retries are caught even if posting is
    # slow. Placed AFTER the thread-mapping check above, so an event that
    # couldn't be mapped is left unmarked and a later retry can still land.
    if event_id:
        await _mark_relayed(event_id)
    await _mark_content_relayed(thread_id, message_text)

    # Resolve agent name from createdBy.user (Plain's structure for user actors)
    thread_obj = payload.get("thread", {}) or {}
    assignee   = thread_obj.get("assignee", {}) or {}

    agent_name = (
        (created_by.get("user") or {}).get("fullName")
        or (created_by.get("user") or {}).get("publicName")
        or created_by.get("fullName")
        or created_by.get("publicName")
        or assignee.get("fullName")
        or assignee.get("publicName")
        or "Support Agent"
    )
    log.debug(f"Relaying {event_type} from {agent_name} (actor={actor_type}) to Discord thread {discord_thread_id}")

    discord_message = (
        f"**💬 Reply from {agent_name}:**\n"
        f"{message_text}\n\n"
        f"_Reply in this thread to respond._"
    )

    if len(discord_message) > 1900:
        discord_message = discord_message[:1900] + "…"

    asyncio.ensure_future(send_discord_message(int(discord_thread_id), discord_message))

    # Log the agent reply to the transcript so the admin panel shows the full
    # ticket conversation. kind marks it transcript-only (the stats dashboard
    # ignores it); session is the ticket, so it groups with the whole case.
    # response_text is the agent's actual message; question is left null since
    # this row is an agent turn, not a user question.
    asyncio.ensure_future(db.log_conversation(
        source="plain",
        kind="ticket_agent_msg",
        username=agent_name,
        channel_id=str(discord_thread_id),
        response_source="ticket_message",
        response_text=message_text,
        session_id=f"ticket_{discord_thread_id}",
        plain_thread_id=thread_id,
    ))

    return web.Response(status=200, text="OK")


# ─── Server Setup ─────────────────────────────────────────────────────────────

async def handle_cancel_deletion(request: web.Request) -> web.Response:
    """
    Called by bot.py when a user types !keep in a Plain-triggered deletion thread.
    POST /cancel-deletion  body: {"thread_id": 123456789}
    """
    try:
        body = await request.json()
        thread_id = int(body.get("thread_id", 0))
        if not thread_id:
            return web.Response(status=400, text="missing thread_id")
        cancelled = cancel_thread_deletion(thread_id)
        return web.json_response({"cancelled": cancelled})
    except Exception as e:
        log.error(f"cancel-deletion error: {e}")
        return web.Response(status=500, text="error")


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/plain-webhook", handle_plain_webhook)
    app.router.add_post("/cancel-deletion", handle_cancel_deletion)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info(f"Plain webhook server listening on port {WEBHOOK_PORT}")
    if PLAIN_WEBHOOK_SECRET:
        log.info("Plain signature verification: ENABLED")
    else:
        log.warning(
            "Plain signature verification: DISABLED — set PLAIN_WEBHOOK_SECRET "
            "to require signed requests (configure secret in Plain: "
            "Settings → Request signing)"
        )
    if is_using_redis():
        log.info("Thread map: Redis ✅")
    else:
        log.warning("Thread map: in-memory only — set REDIS_URL for production")


async def main():
    global discord_client

    # Set up minimal Discord client
    intents = discord.Intents.default()
    discord_client = discord.Client(intents=intents)

    await discord_client.login(DISCORD_TOKEN)

    # Init the stats DB so agent replies can be logged to the transcript.
    # No-op if DATABASE_URL is unset — the relay still works, just without
    # transcript logging of agent replies.
    await db.init_db()

    await start_webhook_server()

    log.info("Webhook server running. Press Ctrl+C to stop.")
    try:
        # Keep running
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await db.close_db()
        await discord_client.close()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN not set")
    asyncio.run(main())
