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
  WEBHOOK_SECRET        - optional secret to validate Plain requests (recommended)
  DISCORD_TOKEN         - reused from main bot env
  PLAIN_WEBHOOK_SECRET  - optional, for request validation
"""

import asyncio
import logging
import os

import aiohttp
import discord
from aiohttp import web
from dotenv import load_dotenv
from redis_map import get_discord_thread_id, is_using_redis

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

async def schedule_thread_deletion(discord_thread_id: int, triggered_by: str = "Plain"):
    """
    Posts a 10-second countdown in the Discord thread then deletes it.
    Users can type !keep to cancel before the timer expires.
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

            # Listen for !keep for 10 seconds
            def check(m):
                return (
                    m.channel.id == discord_thread_id
                    and m.content.strip().lower() == "!keep"
                    and not m.author.bot
                )

            try:
                await discord_client.wait_for("message", check=check, timeout=10.0)
                # !keep received — cancel deletion
                await channel.send("✅ Deletion cancelled. This thread will stay open.")
                log.info(f"Thread {discord_thread_id} deletion cancelled via !keep")
                return
            except asyncio.TimeoutError:
                pass  # No !keep — proceed with deletion

            try:
                await channel.delete()
                log.info(f"Deleted ticket thread {discord_thread_id} (triggered by {triggered_by})")
            except discord.NotFound:
                log.info(f"Thread {discord_thread_id} already gone")
            except discord.Forbidden:
                log.warning(f"No permission to delete thread {discord_thread_id}")

        except Exception as e:
            log.error(f"Error during thread deletion for {discord_thread_id}: {e}")

    asyncio.ensure_future(_do_delete())


# ─── Webhook Handler ──────────────────────────────────────────────────────────

async def handle_plain_webhook(request: web.Request) -> web.Response:
    """
    Handle incoming Plain webhook events.
    We care about events where an agent (non-customer) replies to a thread.
    """
    # Optional signature check — add Plain-Webhook-Signature validation here
    # if you set PLAIN_WEBHOOK_SECRET (see Plain docs: request-signing)

    try:
        body = await request.json()
    except Exception:
        log.warning("Received non-JSON webhook body")
        return web.Response(status=400, text="Bad Request")

    event_type = body.get("type", "")
    log.info(f"Plain webhook received: {event_type}")

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
                # Schedule deletion with 10s countdown + !keep cancellation
                asyncio.ensure_future(
                    schedule_thread_deletion(int(discord_thread_id), triggered_by="Plain team")
                )
                log.info(f"Scheduled thread deletion for Plain thread {thread_id} -> Discord {discord_thread_id}")
            else:
                log.warning(f"No Discord thread mapped for resolved Plain thread {thread_id}")
        return web.Response(status=200, text="OK")

    # ── Handle chat_sent / email_sent ─────────────────────────────────────────

    # Only relay messages FROM agents — skip anything sent by the customer
    # to avoid echoing their own messages back into Discord
    actor = payload.get("actor", {}) or {}
    actor_type = (actor.get("actorType") or "").upper()
    if actor_type == "CUSTOMER":
        log.info("Skipping customer-originated event to avoid echo")
        return web.Response(status=200, text="OK")

    # Extract message text — structure differs slightly between chat and email
    if event_type == "thread.chat_sent":
        message_text = (
            payload.get("chatMessage", {}).get("text")
            or payload.get("text")
            or ""
        )
    else:  # thread.email_sent
        message_text = (
            payload.get("email", {}).get("textContent")
            or payload.get("textContent")
            or payload.get("text")
            or ""
        )

    if not message_text:
        log.info(f"Webhook event {event_type} has no text content, skipping")
        return web.Response(status=200, text="OK")

    # Look up the Discord thread
    discord_thread_id = await get_discord_thread_id(thread_id)

    if not discord_thread_id:
        log.warning(f"No Discord thread mapped for Plain thread {thread_id}")
        return web.Response(status=200, text="OK")

    # Resolve agent name from actor field
    agent_name = (
        actor.get("fullName")
        or actor.get("name")
        or actor.get("user", {}).get("fullName")
        or "Support Agent"
    )

    discord_message = (
        f"**💬 Reply from {agent_name}:**\n"
        f"{message_text}\n\n"
        f"_Reply in this thread to respond._"
    )

    # Cap Discord message length
    if len(discord_message) > 1900:
        discord_message = discord_message[:1900] + "…"

    asyncio.ensure_future(send_discord_message(int(discord_thread_id), discord_message))

    return web.Response(status=200, text="OK")


# ─── Server Setup ─────────────────────────────────────────────────────────────

async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/plain-webhook", handle_plain_webhook)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info(f"Plain webhook server listening on port {WEBHOOK_PORT}")
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
    await start_webhook_server()

    log.info("Webhook server running. Press Ctrl+C to stop.")
    try:
        # Keep running
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await discord_client.close()


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN not set")
    asyncio.run(main())
