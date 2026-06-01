"""
docs_snapshot.py
----------------
Redis-backed storage for the "last successfully synced docs" snapshot.

The article-sync feature uses this to:
  1. Decide whether docs changed at all since the last sync (cheap hash
     compare → if unchanged, skip the LLM entirely).
  2. Compute a section-aware diff against the previous version, so the
     propose LLM only sees the docs sections that actually changed, not
     the whole corpus. This is the token-conservation move we designed
     for and the reason typical Sync runs stay cheap.

We store two things, both under straightforward keys:

  docs_snapshot:hash    - SHA-256 hex of the docs text, plus an ISO timestamp
                          of when it was saved. JSON blob. Cheap to read on
                          every Sync click.
  docs_snapshot:text    - the raw docs text from that last successful sync,
                          so the section-aware diff can compare new vs old.
                          Can be large (>100KB) — fine for Redis at our scale.

If REDIS_URL is unset, both reads return None and writes are no-ops; the
caller treats "no snapshot" as "first sync ever, full corpus to LLM" which
is the correct fallback.

Snapshot is only written on a *successful* sync (i.e. after the user has
clicked Publish in the review UI and at least one article actually updated).
We do NOT save on a Propose run alone — that way a half-finished Propose
that the admin abandons doesn't reset the next diff's baseline.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")

HASH_KEY = "docs_snapshot:hash"
TEXT_KEY = "docs_snapshot:text"

# Snapshots are referenced often during sync runs but rarely otherwise. Keep
# them around indefinitely — they're tiny compared to ChromaDB and there's no
# reason to expire. A future "reset baseline" admin button could delete them
# explicitly.

_redis_client = None


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
        return _redis_client
    except Exception as e:
        log.error(f"docs_snapshot: Redis connection failed: {e}")
        return None


def hash_docs(text: str) -> str:
    """SHA-256 of the docs text, hex-encoded. Stable across runs."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def get_last_snapshot() -> Optional[dict]:
    """
    Return the last successfully-synced docs snapshot, or None if there
    isn't one (Redis unset, or never synced before).

    Returns: { "hash": str, "saved_at": str (ISO), "text": str }
    """
    r = _get_redis()
    if r is None:
        return None
    try:
        raw_meta = await r.get(HASH_KEY)
        if not raw_meta:
            return None
        meta = json.loads(raw_meta)
        text = await r.get(TEXT_KEY)
        if text is None:
            # hash recorded but text missing — treat as no snapshot. Could
            # happen if someone manually deleted one of the keys.
            log.warning("docs_snapshot: hash present but text missing; treating as no snapshot")
            return None
        return {
            "hash":     meta.get("hash", ""),
            "saved_at": meta.get("saved_at", ""),
            "text":     text,
        }
    except Exception as e:
        log.error(f"docs_snapshot: get_last_snapshot failed: {e}")
        return None


async def save_snapshot(text: str) -> Optional[str]:
    """
    Persist a new docs snapshot. Returns the hash of the saved text, or None
    if storage failed.

    Call this only after a successful sync — see module docstring.
    """
    r = _get_redis()
    if r is None:
        return None
    try:
        h = hash_docs(text)
        meta = {
            "hash":     h,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        pipe = r.pipeline()
        await pipe.set(HASH_KEY, json.dumps(meta))
        await pipe.set(TEXT_KEY, text)
        await pipe.execute()
        log.info(f"docs_snapshot: saved, hash={h[:12]}...")
        return h
    except Exception as e:
        log.error(f"docs_snapshot: save_snapshot failed: {e}")
        return None
