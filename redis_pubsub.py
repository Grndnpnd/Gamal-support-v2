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

import asyncio
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
    """
    Lazy-init Redis client for SHORT request/response ops (publish, get, set).
    Returns None if REDIS_URL not set.

    Note: this client has a 5s socket_timeout, which is a good safety net for
    quick ops but is WRONG for a pub/sub subscriber — an idle subscriber
    legitimately reads nothing for long stretches and would hit that timeout.
    The subscriber uses its own client; see listen_for_reindex.
    """
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


async def heal_stale_reindex_status(service: str) -> bool:
    """
    Startup self-heal for orphaned reindex status.

    request_reindex() pre-marks every service 'running' when the button is
    pressed. Each service is then supposed to write 'done'/'failed' when it
    finishes. If a service CRASHES mid-reindex (or its listener task dies), it
    never writes a terminal state, so reindex:status:{service} stays 'running'
    for the full 7-day TTL — which leaves the admin button disabled ("stuck
    pressed"), because the panel sees a service still in progress.

    A freshly-booted process cannot be in the middle of a reindex that started
    before it existed. So on startup: if THIS service's status is 'running',
    it is necessarily stale — clear it to 'failed' with an explanatory detail.
    This un-sticks the button on deploy and prevents the wedge from recurring.

    Returns True if a stale 'running' was cleared, else False.
    """
    r = _get_redis()
    if r is None:
        return False
    try:
        raw = await r.get(f"{STATUS_KEY_PREFIX}{service}")
        if not raw:
            return False
        try:
            cur = json.loads(raw)
        except json.JSONDecodeError:
            return False
        if cur.get("state") == "running":
            await set_reindex_status(
                service, "failed",
                detail="stale 'running' cleared on startup (previous reindex "
                       "never completed — likely crashed mid-run)",
            )
            log.warning(
                f"heal_stale_reindex_status: cleared orphaned 'running' status "
                f"for {service} (was stuck since {cur.get('at')})"
            )
            return True
        return False
    except Exception as e:
        log.error(f"heal_stale_reindex_status failed for {service}: {e}")
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

    Each service starts this once as a background task. If REDIS_URL is unset
    it logs and returns (the service still runs, just without manual reindex).

    Two things this has to get right that a naive version doesn't:

    1. Its own client with NO socket read timeout. A subscriber spends almost
       all its time idle, waiting for a publish that may not come for hours.
       The shared _get_redis() client has socket_timeout=5 — correct for quick
       ops, fatal here (an idle subscriber would "time out" after 5s and die).

    2. A reconnect loop. Redis connections drop — deploys, network blips,
       Railway internal hiccups. Without the loop, one drop permanently kills
       the feature until a redeploy. With it, the subscriber just reconnects
       after a short backoff and carries on.
    """
    if not REDIS_URL:
        log.warning("listen_for_reindex: REDIS_URL unset — manual reindex disabled")
        return

    import redis.asyncio as aioredis

    backoff = 2
    while True:
        sub_client = None
        try:
            # Dedicated client: socket_timeout=None so idle waiting is fine.
            sub_client = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=None,
                health_check_interval=30,
            )
            pubsub = sub_client.pubsub()
            await pubsub.subscribe(REINDEX_CHANNEL)
            log.info("Listening for manual reindex signals")
            backoff = 2  # reset backoff after a successful (re)subscribe

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue  # ignore subscribe-confirmation frames
                triggered_by = message.get("data", "unknown")
                log.info(f"Reindex signal received (by {triggered_by})")
                try:
                    await on_signal(triggered_by)
                except Exception as e:
                    log.error(f"Reindex callback errored: {e}")

        except asyncio.CancelledError:
            # Service is shutting down — exit cleanly, don't reconnect.
            log.info("Reindex subscriber cancelled — stopping")
            raise
        except Exception as e:
            log.warning(
                f"Reindex subscriber connection lost ({e}) — "
                f"reconnecting in {backoff}s"
            )
        finally:
            if sub_client is not None:
                try:
                    await sub_client.aclose()
                except Exception:
                    pass

        # Reconnect with capped exponential backoff.
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ─── Article-sync signaling ──────────────────────────────────────────────────
#
# Same pub/sub pattern as docs reindex above, but for the help-center article
# sync feature. Only the `api` service runs the article-sync job (unlike
# reindex, which fans out to bot+api), so the subscriber lives in api_server
# only. We use pub/sub anyway because (a) it gives us a clean "start now"
# trigger without polling and (b) the architecture stays consistent — admin
# panel publishes, worker subscribes.
#
# Why this is a distinct channel from reindex: article-sync uses the docs
# index but its own job (LLM passes + Plain reads) is conceptually unrelated.
# Mixing them on one channel would mean every reindex would try to run a sync
# and vice versa, which is wrong.
#
# Redis keys (all under "article_sync:" so they're easy to inspect/clean):
#   article_sync:channel             pub/sub channel
#   article_sync:status              JSON {state, at, detail, proposal_id}
#   article_sync:proposal:{id}       full Proposal JSON
#   article_sync:current_proposal_id current proposal id (for review page)
#   article_sync:pinned              hash of {slug: {pinned_by, pinned_at}}
#                                    — used by stage 3 review UI to suppress
#                                    article from future propose runs

ARTICLE_SYNC_CHANNEL          = "article_sync:channel"
ARTICLE_SYNC_STATUS_KEY       = "article_sync:status"
ARTICLE_SYNC_PROPOSAL_KEY_PFX = "article_sync:proposal:"
ARTICLE_SYNC_CURRENT_KEY      = "article_sync:current_proposal_id"
# Proposals self-expire after 7 days; long enough for review, short enough
# that we don't accumulate. The latest proposal id is also stored separately
# so the review page can find it without scanning keys.
ARTICLE_SYNC_PROPOSAL_TTL_SECONDS = 7 * 86400


async def request_article_sync(triggered_by: str = "admin") -> bool:
    """
    Publish a "start an article-sync proposal" signal. Returns True if
    published, False if Redis unavailable.

    Pre-marks status as 'running' so the panel reflects in-flight state
    immediately rather than waiting for the subscriber to start.

    Caller is responsible for the "is there already one running?" check —
    request_article_sync deliberately doesn't refuse a second request,
    because the admin may have *just* confirmed they want to cancel and
    replace. The decision belongs in the admin route, not here.
    """
    r = _get_redis()
    if r is None:
        log.warning("request_article_sync: Redis unavailable — cannot signal")
        return False
    try:
        await set_article_sync_status("running", detail=f"requested by {triggered_by}")
        await r.publish(ARTICLE_SYNC_CHANNEL, triggered_by)
        log.info(f"request_article_sync: published (by {triggered_by})")
        return True
    except Exception as e:
        log.error(f"request_article_sync failed: {e}")
        return False


async def set_article_sync_status(
    state: str,
    *,
    detail: str = "",
    proposal_id: Optional[str] = None,
) -> None:
    """
    Record the current state of the article-sync job:
      'running'  — propose pipeline is mid-flight
      'ready'    — proposal generated, waiting for review/publish
      'failed'   — propose pipeline errored; detail carries the message
      'idle'     — nothing in progress (the default when no sync has run)
    """
    r = _get_redis()
    if r is None:
        return
    try:
        blob = {
            "state":  state,
            "at":     datetime.now(timezone.utc).isoformat(),
            "detail": detail,
        }
        if proposal_id is not None:
            blob["proposal_id"] = proposal_id
        # No TTL on status itself — it's tiny and we want it to persist
        # across the gap between "ready" and the admin actually reviewing.
        await r.set(ARTICLE_SYNC_STATUS_KEY, json.dumps(blob))
    except Exception as e:
        log.error(f"set_article_sync_status failed: {e}")


async def get_article_sync_status() -> Optional[dict]:
    """Read the current article-sync status. Returns None if Redis unavailable or no status yet."""
    r = _get_redis()
    if r is None:
        return None
    try:
        raw = await r.get(ARTICLE_SYNC_STATUS_KEY)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.error(f"get_article_sync_status failed: {e}")
        return None


async def save_article_sync_proposal(proposal_id: str, proposal: dict) -> bool:
    """
    Store a generated proposal so the review page can render it. The proposal
    is the dict form of an article_sync.Proposal (Proposal.to_dict()).

    Also updates `article_sync:current_proposal_id` so the review page can
    locate the latest without scanning. Older proposals stay around until
    their TTL expires, but the panel only shows the current one.
    """
    r = _get_redis()
    if r is None:
        return False
    try:
        await r.set(
            ARTICLE_SYNC_PROPOSAL_KEY_PFX + proposal_id,
            json.dumps(proposal),
            ex=ARTICLE_SYNC_PROPOSAL_TTL_SECONDS,
        )
        await r.set(ARTICLE_SYNC_CURRENT_KEY, proposal_id)
        return True
    except Exception as e:
        log.error(f"save_article_sync_proposal failed: {e}")
        return False


async def get_article_sync_proposal(proposal_id: Optional[str] = None) -> Optional[dict]:
    """
    Load a proposal by id. If proposal_id is None, loads the *current* one
    (the most recent successful propose run). Returns None if not found.
    """
    r = _get_redis()
    if r is None:
        return None
    try:
        pid = proposal_id
        if not pid:
            pid = await r.get(ARTICLE_SYNC_CURRENT_KEY)
        if not pid:
            return None
        raw = await r.get(ARTICLE_SYNC_PROPOSAL_KEY_PFX + pid)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.error(f"get_article_sync_proposal failed: {e}")
        return None


async def listen_for_article_sync(on_signal) -> None:
    """
    Long-running coroutine. Same shape as listen_for_reindex — dedicated
    Redis client with no socket timeout, reconnect loop with capped
    exponential backoff. Started once from api_server.py.

    The callback gets called with the "triggered_by" string. It's expected
    to do the propose work and update status / proposal storage itself —
    this function is just the trigger plumbing.
    """
    if not REDIS_URL:
        log.warning("listen_for_article_sync: REDIS_URL unset — manual article-sync disabled")
        return

    import redis.asyncio as aioredis

    backoff = 2
    while True:
        sub_client = None
        try:
            sub_client = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=None,
                health_check_interval=30,
            )
            pubsub = sub_client.pubsub()
            await pubsub.subscribe(ARTICLE_SYNC_CHANNEL)
            log.info("Listening for article-sync signals")
            backoff = 2

            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                triggered_by = message.get("data", "unknown")
                log.info(f"Article-sync signal received (by {triggered_by})")
                try:
                    await on_signal(triggered_by)
                except Exception as e:
                    log.error(f"Article-sync callback errored: {e}")

        except asyncio.CancelledError:
            log.info("Article-sync subscriber cancelled — stopping")
            raise
        except Exception as e:
            log.warning(
                f"Article-sync subscriber connection lost ({e}) — "
                f"reconnecting in {backoff}s"
            )
        finally:
            if sub_client is not None:
                try:
                    await sub_client.aclose()
                except Exception:
                    pass

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ─── Article-sync: pins ──────────────────────────────────────────────────────
#
# A pin says "the human has deliberately set this article the way it is —
# don't propose rewrites of this slug." Pins are shared across the team
# (anyone can see them on the review page), and they don't actually skip the
# LLM judgment step — they just disable the Publish action on a pinned row
# so an admin can't accidentally clobber a deliberate-by-design article.
#
# Storage: a single Redis hash keyed by slug, value = JSON {pinned_by, pinned_at, note}.
# A hash works well here because we want both "is this slug pinned?"
# (HGET single field) and "list all pinned" (HGETALL) to be cheap.
ARTICLE_SYNC_PINNED_KEY = "article_sync:pinned"


async def set_pin(slug: str, *, pinned_by: str, note: str = "") -> bool:
    """
    Pin one article slug. pinned_by is freeform — we use "admin panel" today
    but a future per-user admin layer can pass a real identifier.

    Returns True on success.
    """
    if not slug:
        return False
    r = _get_redis()
    if r is None:
        return False
    try:
        payload = {
            "pinned_by": pinned_by,
            "pinned_at": datetime.now(timezone.utc).isoformat(),
            "note":      note or "",
        }
        await r.hset(ARTICLE_SYNC_PINNED_KEY, slug, json.dumps(payload))
        log.info(f"Pinned slug={slug!r} by {pinned_by!r}")
        return True
    except Exception as e:
        log.error(f"set_pin failed for {slug}: {e}")
        return False


async def unset_pin(slug: str) -> bool:
    """Unpin one slug. Returns True even if it wasn't pinned (idempotent)."""
    if not slug:
        return False
    r = _get_redis()
    if r is None:
        return False
    try:
        await r.hdel(ARTICLE_SYNC_PINNED_KEY, slug)
        log.info(f"Unpinned slug={slug!r}")
        return True
    except Exception as e:
        log.error(f"unset_pin failed for {slug}: {e}")
        return False


