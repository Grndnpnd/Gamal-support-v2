"""
llm_router.py
-------------
LLM provider routing with automatic failover.

Background: the bot's primary LLM is the Bankr LLM Gateway, accessed through
`OllamaClient` in shared.py (named that for historical reasons — it predates
the Bankr migration; see that file's docstring). When Bankr has an outage,
the whole bot goes dark. This module adds a fallback provider so a Bankr
outage degrades to "answers come from the backup model" instead of
"every user gets an error."

Components:
  - OllamaCloudClient — talks to Ollama Cloud's native /api/chat endpoint,
    used as the fallback. Default model: gemma4:31b-cloud, thinking disabled.
  - LLMRouter — exposes the same .chat() signature as OllamaClient. Tries the
    primary (Bankr) first; on failure, transparently retries the fallback
    (Ollama Cloud). Callers don't know or care which provider answered.

Routing strategy: fail-slow. Every request attempts Bankr first and only
falls through on failure. We do NOT use a circuit breaker — Bankr's observed
failure mode (the 2026-05-22 outage) was an instant HTTP 500, not a slow
timeout, so the wasted primary attempt costs a few hundred ms, not 30s. If
Bankr ever starts failing slowly instead, revisit and add a breaker.

The router is a drop-in replacement: bot.py and api_server.py change
`OllamaClient()` to `LLMRouter()` and nothing else.

ENV vars:
  OLLAMA_CLOUD_KEY      - Ollama Cloud API key (from ollama.com/settings/keys)
  OLLAMA_CLOUD_URL      - base URL, default https://ollama.com
  OLLAMA_FALLBACK_MODEL - fallback model id, default gemma4:31b-cloud
  (Bankr vars are read by OllamaClient itself — see shared.py)
"""

import asyncio
import logging
import os
import time

import aiohttp

from shared import OllamaClient
from llm_response import LLMResponse

log = logging.getLogger(__name__)

OLLAMA_CLOUD_KEY      = os.getenv("OLLAMA_CLOUD_KEY", "")
OLLAMA_CLOUD_URL      = os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com")
OLLAMA_FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "gemma4:31b-cloud")


# A sentinel substring that OllamaClient.chat() returns in its error strings.
# OllamaClient never raises on HTTP errors — it returns a friendly message.
# The router needs to distinguish "real answer" from "primary failed", so we
# treat these known error strings as failure signals. Keeping this list in
# sync with shared.py's OllamaClient.chat() error returns is important.
_BANKR_ERROR_SIGNALS = (
    "Sorry, the AI backend is temporarily unavailable",   # 402
    "I'm handling a lot of requests right now",            # 429
    "Sorry, I ran into an issue generating a response",    # other non-200 (incl. 500)
    "Sorry, the response took too long",                   # timeout
    "Sorry, I couldn't connect to the AI backend",         # connection error
)


def _looks_like_bankr_failure(answer: str) -> bool:
    """
    OllamaClient.chat() returns a friendly string instead of raising on
    failure. Detect those known failure strings so the router can fall
    through to the backup. A normal answer never contains these.
    """
    if not answer:
        return True
    return any(sig in answer for sig in _BANKR_ERROR_SIGNALS)


# ─── Ollama Cloud fallback client ─────────────────────────────────────────────

