"""
api_server.py
-------------
FastAPI server that exposes two endpoints for external agents to call:

  POST /query-docs   — semantic search over ChromaDB, returns raw chunks
  POST /query-llm    — takes a question + optional context, returns grounded answer

The two-call pattern lets an agent:
  1. Fetch relevant doc chunks  (/query-docs)
  2. Get a grounded LLM answer  (/query-llm)

This prevents hallucination because the LLM is forced to answer from
the retrieved context rather than training data.

Authentication:
  All requests require:  Authorization: Bearer <API_SERVER_KEY>
  Set API_SERVER_KEY in your .env — this is YOUR key, not Plain or Ollama.

Run alongside bot.py:
  python api_server.py

ENV vars:
  API_SERVER_KEY   - secret key external agents must send (required)
  API_SERVER_PORT  - port to listen on (default: 8000)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Security, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
import uvicorn

from shared import SemanticDocsManager, OllamaClient
from llm_router import LLMRouter
from plain_client import PlainClient
from redis_pubsub import (
    listen_for_reindex, set_reindex_status,
    listen_for_article_sync, set_article_sync_status,
    save_article_sync_proposal,
)
import article_sync
import db

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_SERVER_KEY  = os.getenv("API_SERVER_KEY", "")
API_SERVER_PORT = int(os.getenv("API_SERVER_PORT", "8000"))

if not API_SERVER_KEY:
    log.warning("API_SERVER_KEY not set — API is unprotected! Set it in .env before deploying.")

# ─── Shared instances ─────────────────────────────────────────────────────────

docs   = SemanticDocsManager()
# LLM router: Bankr gateway primary, Ollama Cloud (Gemma) fallback.
# Variable kept named "ollama" so endpoint code is unchanged.
ollama = LLMRouter()

# ─── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load docs and init the stats DB on startup."""
    log.info("API server starting — pre-loading docs...")
    await docs.ensure_ready()
    log.info("Docs ready.")
    # Stash on app.state so the admin panel's article-sync routes can reach
    # the same already-loaded docs manager (avoids reloading 65MB of docs
    # per "generate body" click). Same instance the /query endpoint uses.
    app.state.docs_manager = docs
    app.state.llm_router   = ollama
    await db.init_db()  # no-op if DATABASE_URL is unset

    # Listen for manual docs re-index signals from the admin panel.
    async def _on_reindex_signal(triggered_by: str):
        await set_reindex_status("api", "running", detail=f"by {triggered_by}")
        try:
            ok = await docs.force_reindex()
            await set_reindex_status(
                "api", "done" if ok else "failed",
                detail="" if ok else "fetch or index failed",
            )
        except Exception as e:
            log.error(f"API reindex failed: {e}")
            await set_reindex_status("api", "failed", detail=str(e)[:120])

    reindex_task = asyncio.ensure_future(listen_for_reindex(_on_reindex_signal))

    # Listen for manual help-center article-sync signals from the admin panel.
    # Same pattern as reindex above, but here the worker runs the propose
    # pipeline and stashes the resulting proposal in Redis for the review
    # page to render. No writes to Plain happen here — those are gated
    # behind the admin's explicit Publish click on the review page.
    async def _on_article_sync_signal(triggered_by: str):
        proposal_id = datetime.now(timezone.utc).strftime("prop_%Y%m%dT%H%M%SZ")
        try:
            plain_api_key = os.getenv("PLAIN_API_KEY", "")
            if not plain_api_key:
                await set_article_sync_status(
                    "failed",
                    detail="PLAIN_API_KEY not set in api service env",
                )
                return
            # Use the docs manager that's already loaded — same instance the
            # /query endpoint uses, so we don't re-fetch.
            plain = PlainClient(plain_api_key)
            proposal = await article_sync.propose(
                docs_manager=docs,
                plain_client=plain,
                router=ollama,
            )
            ok = await save_article_sync_proposal(proposal_id, proposal.to_dict())
            if not ok:
                await set_article_sync_status(
                    "failed",
                    detail="Proposal generated but could not be stored",
                )
                return
            summary = proposal.summary or {}
            detail = (
                f"{summary.get('needs_update', 0)} update(s), "
                f"{summary.get('new_topics', 0)} new, "
                f"{summary.get('orphans', 0)} orphan(s), "
                f"{summary.get('errors', 0)} error(s) "
                f"— by {triggered_by}"
            )
            await set_article_sync_status("ready", detail=detail, proposal_id=proposal_id)
            log.info(f"Article-sync proposal {proposal_id} ready: {detail}")
        except Exception as e:
            log.error(f"Article-sync run failed: {e}")
            await set_article_sync_status("failed", detail=str(e)[:200])

    article_sync_task = asyncio.ensure_future(
        listen_for_article_sync(_on_article_sync_signal)
    )

    yield
    # shutdown
    reindex_task.cancel()
    article_sync_task.cancel()
    await db.close_db()