async def get_pins() -> dict[str, dict]:
    """Return {slug: {pinned_by, pinned_at, note}} for every currently pinned slug."""
    r = _get_redis()
    if r is None:
        return {}
    try:
        raw = await r.hgetall(ARTICLE_SYNC_PINNED_KEY)
        out: dict[str, dict] = {}
        for slug, blob in (raw or {}).items():
            try:
                out[slug] = json.loads(blob)
            except json.JSONDecodeError:
                log.warning(f"Corrupt pin entry for {slug}, skipping")
        return out
    except Exception as e:
        log.error(f"get_pins failed: {e}")
        return {}


# ─── Article-sync: per-item edits on top of a proposal ──────────────────────
#
# When an admin clicks "Save my edits" on a card, we don't mutate the
# original proposal blob — we layer the edits in a separate per-(proposal_id,
# slug) record. The review page reads the proposal AND the edits and shows
# whichever is newer for each field. This keeps history intact (you can
# always see what the LLM originally proposed) and lets us reset edits if
# the admin asks.
#
# A "new-topic" suggestion that's had a body generated gets stored here too,
# with the generated HTML in the same shape as a needs_update edit.
#
# Keys:  article_sync:edit:{proposal_id}:{slug}  → JSON dict
ARTICLE_SYNC_EDIT_KEY_PFX = "article_sync:edit:"


