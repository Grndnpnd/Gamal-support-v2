"""
shared.py
---------
Shared components used by both bot.py and api_server.py.
Keeps SemanticDocsManager and OllamaClient in one place so
there's no duplication between the Discord bot and the API server.
"""

import asyncio
import aiohttp
import re
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

import chromadb
from chromadb.utils import embedding_functions

load_dotenv()

log = logging.getLogger(__name__)

# ─── Config (read once, shared everywhere) ────────────────────────────────────

BANKR_LLM_KEY   = os.getenv("BANKR_LLM_KEY", "")
BANKR_LLM_URL   = os.getenv("BANKR_LLM_URL", "https://llm.bankr.bot")
BANKR_LLM_MODEL = os.getenv("BANKR_LLM_MODEL", "gemini-3-flash")

DOCS_URL              = os.getenv("DOCS_URL", "https://docs.bankr.bot/llms-full.txt")
DOCS_REFRESH_HOURS    = int(os.getenv("DOCS_REFRESH_HOURS", "12"))

CHUNK_SIZE          = int(os.getenv("CHUNK_SIZE", "600"))
CHUNK_OVERLAP       = int(os.getenv("CHUNK_OVERLAP", "80"))
TOP_K_CHUNKS        = int(os.getenv("TOP_K_CHUNKS", "6"))
MAX_RETRIEVED_CHARS = int(os.getenv("MAX_RETRIEVED_CHARS", "8000"))


# ─── Semantic Docs Manager ────────────────────────────────────────────────────

class SemanticDocsManager:
    """
    Fetches Bankr docs from DOCS_URL, chunks and embeds them into ChromaDB,
    and exposes a semantic search query method.

    Both bot.py and api_server.py share a single instance of this so the
    docs are only fetched and indexed once.
    """

    def __init__(self):
        self.raw_content: str = ""
        self.last_fetched: datetime | None = None
        self._ready: bool = False

        self._client = chromadb.Client()
        self._ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        self._collection = self._client.get_or_create_collection(
            name="bankr_docs",
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )

    async def ensure_ready(self):
        now = datetime.utcnow()
        if (
            not self._ready
            or not self.last_fetched
            or now - self.last_fetched > timedelta(hours=DOCS_REFRESH_HOURS)
        ):
            await self._fetch_and_index()

    async def query(self, question: str, top_k: int | None = None) -> str:
        """
        Semantic search over the docs. Returns a string of relevant chunks
        joined by separators, trimmed to MAX_RETRIEVED_CHARS.
        """
        await self.ensure_ready()

        if not self._ready:
            return "Documentation unavailable. Please check docs.bankr.bot directly."

        k = min(top_k or TOP_K_CHUNKS, self._collection.count())
        results = self._collection.query(
            query_texts=[question],
            n_results=k,
        )

        chunks = results["documents"][0] if results["documents"] else []
        if not chunks:
            return "No relevant documentation found."

        combined = "\n\n---\n\n".join(chunks)
        if len(combined) > MAX_RETRIEVED_CHARS:
            combined = combined[:MAX_RETRIEVED_CHARS] + \
                "\n\n[Context trimmed — full docs at docs.bankr.bot]"

        return combined

    async def query_chunks(self, question: str, top_k: int | None = None) -> list[dict]:
        """
        Like query() but returns structured list of chunks with metadata.
        Used by the API so callers can see individual chunks and distances.
        """
        await self.ensure_ready()

        if not self._ready:
            return []

        k = min(top_k or TOP_K_CHUNKS, self._collection.count())
        results = self._collection.query(
            query_texts=[question],
            n_results=k,
            include=["documents", "distances", "metadatas"],
        )

        chunks = []
        docs      = results.get("documents", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        for i, doc in enumerate(docs):
            chunks.append({
                "text": doc,
                "distance": round(distances[i], 4) if i < len(distances) else None,
                "index": metadatas[i].get("index") if i < len(metadatas) else None,
            })

        return chunks

    # ── Internal ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str) -> list[str]:
        paragraphs = re.split(r"\n{2,}", text)
        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current) + len(para) + 2 <= CHUNK_SIZE:
                current = (current + "\n\n" + para).strip()
            else:
                if current:
                    chunks.append(current)
                overlap_text = current[-CHUNK_OVERLAP:] if current else ""
                current = (overlap_text + "\n\n" + para).strip() if overlap_text else para

                while len(current) > CHUNK_SIZE:
                    chunks.append(current[:CHUNK_SIZE])
                    current = current[CHUNK_SIZE - CHUNK_OVERLAP:]

        if current:
            chunks.append(current)

        return [c for c in chunks if len(c) > 40]

    def _index_docs(self):
        chunks = self._chunk_text(self.raw_content)
        log.info(f"Chunked into {len(chunks)} segments — embedding...")

        try:
            self._collection.delete(where={"source": "bankr_docs"})
        except Exception:
            pass

        batch_size = 64
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            self._collection.add(
                documents=batch,
                ids=[f"chunk_{i + j}" for j in range(len(batch))],
                metadatas=[{"source": "bankr_docs", "index": i + j}
                           for j in range(len(batch))],
            )

        log.info(f"Indexed {len(chunks)} chunks into ChromaDB")

    async def _fetch_and_index(self):
        log.info(f"Fetching docs from {DOCS_URL}...")
        try:
            headers = {"Accept-Encoding": "gzip, deflate"}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    DOCS_URL,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        self.raw_content = await resp.text()
                        self.last_fetched = datetime.utcnow()
                        log.info(
                            f"Docs fetched ({len(self.raw_content):,} chars) — indexing..."
                        )
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._index_docs
                        )
                        self._ready = True
                        log.info("Docs ready.")
                    else:
                        log.error(f"Failed to fetch docs: HTTP {resp.status}")
        except Exception as e:
            log.error(f"Error fetching/indexing docs: {e}")
            if not self.raw_content:
                self.raw_content = "Documentation unavailable."


