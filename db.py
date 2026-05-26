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
    username         TEXT,                              -- discord display name
    channel_id       TEXT,

    -- what was asked
    question         TEXT,
    topic            TEXT,                              -- model-assigned topic tag

    -- what happened
    response_source  TEXT,        -- 'docs'|'override'|'fallback'|'escalated'|'unresolved'|'error'
    response_text    TEXT,                              -- the bot's reply, as the user saw it
    resolved_by_bot  BOOLEAN DEFAULT FALSE,             -- answered, no escalation, no ticket
    doc_gap          BOOLEAN DEFAULT FALSE,             -- answer hit the "can't find it" path
    override_id      TEXT,                              -- set if an override fired
    plain_thread_id  TEXT,                              -- set if a ticket was opened

    -- conversation grouping — all messages in one back-and-forth share a
    -- session_id, so the admin panel can show a full transcript.
    --   non-escalated: a TTL-scoped id minted when the conversation starts
    --   escalated:     re-keyed to the Discord ticket-thread id (stage 2)
    session_id       TEXT,

    -- row kind — what this row represents. The stats dashboard counts only
    -- 'bot_response' rows; the transcript view uses all kinds.
    --   bot_response     a question the bot answered (the original behavior)
    --   ticket_user_msg  a user message sent inside a ticket thread
    --   ticket_agent_msg a support-agent reply relayed in from Plain
    kind             TEXT NOT NULL DEFAULT 'bot_response',

    -- llm call metadata
    llm_provider     TEXT,                              -- 'bankr' | 'ollama_cloud' | NULL
    tokens_in        INTEGER DEFAULT 0,
    tokens_out       INTEGER DEFAULT 0,
    latency_ms       INTEGER,

    error            TEXT                               -- error detail if response_source='error'
);

-- Migrations for tables created before later columns existed.
-- ADD COLUMN IF NOT EXISTS is idempotent — safe to run on every startup.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS username      TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS response_text TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS session_id    TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS kind          TEXT NOT NULL DEFAULT 'bot_response';

-- Indexes for the dashboard queries. started_at drives every time-window
-- filter; topic and response_source drive the grouping reports.
CREATE INDEX IF NOT EXISTS idx_conv_started_at      ON conversations (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_response_source ON conversations (response_source);
CREATE INDEX IF NOT EXISTS idx_conv_topic           ON conversations (topic);
CREATE INDEX IF NOT EXISTS idx_conv_user            ON conversations (user_id);
CREATE INDEX IF NOT EXISTS idx_conv_channel         ON conversations (channel_id);
CREATE INDEX IF NOT EXISTS idx_conv_session         ON conversations (session_id);
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
    kind: str = "bot_response",
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    channel_id: Optional[str] = None,
    question: Optional[str] = None,
    topic: Optional[str] = None,
    response_source: Optional[str] = None,
    response_text: Optional[str] = None,
    resolved_by_bot: bool = False,
    doc_gap: bool = False,
    override_id: Optional[str] = None,
    plain_thread_id: Optional[str] = None,
    session_id: Optional[str] = None,
    llm_provider: Optional[str] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    """
    Insert one conversation row. Returns the new row id, or None if stats
    logging is disabled or the write fails.

    `kind` distinguishes a bot answer ('bot_response', the default and the
    only kind the stats dashboard counts) from transcript-only message rows
    ('ticket_user_msg', 'ticket_agent_msg').

    This is called on the bot's hot path, so it must never raise — any
    failure is logged and swallowed.
    """
    if _pool is None:
        return None

    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversations (
                    source, kind, user_id, username, channel_id, question,
                    topic, response_source, response_text, resolved_by_bot,
                    doc_gap, override_id, plain_thread_id, session_id,
                    llm_provider, tokens_in, tokens_out, latency_ms, error
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19
                )
                RETURNING id
                """,
                source, kind, user_id, username, channel_id, question,
                topic, response_source, response_text, resolved_by_bot,
                doc_gap, override_id, plain_thread_id, session_id,
                llm_provider, tokens_in, tokens_out, latency_ms, error,
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
                    count(*) FILTER (WHERE response_source = 'escalated') AS escalated,
                    count(*) FILTER (WHERE plain_thread_id IS NOT NULL)   AS tickets,
                    count(*) FILTER (WHERE doc_gap)                       AS doc_gaps,
                    count(*) FILTER (WHERE response_source = 'error')     AS errors,
                    COALESCE(sum(tokens_in), 0)                           AS tokens_in,
                    COALESCE(sum(tokens_out), 0)                          AS tokens_out
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                """,
                since, until,
            )
        total = row["total"] or 0
        resolved = row["resolved"] or 0
        return {
            "total": total,
            "resolved": resolved,
            "resolved_rate": round(100 * resolved / total, 1) if total else 0.0,
            "escalated": row["escalated"] or 0,
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
                  AND kind = 'bot_response'
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
                  AND kind = 'bot_response'
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
                SELECT id, started_at, kind, source, user_id, username,
                       question, topic, response_source, resolved_by_bot,
                       doc_gap, plain_thread_id, session_id, llm_provider,
                       tokens_in, tokens_out, latency_ms, error
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                ORDER BY started_at DESC
                LIMIT $3
                """,
                since, until, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_recent failed: {e}")
        return []


async def get_top_users(since: datetime, until: datetime, limit: int = 10) -> list[dict]:
    """
    Users with the most interactions in the window — the "who needs the most
    help" report. Returns username (falls back to a truncated user_id when no
    username was recorded), interaction count, and how many of those escalated.

    Discord-source rows only — API calls have no user.
    """
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(
                        NULLIF(username, ''),
                        'user ' || right(user_id, 6)
                    )                                                     AS who,
                    user_id,
                    count(*)                                              AS interactions,
                    count(*) FILTER (WHERE plain_thread_id IS NOT NULL)   AS escalated,
                    count(*) FILTER (WHERE doc_gap)                        AS doc_gaps
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                  AND source = 'discord'
                  AND user_id IS NOT NULL
                GROUP BY user_id, username
                ORDER BY interactions DESC
                LIMIT $3
                """,
                since, until, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_top_users failed: {e}")
        return []