async def save_item_edit(
    proposal_id: str,
    slug: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    content_html: Optional[str] = None,
    generated_for_new_topic: bool = False,
) -> bool:
    """
    Persist an admin edit (or generated-new-topic body) for one item.

    Only non-None fields are written. To reset, pass empty strings — they
    overwrite. The review page composes the final view by reading the
    proposal AND any matching edit, with edit fields winning where present.

    Returns True on success.
    """
    if not (proposal_id and slug):
        return False
    r = _get_redis()
    if r is None:
        return False
    try:
        key = f"{ARTICLE_SYNC_EDIT_KEY_PFX}{proposal_id}:{slug}"
        # Read-modify-write so partial edits compose.
        raw = await r.get(key)
        existing = json.loads(raw) if raw else {}
        if title is not None:
            existing["title"] = title
        if description is not None:
            existing["description"] = description
        if content_html is not None:
            existing["content_html"] = content_html
        if generated_for_new_topic:
            existing["generated_for_new_topic"] = True
        existing["edited_at"] = datetime.now(timezone.utc).isoformat()
        # Edits expire with the proposal (7d). After that you'd be reviewing
        # a stale proposal anyway.
        await r.set(key, json.dumps(existing), ex=ARTICLE_SYNC_PROPOSAL_TTL_SECONDS)
        return True
    except Exception as e:
        log.error(f"save_item_edit failed for {proposal_id}/{slug}: {e}")
        return False