# ─── LLM Client (Bankr Gateway) ──────────────────────────────────────────────

class OllamaClient:
    """
    Async client for the Bankr LLM Gateway (OpenAI-compatible format).
    Named OllamaClient for import compatibility — no changes needed in callers.

    Base URL:      https://llm.bankr.bot
    Auth header:   X-API-Key: bk_YOUR_KEY  (also accepts Authorization: Bearer)
    Endpoint:      POST /v1/chat/completions
    Default model: gemini-3-flash  ($0.50/M input, $3.00/M output)

    Get your key at bankr.bot/api with LLM Gateway enabled.
    Top up credits at bankr.bot/llm before first use.
    """

    def __init__(
        self,
        base_url: str = BANKR_LLM_URL,
        model: str = BANKR_LLM_MODEL,
        api_key: str | None = BANKR_LLM_KEY,
    ):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self.api_key  = api_key

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            # Bankr gateway accepts both X-API-Key and Authorization: Bearer
            h["X-API-Key"] = self.api_key
        return h

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float = 0.3,
    ) -> str:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        # Bankr gateway uses OpenAI-compatible /v1/chat/completions format
        payload = {
            "model": self.model,
            "messages": all_messages,
            "stream": False,
            "temperature": temperature,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # OpenAI-compatible response shape
                        return data["choices"][0]["message"]["content"]
                    elif resp.status == 402:
                        log.error("Bankr LLM Gateway: insufficient credits (402). Top up at bankr.bot/llm")
                        return "Sorry, the AI backend is temporarily unavailable. Please try again shortly."
                    elif resp.status == 429:
                        log.warning("Bankr LLM Gateway: rate limited (429)")
                        return "I'm handling a lot of requests right now — please try again in a moment."
                    else:
                        text = await resp.text()
                        log.error(f"Bankr LLM Gateway error {resp.status}: {text}")
                        return "Sorry, I ran into an issue generating a response."
        except asyncio.TimeoutError:
            return "Sorry, the response took too long. Please try again."
        except Exception as e:
            log.error(f"Bankr LLM Gateway request failed: {e}")
            return "Sorry, I couldn't connect to the AI backend."
