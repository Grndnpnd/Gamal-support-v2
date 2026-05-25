"""
db.py
-----
Async PostgreSQL layer for conversation analytics.

Stores one row per handled support interaction (a Discord message the bot
answered, or an /query* API call) so the admin panel can show stats: message
volume, token usage, resolution rates, ticket counts, and a most-asked-
questions / documentation-gap report.

Backed by asyncpg. If DATABASE_URL is not set, every function in this module
becomes a no-op — log_conversation() silently does nothing, queries return
empty results. This means the bot runs perfectly fine without Postgres
configured (stats just don't record), and adding the Postgres service later
is purely additive — no code change, just set the env var and redeploy.

Railway setup:
  - Add a PostgreSQL database service to the project
  - Reference DATABASE_URL on the `bot` and `api` services
  - The conversations table is created automatically on first startup

ENV vars:
  DATABASE_URL          - Postgres connection string (Railway auto-provides)
  STATS_RETENTION_DAYS  - rows older than this are pruned (default 90)
"""

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
STATS_RETENTION_DAYS = int(os.getenv("STATS_RETENTION_DAYS", "90"))

# Lazy-initialized asyncpg pool. None until init_db() runs (or if no DB URL).
_pool = None
_init_failed = False  # set if init_db() hit an error — stops retry spam


# ─── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id               BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- where the interaction came from
    source           TEXT NOT NULL DEFAULT 'discord',   -- 'discord' | 'api'
    user_id          TEXT,                              -- discord user id (string)
    channel_id       TEXT,

    -- what was asked
    question         TEXT,
    topic            TEXT,                              -- model-assigned topic tag

    -- what happened
    response_source  TEXT,        -- 'docs'|'override'|'fallback'|'escalated'|'unresolved'|'error'
    resolved_by_bot  BOOLEAN DEFAULT FALSE,             -- answered, no escalation, no ticket
    doc_gap          BOOLEAN DEFAULT FALSE,             -- answer hit the "can't find it" path
    override_id      TEXT,                              -- set if an override fired
    plain_thread_id  TEXT,                              -- set if a ticket was opened

    -- llm call metadata
    llm_provider     TEXT,                              -- 'bankr' | 'ollama_cloud' | NULL
    tokens_in        INTEGER DEFAULT 0,
    tokens_out       INTEGER DEFAULT 0,
    latency_ms       INTEGER,

    error            TEXT                               -- error detail if response_source='error'
);

