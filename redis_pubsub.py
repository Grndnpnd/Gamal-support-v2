"""
redis_pubsub.py
---------------
Cross-service signaling for the manual docs re-index button.

The problem this solves: the `api` service and the `bot` service each run
their own SemanticDocsManager with its own ChromaDB index. The admin panel
lives in the `api` service, so it can re-index `api`'s docs directly — but
the `bot` service is a separate process and can't be called in-process.

Mechanism:
  - The panel calls request_reindex(), which PUBLISHes to a Redis channel.
  - Each service runs a background subscriber (see bot.py / api_server.py)
    that re-indexes its docs when a message arrives.
  - After a service finishes re-indexing, it calls set_reindex_status() to
    record a per-service "last reindexed at" timestamp + state.
  - The panel reads all statuses via get_reindex_statuses() to show the
    readout, so an admin can see when each service last finished and avoid
    triggering a redundant re-index.

Falls back to no-op / empty status when REDIS_URL is unset (local dev) —
the button simply won't do anything useful without Redis, same graceful
degradation pattern as the other redis_* modules.

Redis keys:
  reindex:channel              pub/sub channel for the trigger signal
  reindex:status:{service}     JSON {state, at, detail} per service
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "")

REINDEX_CHANNEL = "reindex:channel"
STATUS_KEY_PREFIX = "reindex:status:"
# Services we expect to report status. Used so the panel can show a row for
# each even before any has reported.
KNOWN_SERVICES = ("bot", "api")

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
        log.info("Redis pubsub: connected")
        return _redis_client
    except Exception as e:
        log.error(f"Redis connection failed: {e} — reindex signaling disabled")
        return None


# ─── Trigger ──────────────────────────────────────────────────────────────────

async def request_reindex(triggered_by: str = "admin") -> bool:
    """
    Publish a re-index request to all subscribed services. Called by the
    admin panel when the button is clicked. Returns True if the message was
    published, False if Redis is unavailable.

    Also pre-marks every known service's status as 'running' so the panel
    immediately reflects "in progress" rather than showing stale 'idle'
    until each service gets around to reporting.
    """
    r = _get_redis()
    if r is None:
        log.warning("request_reindex: Redis unavailable — cannot signal services")
        return False
    try:
        # Pre-mark services as running so the UI updates instantly.
        for svc in KNOWN_SERVICES:
            await set_reindex_status(svc, "running", detail=f"requested by {triggered_by}")
        await r.publish(REINDEX_CHANNEL, triggered_by)
        log.info(f"request_reindex: published reindex signal (by {triggered_by})")
        return True
    except Exception as e:
        log.error(f"request_reindex failed: {e}")
        return False


# ─── Status ───────────────────────────────────────────────────────────────────

async def set_reindex_status(service: str, state: str, detail: str = "") -> None:
    """
    Record a service's re-index status. Called by each service:
      - 'running'  when it starts re-indexing
      - 'done'     when it finishes successfully
      - 'failed'   if re-indexing errored

    state/at/detail are stored as a JSON blob under reindex:status:{service}.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        blob = json.dumps({
            "state": state,
            "at": datetime.now(timezone.utc).isoformat(),
            "detail": detail,
        })
        # Keep status around for 7 days — long enough to always have a
        # "last reindexed" to show, short enough to self-clean.
        await r.set(f"{STATUS_KEY_PREFIX}{service}", blob, ex=7 * 86400)
    except Exception as e:
        log.error(f"set_reindex_status failed for {service}: {e}")


async def get_reindex_statuses() -> dict:
    """
    Return the re-index status of every known service, for the admin panel.
    Shape: { "bot": {state, at, detail} | None, "api": {...} | None }
    None means that service has never reported (no status recorded yet).
    """
    r = _get_redis()
    result: dict = {svc: None for svc in KNOWN_SERVICES}
    if r is None:
        return result
    try:
        for svc in KNOWN_SERVICES:
            raw = await r.get(f"{STATUS_KEY_PREFIX}{svc}")
            if raw:
                try:
                    result[svc] = json.loads(raw)
                except json.JSONDecodeError:
                    result[svc] = None
    except Exception as e:
        log.error(f"get_reindex_statuses failed: {e}")
    return result


# ─── Subscriber ───────────────────────────────────────────────────────────────

async def listen_for_reindex(on_signal) -> None:
    """
    Long-running coroutine: subscribes to the reindex channel and calls the
    async `on_signal` callback whenever a re-index is requested.

    Each service starts this once as a background task. If Redis is
    unavailable it logs and returns (the service still runs, just without
    the manual-reindex feature).

    The callback is responsible for the actual re-index and for reporting
    status via set_reindex_status.
    """
    r = _get_redis()
    if r is None:
        log.warning("listen_for_reindex: Redis unavailable — manual reindex disabled")
        return

    try:
        pubsub = r.pubsub()
        await pubsub.subscribe(REINDEX_CHANNEL)
        log.info("Listening for manual reindex signals")
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue  # ignore subscribe-confirmation frames
            triggered_by = message.get("data", "unknown")
            log.info(f"Reindex signal received (by {triggered_by})")
            try:
                await on_signal(triggered_by)
            except Exception as e:
                log.error(f"Reindex callback errored: {e}")
    except Exception as e:
        log.error(f"listen_for_reindex loop ended: {e}")