async def get_provider_split(since: datetime, until: datetime) -> list[dict]:
    """
    How answers were split across LLM providers — Bankr primary vs the
    Ollama Cloud fallback. A high ollama_cloud share means Bankr has been
    flaky and the failover is carrying load.
    """
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(llm_provider, 'none')  AS provider,
                    count(*)                        AS count
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                GROUP BY llm_provider
                ORDER BY count DESC
                """,
                since, until,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_provider_split failed: {e}")
        return []


async def get_latency_stats(since: datetime, until: datetime) -> dict:
    """
    Response-latency summary: average and 95th percentile in milliseconds.
    p95 is the early-warning signal — if the slow tail grows, the LLM
    (primary or fallback) is degrading.
    """
    if _pool is None:
        return {}
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(round(avg(latency_ms)), 0)                            AS avg_ms,
                    COALESCE(percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY latency_ms), 0)                                   AS p95_ms,
                    COALESCE(max(latency_ms), 0)                                   AS max_ms
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                  AND latency_ms IS NOT NULL
                """,
                since, until,
            )
        return {
            "avg_ms": int(row["avg_ms"]),
            "p95_ms": int(row["p95_ms"]),
            "max_ms": int(row["max_ms"]),
        }
    except Exception as e:
        log.error(f"get_latency_stats failed: {e}")
        return {}


async def get_busiest_hours(since: datetime, until: datetime) -> list[dict]:
    """
    Message volume by hour-of-day (0-23, UTC), summed across the window.
    Tells you when support load peaks so staffing can match it.
    Always returns 24 rows, zero-filled, so the chart has a full day.
    """
    if _pool is None:
        return [{"hour": h, "count": 0} for h in range(24)]
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    EXTRACT(HOUR FROM started_at)::int  AS hour,
                    count(*)                            AS count
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                GROUP BY hour
                """,
                since, until,
            )
        counts = {r["hour"]: r["count"] for r in rows}
        return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]
    except Exception as e:
        log.error(f"get_busiest_hours failed: {e}")
        return [{"hour": h, "count": 0} for h in range(24)]


async def get_busiest_channels(since: datetime, until: datetime, limit: int = 8) -> list[dict]:
    """
    Discord channels with the most support traffic in the window.
    Returns the raw channel_id — the dashboard renders it as a <#id> mention
    style; Discord clients resolve those to names.
    """
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT channel_id, count(*) AS count
                FROM conversations
                WHERE started_at >= $1 AND started_at < $2
                  AND kind = 'bot_response'
                  AND source = 'discord'
                  AND channel_id IS NOT NULL
                GROUP BY channel_id
                ORDER BY count DESC
                LIMIT $3
                """,
                since, until, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_busiest_channels failed: {e}")
        return []


