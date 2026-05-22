"""
redis_overrides.py
------------------
Storage and matching layer for the "doc override" feature.

An override is an admin-configured response that takes precedence over the
normal docs-search → LLM answer flow. Typical use: during a service outage,
post an override with keywords like "401, login, can't sign in" and a message
like "We've temporarily paused the service — we'll announce when it's back."
Any user whose message matches gets the override instead of a doc answer.

Each override carries an `allow_ticket_offer` flag:
  - False (default): send the message and stop. No ticket offer.
  - True:            send the message AND append the standard "want a
                     ticket?" prompt, so the user can still escalate.

Backed by Redis with an in-memory fallback for local dev, same as redis_map.py.

Redis key layout:
  overrides:active             → SET of override IDs that are enabled
  overrides:item:{id}          → JSON blob of the override
  overrides:stats:{id}:hits    → INCR counter, lifetime hit count

We deliberately do NOT subscribe to keyspace notifications or use pub/sub —
overrides are checked on every incoming message, and one Redis SMEMBERS + a
few GETs per message is well within budget for our volume. Simpler beats
clever here.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")

# Redis key constants
ACTIVE_SET_KEY = "overrides:active"
ITEM_KEY_PREFIX = "overrides:item:"
HITS_KEY_PREFIX = "overrides:stats:"  # full key: overrides:stats:{id}:hits


# ─── Backend selection ────────────────────────────────────────────────────────
# We share the same Redis client pattern as redis_map.py but keep a separate
# lazy-init function so the two modules don't have to import each other.

_redis_client = None

# In-memory fallback structures (local dev without Redis)
_mem_active: set[str] = set()
_mem_items: dict[str, dict] = {}
_mem_hits: dict[str, int] = {}


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
        log.info("Redis overrides: connected")
        return _redis_client
    except Exception as e:
        log.error(f"Redis connection failed: {e} — falling back to in-memory overrides")
        return None


# ─── Override matching ────────────────────────────────────────────────────────

def _matches(message: str, override: dict) -> bool:
    """
    Decide whether an override should fire for a given incoming message.

    Matching rules (v1 — kept deliberately simple):
      1. Override must be enabled.
      2. If starts_at/ends_at are set, current UTC time must be within window.
      3. At least one keyword in the override must appear in the message,
         case-insensitive, substring match.

    Returns True if all three conditions are met.
    """
    if not override.get("enabled", True):
        return False

    now = datetime.now(timezone.utc)

    starts_at = override.get("starts_at")
    if starts_at:
        try:
            if now < datetime.fromisoformat(starts_at):
                return False
        except ValueError:
            # Bad timestamp in storage — treat as "no lower bound" rather than
            # crashing the message handler. Worth flagging in admin UI.
            log.warning(f"Override {override.get('id')} has invalid starts_at: {starts_at}")

    ends_at = override.get("ends_at")
    if ends_at:
        try:
            if now > datetime.fromisoformat(ends_at):
                return False
        except ValueError:
            log.warning(f"Override {override.get('id')} has invalid ends_at: {ends_at}")

    keywords = override.get("keywords") or []
    if not keywords:
        return False  # An override with no keywords would match everything — refuse

    low = message.lower()
    return any(kw.lower() in low for kw in keywords if kw.strip())


async def find_matching_override(message: str) -> Optional[dict]:
    """
    Walk through active overrides and return the FIRST one that matches the
    incoming message. Returns None if nothing matches.

    "First" means most-recently-created — sorted by created_at descending.
    Rationale: if you're posting overrides during an incident, the newest
    one is probably the most relevant. We can swap this for an explicit
    priority field later if needed.
    """
    overrides = await list_overrides(only_active=True)

    # Sort newest first so the most recent override wins ties
    overrides.sort(key=lambda o: o.get("created_at", ""), reverse=True)

    for o in overrides:
        if _matches(message, o):
            return o
    return None


# ─── CRUD ─────────────────────────────────────────────────────────────────────

async def list_overrides(only_active: bool = False) -> list[dict]:
    """
    Return all overrides, optionally filtered to only those in the active set.
    Used by both the matcher (only_active=True) and the admin UI (False).
    """
    r = _get_redis()

    if r:
        try:
            if only_active:
                ids = await r.smembers(ACTIVE_SET_KEY)
            else:
                # Scan for all override items — small N (dozens at most),
                # so KEYS is fine. SCAN would be cleaner at scale.
                keys = await r.keys(f"{ITEM_KEY_PREFIX}*")
                ids = [k.removeprefix(ITEM_KEY_PREFIX) for k in keys]

            results = []
            for oid in ids:
                raw = await r.get(f"{ITEM_KEY_PREFIX}{oid}")
                if raw:
                    try:
                        results.append(json.loads(raw))
                    except json.JSONDecodeError:
                        log.error(f"Override {oid} has corrupt JSON, skipping")
            return results
        except Exception as e:
            log.error(f"Redis list_overrides failed: {e} — using memory fallback")

    # Memory fallback
    if only_active:
        return [_mem_items[i] for i in _mem_active if i in _mem_items]
    return list(_mem_items.values())


async def get_override(override_id: str) -> Optional[dict]:
    """Fetch a single override by ID. Returns None if not found."""
    r = _get_redis()
    if r:
        try:
            raw = await r.get(f"{ITEM_KEY_PREFIX}{override_id}")
            return json.loads(raw) if raw else None
        except Exception as e:
            log.error(f"Redis get_override failed: {e}")
    return _mem_items.get(override_id)


async def create_override(
    name: str,
    keywords: list[str],
    message: str,
    allow_ticket_offer: bool = False,
    starts_at: Optional[str] = None,
    ends_at: Optional[str] = None,
    enabled: bool = True,
    created_by: str = "admin",
) -> dict:
    """
    Create a new override. Returns the full override dict (including the
    generated UUID and created_at timestamp).

    starts_at / ends_at are optional ISO-8601 strings. If both are None, the
    override is unbounded in time (enabled => always active).
    """
    override = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "keywords": [k.strip() for k in keywords if k.strip()],
        "message": message,
        "allow_ticket_offer": bool(allow_ticket_offer),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "enabled": bool(enabled),
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await _write_override(override)
    log.info(
        f"Override created: id={override['id']} name={override['name']!r} "
        f"keywords={override['keywords']} enabled={override['enabled']}"
    )
    return override


async def update_override(override_id: str, **fields) -> Optional[dict]:
    """
    Patch an existing override. Only the keys passed in `fields` are updated;
    everything else stays as-is. Returns the updated override, or None if it
    didn't exist.
    """
    existing = await get_override(override_id)
    if not existing:
        return None

    # Whitelist what's patchable so callers can't inject arbitrary keys
    allowed = {"name", "keywords", "message", "allow_ticket_offer",
               "starts_at", "ends_at", "enabled"}
    for key, value in fields.items():
        if key in allowed:
            existing[key] = value

    # Normalize keywords if they got touched
    if "keywords" in fields:
        existing["keywords"] = [k.strip() for k in existing["keywords"] if k.strip()]

    await _write_override(existing)
    log.info(f"Override updated: id={override_id} fields={list(fields.keys())}")
    return existing


async def delete_override(override_id: str) -> bool:
    """Remove an override entirely. Returns True if it existed."""
    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            await pipe.delete(f"{ITEM_KEY_PREFIX}{override_id}")
            await pipe.srem(ACTIVE_SET_KEY, override_id)
            await pipe.delete(f"{HITS_KEY_PREFIX}{override_id}:hits")
            results = await pipe.execute()
            deleted = bool(results[0])
            if deleted:
                log.info(f"Override deleted: id={override_id}")
            return deleted
        except Exception as e:
            log.error(f"Redis delete_override failed: {e}")

    existed = override_id in _mem_items
    _mem_items.pop(override_id, None)
    _mem_active.discard(override_id)
    _mem_hits.pop(override_id, None)
    if existed:
        log.info(f"Override deleted (memory): id={override_id}")
    return existed


# ─── Hit tracking ─────────────────────────────────────────────────────────────

async def record_override_hit(override_id: str) -> None:
    """
    Increment the lifetime hit counter for an override. Called from the bot
    every time an override fires. Cheap — single INCR on Redis.
    """
    r = _get_redis()
    if r:
        try:
            await r.incr(f"{HITS_KEY_PREFIX}{override_id}:hits")
            return
        except Exception as e:
            log.error(f"Redis record_override_hit failed: {e}")
    _mem_hits[override_id] = _mem_hits.get(override_id, 0) + 1


async def get_override_hits(override_id: str) -> int:
    """Return the lifetime hit count for an override. 0 if never hit."""
    r = _get_redis()
    if r:
        try:
            val = await r.get(f"{HITS_KEY_PREFIX}{override_id}:hits")
            return int(val) if val else 0
        except Exception as e:
            log.error(f"Redis get_override_hits failed: {e}")
    return _mem_hits.get(override_id, 0)


# ─── Internals ────────────────────────────────────────────────────────────────

async def _write_override(override: dict) -> None:
    """
    Persist an override and sync its membership in the active set.
    Called by create_override and update_override.
    """
    oid = override["id"]
    enabled = bool(override.get("enabled", True))
    raw = json.dumps(override)

    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            await pipe.set(f"{ITEM_KEY_PREFIX}{oid}", raw)
            if enabled:
                await pipe.sadd(ACTIVE_SET_KEY, oid)
            else:
                await pipe.srem(ACTIVE_SET_KEY, oid)
            await pipe.execute()
            return
        except Exception as e:
            log.error(f"Redis _write_override failed: {e} — falling back to memory")

    _mem_items[oid] = override
    if enabled:
        _mem_active.add(oid)
    else:
        _mem_active.discard(oid)