app = FastAPI(
    title="Bankr Support API",
    description="Grounded doc search and LLM query endpoints for external agents.",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount the admin panel routes (/admin/*). Imported here rather than at the
# top of the file so a missing dependency in admin_routes.py (e.g.
# itsdangerous, python-multipart) doesn't take down the agent API.
try:
    from admin_routes import router as admin_router
    app.include_router(admin_router)
    log.info("Admin panel mounted at /admin")
except ImportError as e:
    log.warning(f"Admin panel disabled — missing dependency: {e}")

security = HTTPBearer(auto_error=False)

# ─── Simple in-memory rate limiter ───────────────────────────────────────────
# Limits per IP: 60 requests/minute (matches Bankr gateway limit)
from collections import defaultdict
import time as _time

_rate_buckets: dict = defaultdict(lambda: {"count": 0, "reset_at": 0.0})
RATE_LIMIT_RPM = int(os.getenv("API_RATE_LIMIT_RPM", "60"))


def _check_rate_limit(request):
    """Raise 429 if caller exceeds RATE_LIMIT_RPM requests per minute."""
    ip = request.client.host if request.client else "unknown"
    now = _time.time()
    bucket = _rate_buckets[ip]
    if now > bucket["reset_at"]:
        bucket["count"] = 0
        bucket["reset_at"] = now + 60
    bucket["count"] += 1
    if bucket["count"] > RATE_LIMIT_RPM:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded — max {RATE_LIMIT_RPM} requests/minute",
        )


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Validate the Bearer token if API_SERVER_KEY is set."""
    if not API_SERVER_KEY:
        return  # No key configured — open access (dev only)
    if not credentials or credentials.credentials != API_SERVER_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ─── Request / Response Models ────────────────────────────────────────────────

class QueryDocsRequest(BaseModel):
    question: str
    top_k: Optional[int] = None  # override default TOP_K_CHUNKS

class ChunkResult(BaseModel):
    text: str
    distance: Optional[float]
    index: Optional[int]

class QueryDocsResponse(BaseModel):
    question: str
    chunks: list[ChunkResult]
    combined_text: str          # pre-joined for convenience


class QueryLLMRequest(BaseModel):
    question: str
    context: Optional[str] = None   # pre-fetched doc context (from /query-docs)
    system_prompt: Optional[str] = None  # optional override
    temperature: Optional[float] = 0.1  # low default — factual answers only


class QueryLLMResponse(BaseModel):
    question: str
    answer: str
    grounded: bool  # True if context was provided, False if LLM answered cold


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "docs_ready": docs._ready}


@app.post("/query-docs", response_model=QueryDocsResponse)
async def query_docs(
    req: QueryDocsRequest,
    request: Request,
    _: None = Depends(verify_token),
):
    _check_rate_limit(request)
    """
    Semantic search over the Bankr documentation ChromaDB index.

    Returns the most relevant chunks for the given question.
    Call this first, then pass the combined_text to /query-llm as context.

    Example:
        POST /query-docs
        { "question": "how do I set up a skill on Bankr?" }
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    log.info(f"[/query-docs] question={req.question[:80]}")

    raw_chunks = await docs.query_chunks(req.question, top_k=req.top_k)
    combined   = "\n\n---\n\n".join(c["text"] for c in raw_chunks)

    return QueryDocsResponse(
        question=req.question,
        chunks=[ChunkResult(**c) for c in raw_chunks],
        combined_text=combined,
    )


@app.post("/query-llm", response_model=QueryLLMResponse)
async def query_llm(
    req: QueryLLMRequest,
    request: Request,
    _: None = Depends(verify_token),
):
    _check_rate_limit(request)
    """
    Ask the LLM a question, optionally grounded in doc context.

    For best results:
      1. Call /query-docs first to get relevant chunks
      2. Pass the combined_text back here as `context`

    When context is provided the LLM is instructed to answer ONLY from
    that context and say "I don't know" if it can't find the answer —
    this is what prevents hallucination.

    Example:
        POST /query-llm
        {
          "question": "how do I set up a skill on Bankr?",
          "context": "<text from /query-docs combined_text>"
        }
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    log.info(f"[/query-llm] question={req.question[:80]} grounded={bool(req.context)}")

    grounded = bool(req.context and req.context.strip())

    if req.system_prompt:
        system = req.system_prompt
    elif grounded:
        system = (
            "You are a precise support assistant for Bankr — a platform for AI agents "
            "that fund themselves through DeFi and token launching.\n\n"
            "Answer the user's question using ONLY the documentation context provided below. "
            "Do not use outside knowledge or make assumptions beyond what is in the context. "
            "If the answer is not in the context, say exactly: "
            "\"I couldn't find information about that in the Bankr documentation.\"\n\n"
            "Be concise and accurate. Do not add caveats or filler text.\n\n"
            f"--- BANKR DOCUMENTATION CONTEXT ---\n{req.context}\n--- END CONTEXT ---"
        )
    else:
        # No context provided — still answer but flag as ungrounded
        system = (
            "You are a support assistant for Bankr — a platform for AI agents "
            "that fund themselves through DeFi and token launching.\n\n"
            "Answer as accurately as you can. If you are unsure, say so clearly "
            "rather than guessing. Keep answers concise."
        )

    messages = [{"role": "user", "content": req.question}]
    _t0 = _time.time()
    answer = await ollama.chat(
        messages=messages,
        system=system,
        temperature=req.temperature or 0.1,
    )
    _latency_ms = int((_time.time() - _t0) * 1000)

    # Stats row for the agent-API call. source='api' separates these from
    # Discord traffic in the dashboard. Fire-and-forget — never blocks the response.
    _llm_ok = getattr(answer, "ok", True)
    asyncio.ensure_future(db.log_conversation(
        source="api",
        question=req.question,
        topic=None,  # API calls don't run the topic-tagging prompt
        response_source=("error" if not _llm_ok else "docs"),
        resolved_by_bot=_llm_ok,
        llm_provider=getattr(answer, "provider", None),
        tokens_in=getattr(answer, "tokens_in", 0),
        tokens_out=getattr(answer, "tokens_out", 0),
        latency_ms=_latency_ms,
        error=(None if _llm_ok else "LLM call failed"),
    ))

    return QueryLLMResponse(
        question=req.question,
        answer=answer,
        grounded=grounded,
    )


@app.post("/query", response_model=QueryLLMResponse)
async def query_combined(
    req: QueryLLMRequest,
    request: Request,
    _: None = Depends(verify_token),
):
    _check_rate_limit(request)
    """
    Convenience endpoint — does both calls in one request.
    Fetches docs internally then passes them to the LLM.

    Use /query-docs + /query-llm separately if you want to inspect
    or cache the chunks. Use this if you just want a quick answer.

    Example:
        POST /query
        { "question": "what chains does Bankr support?" }
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    log.info(f"[/query] combined call, question={req.question[:80]}")

    # Step 1: fetch docs
    context = await docs.query(req.question)

    # Step 2: build grounded system prompt
    system = req.system_prompt or (
        "You are a precise support assistant for Bankr — a platform for AI agents "
        "that fund themselves through DeFi and token launching.\n\n"
        "Answer the user's question using ONLY the documentation context provided below. "
        "Do not use outside knowledge or make assumptions beyond what is in the context. "
        "If the answer is not in the context, say exactly: "
        "\"I couldn't find information about that in the Bankr documentation.\"\n\n"
        "Be concise and accurate.\n\n"
        f"--- BANKR DOCUMENTATION CONTEXT ---\n{context}\n--- END CONTEXT ---"
    )

    messages = [{"role": "user", "content": req.question}]
    _t0 = _time.time()
    answer = await ollama.chat(
        messages=messages,
        system=system,
        temperature=req.temperature or 0.1,
    )
    _latency_ms = int((_time.time() - _t0) * 1000)

    # Stats row. /query is always grounded, so doc-gap detection applies here.
    _llm_ok = getattr(answer, "ok", True)
    _doc_gap = "couldn't find information about that in the Bankr documentation" in answer
    asyncio.ensure_future(db.log_conversation(
        source="api",
        question=req.question,
        topic=None,
        response_source=("error" if not _llm_ok else "docs"),
        resolved_by_bot=_llm_ok,
        doc_gap=_doc_gap,
        llm_provider=getattr(answer, "provider", None),
        tokens_in=getattr(answer, "tokens_in", 0),
        tokens_out=getattr(answer, "tokens_out", 0),
        latency_ms=_latency_ms,
        error=(None if _llm_ok else "LLM call failed"),
    ))

    return QueryLLMResponse(
        question=req.question,
        answer=answer,
        grounded=True,
    )


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=API_SERVER_PORT,
        reload=False,
    )