async def get_item_edits(proposal_id: str) -> dict[str, dict]:
    """
    Return {slug: edit_dict} for every edit recorded against this proposal.
    Used by the review page to overlay edits onto the rendered cards.
    """
    if not proposal_id:
        return {}
    r = _get_redis()
    if r is None:
        return {}
    try:
        # SCAN for matching keys. Edit count per proposal is small (≤22+13),
        # so this is fine.
        prefix = f"{ARTICLE_SYNC_EDIT_KEY_PFX}{proposal_id}:"
        cursor = 0
        out: dict[str, dict] = {}
        while True:
            cursor, keys = await r.scan(cursor=cursor, match=prefix + "*", count=100)
            for key in keys:
                raw = await r.get(key)
                if not raw:
                    continue
                slug = key[len(prefix):]
                try:
                    out[slug] = json.loads(raw)
                except json.JSONDecodeError:
                    continue
            if cursor == 0:
                break
        return out
    except Exception as e:
        log.error(f"get_item_edits failed for {proposal_id}: {e}")
        return {}


# ─── Article-sync: publish results ──────────────────────────────────────────
#
# After the admin clicks Publish on the review page, we kick off the Plain
# upserts and record a per-article result. The results page reads this back
# so the admin can see exactly what happened, even after navigating away.
#
# Key: article_sync:result:{run_id}  → JSON dict shaped like
#   { "run_id":..., "proposal_id":..., "started_at":..., "finished_at":...,
#     "items": [
#        { "slug":..., "ok": bool, "plain_id_after":..., "error":...|null }
#     ]
#   }
ARTICLE_SYNC_RESULT_KEY_PFX = "article_sync:result:"
# Results expire with proposals.
# Reuses ARTICLE_SYNC_PROPOSAL_TTL_SECONDS above.


async def save_publish_result(run_id: str, result: dict) -> bool:
    """Persist a publish run's per-article results."""
    if not run_id:
        return False
    r = _get_redis()
    if r is None:
        return False
    try:
        await r.set(
            f"{ARTICLE_SYNC_RESULT_KEY_PFX}{run_id}",
            json.dumps(result),
            ex=ARTICLE_SYNC_PROPOSAL_TTL_SECONDS,
        )
        return True
    except Exception as e:
        log.error(f"save_publish_result failed for {run_id}: {e}")
        return False


async def get_publish_result(run_id: str) -> Optional[dict]:
    if not run_id:
        return None
    r = _get_redis()
    if r is None:
        return None
    try:
        raw = await r.get(f"{ARTICLE_SYNC_RESULT_KEY_PFX}{run_id}")
        return json.loads(raw) if raw else None
    except Exception as e:
        log.error(f"get_publish_result failed for {run_id}: {e}")
        return None