class OllamaCloudClient:
    """
    Async client for Ollama Cloud's native /api/chat endpoint.

    This is a real Ollama Cloud connection — not the Bankr gateway, despite
    shared.py's primary client also being named "Ollama" for legacy reasons.

    Endpoint:    POST {OLLAMA_CLOUD_URL}/api/chat
    Auth:        Authorization: Bearer {OLLAMA_CLOUD_KEY}
    Model:       gemma4:31b-cloud by default (env-overridable)
    Thinking:    disabled via "think": false — Gemma 4 accepts the boolean.
                 Even if a trace were emitted it lands in message.thinking,
                 separate from message.content, so it can't leak to Discord.

    Note the response shape differs from the Bankr gateway. Bankr is
    OpenAI-compatible (choices[0].message.content); the native Ollama API
    returns message.content directly, with token counts in prompt_eval_count
    and eval_count.
    """

    def __init__(
        self,
        base_url: str = OLLAMA_CLOUD_URL,
        model: str = OLLAMA_FALLBACK_MODEL,
        api_key: str = OLLAMA_CLOUD_KEY,
    ):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self.api_key  = api_key

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float = 0.3,
    ) -> LLMResponse:
        """
        Returns an LLMResponse carrying token usage and provider='ollama_cloud'.
        Raises on failure (unlike OllamaClient, which returns an ok=False
        LLMResponse) — the router catches and handles it.
        """
        if not self.api_key:
            raise RuntimeError("OLLAMA_CLOUD_KEY not set — fallback unavailable")

        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        payload = {
            "model": self.model,
            "messages": all_messages,
            "stream": False,
            "think": False,        # no reasoning trace — fast, clean replies
            "options": {
                "temperature": temperature,
            },
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"Ollama Cloud error {resp.status}: {text[:200]}"
                    )
                data = await resp.json()

        # Native Ollama API shape: {"message": {"content": "..."}, ...}
        # Token counts are prompt_eval_count / eval_count (NOT the OpenAI names).
        message = data.get("message") or {}
        content = message.get("content", "")
        if not content:
            raise RuntimeError(f"Ollama Cloud returned no content: {str(data)[:200]}")
        return LLMResponse(
            content,
            provider="ollama_cloud",
            tokens_in=data.get("prompt_eval_count", 0) or 0,
            tokens_out=data.get("eval_count", 0) or 0,
            ok=True,
        )


# ─── Router ───────────────────────────────────────────────────────────────────

class LLMRouter:
    """
    Drop-in replacement for OllamaClient. Same .chat() signature.

    Tries the Bankr gateway first (the primary). If that fails — detected
    either by OllamaClient returning one of its known error strings, or by
    an unexpected exception — transparently retries the Ollama Cloud
    fallback. If BOTH fail, returns a friendly degraded-mode message so the
    user gets something coherent rather than a stack trace.

    Fail-slow by design: no circuit breaker, every call tries Bankr first.
    See module docstring for the rationale.
    """

    def __init__(self):
        self.primary  = OllamaClient()           # Bankr gateway (from shared.py)
        self.fallback = OllamaCloudClient()      # Ollama Cloud (Gemma)

        if not self.fallback.api_key:
            log.warning(
                "OLLAMA_CLOUD_KEY not set — LLM fallback is DISABLED. "
                "A Bankr outage will take the bot down. Set it to enable failover."
            )
        else:
            log.info(
                f"LLM router ready — primary: Bankr gateway, "
                f"fallback: Ollama Cloud ({self.fallback.model})"
            )

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float = 0.3,
    ) -> LLMResponse:
        # ── Attempt 1: Bankr (primary) ───────────────────────────────────────
        try:
            t0 = time.monotonic()
            answer = await self.primary.chat(messages, system=system, temperature=temperature)
            # answer is an LLMResponse. .ok is False for a swallowed error;
            # _looks_like_bankr_failure is a belt-and-suspenders text check in
            # case some path returns a plain string.
            ok = getattr(answer, "ok", True) and not _looks_like_bankr_failure(answer)
            if ok:
                return answer
            log.warning(
                f"Bankr primary failed (returned error after "
                f"{time.monotonic() - t0:.1f}s) — falling back to Ollama Cloud"
            )
        except Exception as e:
            log.warning(f"Bankr primary raised {type(e).__name__}: {e} — falling back")

        # ── Attempt 2: Ollama Cloud (fallback) ───────────────────────────────
        if not self.fallback.api_key:
            log.error("Bankr failed and no fallback configured — returning degraded message")
            return LLMResponse(
                "Sorry — our AI assistant is temporarily unavailable. "
                "Please try again in a little while, or ask a team member for help.",
                provider=None, ok=False,
            )

        try:
            t0 = time.monotonic()
            answer = await self.fallback.chat(messages, system=system, temperature=temperature)
            log.info(
                f"Fallback (Ollama Cloud / {self.fallback.model}) answered in "
                f"{time.monotonic() - t0:.1f}s"
            )
            return answer
        except Exception as e:
            log.error(f"Fallback also failed ({type(e).__name__}: {e}) — returning degraded message")
            return LLMResponse(
                "Sorry — our AI assistant is temporarily unavailable. "
                "Please try again in a little while, or ask a team member for help.",
                provider=None, ok=False,
            )