async def rekey_session(old_session_id: str, new_session_id: str) -> int:
    """
    Re-point every row of a conversation from one session id to another.

    Used when a conversation escalates into a ticket: the pre-escalation rows
    were logged under a TTL-scoped 'sess_...' id, but the whole case should
    read as one transcript keyed to the ticket. This UPDATEs those earlier
    rows to the ticket session id ('ticket_<discord_thread_id>').

    Returns the number of rows re-keyed. Best-effort — a failure here just
    means the transcript is split, not that anything breaks.
    """
    if _pool is None or not old_session_id or not new_session_id:
        return 0
    try:
        async with _pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE conversations SET session_id = $1 WHERE session_id = $2",
                new_session_id, old_session_id,
            )
        moved = int(result.split()[-1]) if result else 0
        if moved:
            log.info(f"Re-keyed {moved} row(s) from {old_session_id} to {new_session_id}")
        return moved
    except Exception as e:
        log.error(f"rekey_session failed (swallowed): {e}")
        return 0


async def get_session(session_id: str) -> list[dict]:
    """
    Return every row of one conversation session, oldest-first — the full
    transcript. Includes all kinds (bot answers, ticket user messages, agent
    replies) so the admin transcript view shows the complete back-and-forth.
    """
    if _pool is None or not session_id:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, started_at, kind, source, user_id, username,
                       channel_id, question, topic, response_source,
                       response_text, resolved_by_bot, doc_gap,
                       plain_thread_id, session_id, llm_provider,
                       tokens_in, tokens_out, latency_ms, error
                FROM conversations
                WHERE session_id = $1
                ORDER BY started_at ASC, id ASC
                """,
                session_id,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"get_session failed: {e}")
        return []


async def search_conversations(
    *,
    since: datetime,
    until: datetime,
    topic: Optional[str] = None,
    response_source: Optional[str] = None,
    llm_provider: Optional[str] = None,
    doc_gap_only: bool = False,
    errors_only: bool = False,
    tickets_only: bool = False,
    text_query: Optional[str] = None,
    username_query: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Filtered, paginated search over conversations for the admin panel's
    Conversations tab.

    Searches only kind='bot_response' rows — those are the "a user asked
    something" entries. Ticket user/agent messages are transcript detail,
    surfaced via get_session when a row is opened, not as standalone search
    hits.

    All filters are optional and AND-combined. Returns:
      { "rows": [...], "total": <int matching the filters> }
    so the UI can show "showing 1-50 of 312" and paginate.
    """
    if _pool is None:
        return {"rows": [], "total": 0}

    # Build the WHERE clause dynamically. $1/$2 are always the time window;
    # further params are appended as filters are added.
    conditions = ["started_at >= $1", "started_at < $2", "kind = 'bot_response'"]
    params: list = [since, until]

    def _add(cond_tmpl: str, value):
        params.append(value)
        conditions.append(cond_tmpl.format(n=len(params)))

    if topic:
        _add("topic = ${n}", topic)
    if response_source:
        _add("response_source = ${n}", response_source)
    if llm_provider:
        _add("llm_provider = ${n}", llm_provider)
    if doc_gap_only:
        conditions.append("doc_gap = TRUE")
    if errors_only:
        conditions.append("response_source = 'error'")
    if tickets_only:
        conditions.append("plain_thread_id IS NOT NULL")
    if text_query:
        # case-insensitive substring match on the question text
        _add("question ILIKE '%' || ${n} || '%'", text_query)
    if username_query:
        _add("username ILIKE '%' || ${n} || '%'", username_query)

    where = " AND ".join(conditions)

    try:
        async with _pool.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT count(*) FROM conversations WHERE {where}", *params
            )
            # limit/offset are the last two params
            params_paged = params + [limit, offset]
            rows = await conn.fetch(
                f"""
                SELECT id, started_at, kind, source, user_id, username,
                       channel_id, question, topic, response_source,
                       response_text, resolved_by_bot, doc_gap,
                       plain_thread_id, session_id, llm_provider,
                       tokens_in, tokens_out, latency_ms, error
                FROM conversations
                WHERE {where}
                ORDER BY started_at DESC
                LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
                """,
                *params_paged,
            )
        return {"rows": [dict(r) for r in rows], "total": total or 0}
    except Exception as e:
        log.error(f"search_conversations failed: {e}")
        return {"rows": [], "total": 0}