-- Indexes for the dashboard queries. started_at drives every time-window
-- filter; topic and response_source drive the grouping reports.
CREATE INDEX IF NOT EXISTS idx_conv_started_at      ON conversations (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_response_source ON conversations (response_source);
CREATE INDEX IF NOT EXISTS idx_conv_topic           ON conversations (topic);
"""


# ─── Lifecycle ────────────────────────────────────────────────────────────────

async def init_db() -> None:
    """
    Create the connection pool and ensure the schema exists. Call once on
    service startup. Safe to call when DATABASE_URL is unset — it just logs
    and leaves the module in no-op mode.
    """
    global _pool, _init_failed

    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — stats logging DISABLED (bot runs fine, just no analytics)")
        return

    try:
        import asyncpg
    except ImportError:
        log.error("asyncpg not installed — stats logging disabled. Add asyncpg to requirements.txt")
        _init_failed = True
        return

    try:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        async with _pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        log.info("Postgres connected — conversations table ready, stats logging ENABLED")
    except Exception as e:
        log.error(f"Postgres init failed: {e} — stats logging disabled")
        _init_failed = True
        _pool = None


async def close_db() -> None:
    """Close the pool cleanly on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def is_enabled() -> bool:
    """True if stats logging is live (pool exists)."""
    return _pool is not None


# ─── Writes ───────────────────────────────────────────────────────────────────

async def log_conversation(
    *,
    source: str = "discord",
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    question: Optional[str] = None,
    topic: Optional[str] = None,
    response_source: Optional[str] = None,
    resolved_by_bot: bool = False,
    doc_gap: bool = False,
    override_id: Optional[str] = None,
    plain_thread_id: Optional[str] = None,
    llm_provider: Optional[str] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    """
    Insert one conversation row. Returns the new row id, or None if stats
    logging is disabled or the write fails.

    This is called on the bot's hot path (after every answered message), so
    it must never raise — any failure is logged and swallowed. A stats write
    failing should never break a user's support interaction.
    """
    if _pool is None:
        return None

    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations (
                    source, user_id, channel_id, question, topic,
                    response_source, resolved_by_bot, doc_gap, override_id,
                    plain_thread_id, llm_provider, tokens_in, tokens_out,
                    latency_ms, error
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15
                )
                RETURNING id
                """,
                source, user_id, channel_id, question, topic,
                response_source, resolved_by_bot, doc_gap, override_id,
                plain_thread_id, llm_provider, tokens_in, tokens_out,
                latency_ms, error,
            )
            return row["id"] if row else None
    except Exception as e:
        log.error(f"log_conversation failed (swallowed): {e}")
        return None


async def update_conversation_ticket(row_id: int, plain_thread_id: str) -> None:
    """
    Backfill the plain_thread_id on an existing row — used when a ticket is
    opened *after* the conversation row was already written (the escalation
    yes/no flow opens the ticket on a later message). Best-effort.
    """
    if _pool is None or not row_id:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversations SET plain_thread_id = $1 WHERE id = $2",
                plain_thread_id, row_id,
            )
    except Exception as e:
        log.error(f"update_conversation_ticket failed (swallowed): {e}")


async def prune_old_rows() -> int:
    """
    Delete rows older than STATS_RETENTION_DAYS. Called periodically from the
    bot's cleanup loop. Returns the number of rows deleted.
    """
    if _pool is None:
        return 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=STATS_RETENTION_DAYS)
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM conversations WHERE started_at < $1", cutoff
            )
        # asyncpg returns a status string like "DELETE 42"
        deleted = int(result.split()[-1]) if result else 0
        if deleted:
            log.info(f"Pruned {deleted} conversation row(s) older than {STATS_RETENTION_DAYS}d")
        return deleted
    except Exception as e:
        log.error(f"prune_old_rows failed (swallowed): {e}")
        return 0


# ─── Reads (dashboard queries) ────────────────────────────────────────────────
# All read functions accept an explicit [since, until] UTC window so the
# dashboard can do both fixed presets (24h/7d/30d) and arbitrary date ranges
# with the same code path.

async def get_summary(since: datetime, until: datetime) -> dict:
    """
    Headline numbers for the dashboard cards: total messages, resolved-by-bot
    count and rate, tickets opened, doc-gap count, total tokens.
    """
    if _pool is None:
        return {}
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    count(*)                                              AS total,
                    count(*) FILTER (WHERE resolved_by_bot)               AS resolved,
                    count(*) FILTER (WHERE plain_thread_id IS NOT NULL)   AS tickets,
                    count(*) FILTER (WHERE doc_gap)                       AS doc_gaps,
                    count(*) FILTER (WHERE response_source = 'error')     AS errors,
                    COALESCE(sum(tokens_in), 0)                           AS tokens_in,
                    COALESCE(sum(tokens_out), 0)                          AS tokens_out
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                """,
                since, until,
            )
        total = row["total"] or 0
        resolved = row["resolved"] or 0
        return {
            "total": total,
            "resolved": resolved,
            "resolved_rate": round(100 * resolved / total, 1) if total else 0.0,
            "tickets": row["tickets"] or 0,
            "doc_gaps": row["doc_gaps"] or 0,
            "errors": row["errors"] or 0,
            "tokens_in": row["tokens_in"] or 0,
            "tokens_out": row["tokens_out"] or 0,
        }
    except Exception as e:
        log.error(f"get_summary failed: {e}")
        return {}


async def get_timeseries(since: datetime, until: datetime, bucket: str = "day") -> list[dict]:
    """
    Time-bucketed series for the charts. Each row: the bucket timestamp, total
    messages, a per-response_source breakdown, and token totals.

    bucket is 'hour' or 'day' — the dashboard picks 'hour' for the 24h view
    and 'day' for 7d/30d.
    """
    if _pool is None:
        return []
    if bucket not in ("hour", "day"):
        bucket = "day"
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    date_trunc('{bucket}', started_at)                        AS bucket,
                    count(*)                                                  AS total,
                    count(*) FILTER (WHERE response_source = 'docs')          AS docs,
                    count(*) FILTER (WHERE response_source = 'override')      AS override,
                    count(*) FILTER (WHERE response_source = 'fallback')      AS fallback,
                    count(*) FILTER (WHERE response_source = 'escalated')     AS escalated,
                    count(*) FILTER (WHERE response_source = 'unresolved')    AS unresolved,
                    count(*) FILTER (WHERE response_source = 'error')         AS error,
                    COALESCE(sum(tokens_in), 0)                               AS tokens_in,
                    COALESCE(sum(tokens_out), 0)                              AS tokens_out
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                GROUP BY bucket
                ORDER BY bucket
                """,
                since, until,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_timeseries failed: {e}")
        return []


async def get_top_topics(since: datetime, until: datetime, limit: int = 25) -> list[dict]:
    """
    The most-asked-questions / documentation-gap report. Groups by topic and
    returns, per topic: how often it was asked, how often it hit a doc gap,
    how often the bot resolved it, and how often it escalated.

    This is the report that tells the docs team what to write — topics with a
    high ask count AND a high doc_gap count are unwritten documentation.
    """
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(topic, 'untagged')                           AS topic,
                    count(*)                                              AS asked,
                    count(*) FILTER (WHERE doc_gap)                        AS doc_gaps,
                    count(*) FILTER (WHERE resolved_by_bot)                AS resolved,
                    count(*) FILTER (WHERE plain_thread_id IS NOT NULL)    AS escalated
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                GROUP BY COALESCE(topic, 'untagged')
                ORDER BY asked DESC
                LIMIT $3
                """,
                since, until, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_top_topics failed: {e}")
        return []


async def get_recent(since: datetime, until: datetime, limit: int = 100) -> list[dict]:
    """
    Recent raw conversation rows for the activity/error log view. Newest first.
    """
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, started_at, source, user_id, question, topic,
                       response_source, resolved_by_bot, doc_gap,
                       plain_thread_id, llm_provider, tokens_in, tokens_out,
                       latency_ms, error
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                ORDER BY started_at DESC
                LIMIT $3
                """,
                since, until, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_recent failed: {e}")
        return []
