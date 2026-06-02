"""
redis_map.py
------------
Shared Plain ↔ Discord thread map backed by Redis.

Used by both bot.py (writes) and webhook_server.py (reads) so that agent
replies from Plain route back to the correct Discord ticket thread even
when those two processes run as separate Railway services.

Falls back to an in-memory dict if REDIS_URL is not set, which preserves
full local development functionality without needing a Redis instance.

Railway setup:
  - Add a Redis database service to your Railway project
  - Railway automatically injects REDIS_URL into all services
  - No additional config needed

ENV vars:
  REDIS_URL  - Redis connection URL (e.g. redis://default:pass@host:port)
               Automatically set by Railway when you add a Redis service.
               If not set, falls back to in-memory dict (local dev only).
"""

import logging
import os

log = logging.getLogger(__name__)

REDIS_URL     = os.getenv("REDIS_URL", "")
REDIS_KEY_PREFIX = "plain_discord_map:"
REDIS_TTL_DAYS   = 30  # auto-expire old ticket links after 30 days


# ─── Backend selection ────────────────────────────────────────────────────────

_redis_client = None
_memory_map: dict[str, int] = {}   # fallback for local dev


def _get_redis():
    """Lazy-init Redis client. Returns None if REDIS_URL not set."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not REDIS_URL:
        return None
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        log.info("Redis thread map: connected")
        return _redis_client
    except Exception as e:
        log.error(f"Redis connection failed: {e} — falling back to in-memory map")
        return None


# ─── Public API ───────────────────────────────────────────────────────────────

async def set_thread_link(plain_thread_id: str, discord_thread_id: int) -> None:
    """
    Store plain_thread_id → discord_thread_id mapping.
    Called by bot.py when a new ticket is opened.
    """
    r = _get_redis()
    if r:
        try:
            key = f"{REDIS_KEY_PREFIX}{plain_thread_id}"
            await r.set(key, str(discord_thread_id), ex=REDIS_TTL_DAYS * 86400)
            log.info(f"Redis: linked {plain_thread_id} → {discord_thread_id}")
            return
        except Exception as e:
            log.error(f"Redis set failed: {e} — falling back to memory")

    # In-memory fallback
    _memory_map[plain_thread_id] = discord_thread_id
    log.info(f"Memory map: linked {plain_thread_id} → {discord_thread_id}")


async def get_discord_thread_id(plain_thread_id: str) -> int | None:
    """
    Look up the Discord thread ID for a given Plain thread ID.
    Called by webhook_server.py when an agent reply arrives.
    Returns None if not found.
    """
    r = _get_redis()
    if r:
        try:
            key = f"{REDIS_KEY_PREFIX}{plain_thread_id}"
            value = await r.get(key)
            if value is not None:
                return int(value)
            return None
        except Exception as e:
            log.error(f"Redis get failed: {e} — falling back to memory")

    return _memory_map.get(plain_thread_id)


async def delete_thread_link(plain_thread_id: str) -> None:
    """
    Remove a thread link after the ticket is closed/resolved.
    Optional cleanup — links expire automatically via TTL anyway.
    """
    r = _get_redis()
    if r:
        try:
            await r.delete(f"{REDIS_KEY_PREFIX}{plain_thread_id}")
            return
        except Exception as e:
            log.error(f"Redis delete failed: {e}")

    _memory_map.pop(plain_thread_id, None)


def is_using_redis() -> bool:
    """Returns True if Redis is configured, False if using in-memory fallback."""
    return bool(REDIS_URL)


# ─── Active Ticket Persistence ────────────────────────────────────────────────
# Stores the in-memory state from PlainTicketManager so it survives redeploys.
#
#   active_tickets:    discord_thread_id → plain_thread_id   (str→str hash)
#   user_tickets:      discord_user_id   → discord_thread_id (str→str hash)
#   ticket_customers:  discord_thread_id → plain_customer_id (str→str hash)
#                      ─ added 2026-06-02 to support send_chat, which needs
#                        the customer_id alongside the thread_id. See module
#                        notes below on backward-compat.
#
# All three hashes are written and read together via pipelines so the state
# stays internally consistent. Reads are tolerant of partial state — if a
# ticket existed before ticket_customers was added, its customer_id lookup
# returns None and the caller is expected to handle that (the bot's
# forward_to_plain falls back to the old reply_to_thread path).

ACTIVE_TICKETS_KEY    = "active_tickets"
USER_TICKETS_KEY      = "user_tickets"
TICKET_CUSTOMERS_KEY  = "ticket_customers"

# In-memory fallback dicts (local dev without Redis)
_active_tickets_mem:   dict[int, str] = {}
_user_tickets_mem:     dict[int, int] = {}
_ticket_customers_mem: dict[int, str] = {}


async def save_active_ticket(
    discord_thread_id: int,
    plain_thread_id: str,
    user_id: int,
    plain_customer_id: str | None = None,
) -> None:
    """
    Persist an active ticket to Redis.
    Called when a new ticket is opened.

    plain_customer_id is optional for backward compatibility with old call
    sites that may not pass it (none should after this change ships, but
    the kwarg shape keeps the function safe to call either way). When
    provided, it's stored in the ticket_customers hash so send_chat can
    later look it up.
    """
    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            await pipe.hset(ACTIVE_TICKETS_KEY, str(discord_thread_id), plain_thread_id)
            await pipe.hset(USER_TICKETS_KEY,   str(user_id), str(discord_thread_id))
            if plain_customer_id:
                await pipe.hset(
                    TICKET_CUSTOMERS_KEY,
                    str(discord_thread_id),
                    plain_customer_id,
                )
            await pipe.execute()
            log.info(
                f"Redis: saved active ticket discord={discord_thread_id} "
                f"plain={plain_thread_id}"
                + (f" customer={plain_customer_id}" if plain_customer_id else "")
            )
            return
        except Exception as e:
            log.error(f"Redis save_active_ticket failed: {e} — falling back to memory")

    _active_tickets_mem[discord_thread_id] = plain_thread_id
    _user_tickets_mem[user_id] = discord_thread_id
    if plain_customer_id:
        _ticket_customers_mem[discord_thread_id] = plain_customer_id


async def delete_active_ticket(discord_thread_id: int, user_id: int) -> None:
    """
    Remove an active ticket from Redis.
    Called when a ticket is closed or resolved.
    """
    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            await pipe.hdel(ACTIVE_TICKETS_KEY,   str(discord_thread_id))
            await pipe.hdel(USER_TICKETS_KEY,     str(user_id))
            await pipe.hdel(TICKET_CUSTOMERS_KEY, str(discord_thread_id))
            await pipe.execute()
            log.info(f"Redis: deleted active ticket discord={discord_thread_id}")
            return
        except Exception as e:
            log.error(f"Redis delete_active_ticket failed: {e}")

    _active_tickets_mem.pop(discord_thread_id, None)
    _user_tickets_mem.pop(user_id, None)
    _ticket_customers_mem.pop(discord_thread_id, None)


async def load_active_tickets() -> tuple[dict[int, str], dict[int, int], dict[int, str]]:
    """
    Load all active tickets from Redis on bot startup.
    Returns (active_tickets, user_tickets, ticket_customers) as plain dicts.

    active_tickets:   discord_thread_id (int) → plain_thread_id (str)
    user_tickets:     discord_user_id (int)   → discord_thread_id (int)
    ticket_customers: discord_thread_id (int) → plain_customer_id (str)

    The ticket_customers dict may be EMPTY or a SUBSET of active_tickets —
    that's expected: tickets opened before the customer_id storage was added
    have no entry, and their forward path falls back to reply_to_thread.
    """
    r = _get_redis()
    if r:
        try:
            raw_active    = await r.hgetall(ACTIVE_TICKETS_KEY)
            raw_users     = await r.hgetall(USER_TICKETS_KEY)
            raw_customers = await r.hgetall(TICKET_CUSTOMERS_KEY)

            active_tickets   = {int(k): v for k, v in raw_active.items()}
            user_tickets     = {int(k): int(v) for k, v in raw_users.items()}
            ticket_customers = {int(k): v for k, v in raw_customers.items()}

            log.info(
                f"Redis: loaded {len(active_tickets)} active ticket(s) on startup "
                f"({len(ticket_customers)} with customer_id)"
            )
            return active_tickets, user_tickets, ticket_customers
        except Exception as e:
            log.error(f"Redis load_active_tickets failed: {e} — starting with empty state")

    return (
        dict(_active_tickets_mem),
        dict(_user_tickets_mem),
        dict(_ticket_customers_mem),
    )


async def get_customer_for_thread(discord_thread_id: int) -> str | None:
    """
    Look up the Plain customer ID associated with a Discord thread.

    Returns None for tickets opened before customer_id storage was added —
    callers must handle this case (the bot's forward_to_plain falls back to
    reply_to_thread on None, which keeps pre-fix tickets working until they
    close naturally).
    """
    r = _get_redis()
    if r:
        try:
            value = await r.hget(TICKET_CUSTOMERS_KEY, str(discord_thread_id))
            return value  # already str or None
        except Exception as e:
            log.error(f"Redis get_customer_for_thread failed: {e}")
    return _ticket_customers_mem.get(discord_thread_id)
