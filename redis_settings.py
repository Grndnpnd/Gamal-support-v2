"""
redis_settings.py
-----------------
General-purpose store for admin-tunable bot settings.

Distinct from redis_overrides.py (which stores a *collection* of override
objects) — this module holds a single flat settings blob: the handful of
on/off switches and small config values an admin flips from the panel.

First occupant: "busy mode" — when enabled, the bot suppresses passive
proactive support offers aimed at members holding configured staff roles
(Moderator, Support, etc). The point is high-traffic incident windows: when
the support team is actively answering customers live in channels, the bot
shouldn't keep jumping in on their messages. Busy mode is a toggle so it's
off during normal operation and flipped on only when needed.

Backed by Redis with an in-memory fallback for local dev, same pattern as
redis_map.py and redis_overrides.py.

Redis key layout:
  settings:bot   → JSON blob of all settings

We store everything under a single key (rather than one key per setting)
because the settings set is tiny and always read together — one GET on the
bot's hot path is cheaper than several, and a single JSON blob is trivial
to extend with future settings.
"""

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")
SETTINGS_KEY = "settings:bot"

# Default staff roles to pre-populate busy mode with. Mirrors the existing
# MOD_ROLE_NAME env convention (comma-separated role names) so the admin UI
# starts from something sensible rather than empty.
_DEFAULT_STAFF_ROLES = [
    r.strip() for r in os.getenv("MOD_ROLE_NAME", "Moderator").split(",") if r.strip()
]

# The full settings schema with defaults. get_settings() always returns a
# dict shaped like this — missing keys are backfilled — so callers never
# have to handle a partial blob.
_DEFAULTS: dict[str, Any] = {
    # Busy mode: when True, passive proactive offers are suppressed for
    # members holding any role in busy_mode_roles.
    "busy_mode_enabled": False,
    # Role names (not IDs) whose members are ignored on the passive path
    # while busy mode is on. Matched case-insensitively in the bot.
    "busy_mode_roles": _DEFAULT_STAFF_ROLES,
}


# ─── Backend selection ────────────────────────────────────────────────────────

_redis_client = None
_mem_settings: Optional[dict] = None  # in-memory fallback blob


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
        log.info("Redis settings: connected")
        return _redis_client
    except Exception as e:
        log.error(f"Redis connection failed: {e} — falling back to in-memory settings")
        return None


def _merge_defaults(stored: Optional[dict]) -> dict:
    """
    Return a complete settings dict: defaults overlaid with whatever was
    stored. This means adding a new setting to _DEFAULTS automatically
    works for existing deployments — the new key just takes its default
    until an admin sets it.
    """
    result = dict(_DEFAULTS)
    # deep-ish copy of the list default so callers can't mutate _DEFAULTS
    result["busy_mode_roles"] = list(_DEFAULTS["busy_mode_roles"])
    if stored:
        for k, v in stored.items():
            if k in _DEFAULTS:  # ignore unknown/legacy keys
                result[k] = v
    return result


# ─── Public API ───────────────────────────────────────────────────────────────

async def get_settings() -> dict:
    """
    Return the full settings dict (defaults backfilled). Safe to call on the
    bot's hot path — one Redis GET, and on any Redis error it falls back to
    in-memory / defaults rather than raising.
    """
    r = _get_redis()
    if r:
        try:
            raw = await r.get(SETTINGS_KEY)
            stored = json.loads(raw) if raw else None
            return _merge_defaults(stored)
        except Exception as e:
            log.error(f"Redis get_settings failed: {e} — using defaults")
            return _merge_defaults(None)

    return _merge_defaults(_mem_settings)


async def update_settings(**fields) -> dict:
    """
    Patch one or more settings. Only keys present in _DEFAULTS are accepted;
    unknown keys are ignored so a malformed admin form can't inject junk.
    Returns the full updated settings dict.
    """
    current = await get_settings()
    for key, value in fields.items():
        if key in _DEFAULTS:
            current[key] = value
        else:
            log.warning(f"update_settings ignoring unknown key: {key}")

    raw = json.dumps(current)
    r = _get_redis()
    if r:
        try:
            await r.set(SETTINGS_KEY, raw)
            log.info(f"Settings updated: {list(fields.keys())}")
            return current
        except Exception as e:
            log.error(f"Redis update_settings failed: {e} — falling back to memory")

    global _mem_settings
    _mem_settings = current
    log.info(f"Settings updated (memory): {list(fields.keys())}")
    return current
