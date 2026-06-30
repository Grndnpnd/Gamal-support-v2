"""
article_sync.py
---------------
The propose-side of the help-center article sync feature.

Two passes, each optimized for its own job:

  Pass 1 — Freshness check (per existing article)
    For each of the 22 mapped articles, use SemanticDocsManager to find
    the docs chunks most relevant to that article's title, then ask the
    LLM whether the article still reflects the docs. Output:
      unchanged / needs_update / orphan
    The LLM also writes a new article body when needs_update.

  Pass 2 — Gap detection (across the docs)
    Take the docs' real top-level structure (the H2 headers, filtered to
    skip code-block noise and placeholder examples). Ask the LLM, in a
    single structured call, whether each docs section is meaningfully
    covered by the 22 existing articles. Output: a list of suggested
    new articles for human review.

WHY THIS SHAPE
  Bankr's llms-full.txt is a 548k-char concatenation with ~1600 markdown
  headers, many inside code blocks and many as inline-example placeholders
  like 'My Skill'. Section-diff approaches that assume clean hierarchy
  collapse against this format. Pass 1 doesn't care about doc structure —
  it asks "what's relevant to this article?" and lets ChromaDB answer.
  Pass 2 uses structure but only at the level Bankr actually maintains
  cleanly (H2), with filtering for the known noise.

  We dropped the "skip if docs unchanged since last sync" optimization
  that diff-based proposing offered. Sync runs infrequently and quality
  matters more than cost — explicit choice by the admin.

NO WRITES TO PLAIN HERE. Returns a Proposal dict. Stage 3 (review/publish
UI) is what actually upserts.

NEVER RAISES on a per-article failure — that one article's item is marked
kind='error' and the run continues, so one bad LLM response doesn't kill
the whole batch.
"""

import asyncio
import json
import logging
import re
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from llm_router import LLMRouter
from llm_response import LLMResponse
from plain_client import PlainClient
from shared import SemanticDocsManager
import plain_articles

log = logging.getLogger(__name__)


# ─── Proposal data classes ───────────────────────────────────────────────────

@dataclass
class ProposalItem:
    """One row in the review UI."""
    kind:          str                       # see below
    slug:          str
    title:         str
    plain_id:      Optional[str] = None
    group_id:      str = ""
    group_name:    str = ""
    status:        Optional[str] = None    # existing Plain status (PUBLISHED/DRAFT)
                                           # preserved so updates don't unpublish
    current_html:  Optional[str] = None
    proposed_html: Optional[str] = None
    proposed_description: Optional[str] = None
    reason:        str = ""
    docs_excerpt:  Optional[str] = None
    audit_flags:   list = field(default_factory=list)
                                # completeness-audit residue: documented facts
                                # the audit found still missing AFTER the auto-
                                # revision pass. Each: {fact, severity, why}.
                                # Surfaced in the review UI as a warning so a
                                # human can eyeball before publishing. Empty =
                                # audit found nothing missing (the good case).

# kind values:
#   "unchanged"     article still reflects the docs — no action needed
#   "needs_update"  article should be rewritten; proposed_html is the new body
#   "orphan"        article's topic is no longer well-represented in docs
#                   (human review only — we do not auto-archive)
#   "new_topic"     suggested new article — for review only, no proposed_html
#                   because we don't auto-generate new article bodies yet
#   "error"         the LLM step failed for this item; surfaced so the
#                   admin can re-run or skip


@dataclass
class Proposal:
    decision:     str
    summary:      dict = field(default_factory=dict)
    docs_hash:    str = ""
    items:        list[ProposalItem] = field(default_factory=list)
    generated_at: str = ""
    tokens_in:    int = 0
    tokens_out:   int = 0
    error:        Optional[str] = None
    notes:        list[str] = field(default_factory=list)    # diagnostics

    def to_dict(self) -> dict:
        return asdict(self)


def _html_to_text_excerpt(html: str, max_chars: int = 800) -> str:
    """
    Strip HTML tags and collapse whitespace to produce a plain-text preview
    of an article body, suitable for feeding into an LLM prompt without
    burning tokens on markup.

    Used by Pass 2 (gap detection): we want the LLM to judge coverage based
    on what each article actually says, not its tag soup. A plain-text 800-
    char excerpt captures the topic + approach + key terms with minimal
    prompt-cost overhead.

    Not a full HTML sanitizer — just a quick token-conscious flattener.
    Handles the small subset of tags we generate (p, h2, h3, ol/ul/li,
    code, pre, a, strong, em). Drops everything else's tags but keeps text.
    """
    if not html:
        return ""
    # Drop script/style blocks entirely (defense-in-depth — we don't generate
    # them, but if Plain ever returns one we don't want it in the LLM prompt).
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.IGNORECASE | re.DOTALL)
    # Add a space at block-level tag boundaries so words don't run together
    text = re.sub(r"</(p|h\d|li|div|br|tr|td|th)>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode the few HTML entities we commonly emit
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


# ─── Multi-query union retrieval (Pass 1 recall) ─────────────────────────────
#
# The original Pass 1 retrieved chunks with ONE query: "<title> (<group>)".
# That anchors retrieval on the article's identity, which is great for
# "is this article's existing content still accurate" but blind to NEW
# subtopics the docs grew that this article SHOULD now cover. Concrete
# failure that motivated this: docs added a "creator vesting" section to
# token launching; the query "Launching a Token (Token Launching)" never
# surfaced the vesting chunks in its top-k, so the rewrite couldn't include
# what it never saw. Because the help-center articles are the authoritative
# layer Plain's AI reads first (and stops at), a recall miss here silently
# propagates a stale answer all the way to the user.
#
# Fix: retrieve with SEVERAL queries and union the results, deduped by chunk
# index. The query set casts a wider net:
#   1. title + group          — article identity (original behavior)
#   2. each section heading    — catches "an existing section went stale"
#      pulled from current_html
#   3. a broad subject query   — catches "an adjacent new subtopic appeared"
#      (title + group + generic facet terms)
# The union is sorted by best distance and capped so prompt cost stays bounded.

# Block-level HTML heading tags we emit in article bodies.
_ARTICLE_HEADING_RE = re.compile(
    r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL
)


def _extract_article_headings(html: str, max_headings: int = 8) -> list[str]:
    """
    Pull section heading texts out of an article's HTML body so each can seed
    its own retrieval query. Returns cleaned, deduped heading strings (order
    preserved), capped at max_headings to bound the number of queries.
    """
    if not html:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _ARTICLE_HEADING_RE.finditer(html):
        # Strip any nested tags inside the heading, collapse whitespace.
        raw = re.sub(r"<[^>]+>", "", m.group(1))
        raw = re.sub(r"\s+", " ", raw).strip()
        # Skip empties and trivially short headings (e.g. a stray "&nbsp;").
        if len(raw) < 3:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
        if len(out) >= max_headings:
            break
    return out


async def _retrieve_union(
    docs_manager,
    *,
    title: str,
    group_name: str,
    current_html: Optional[str] = None,
    per_query_k: int = 8,
    union_cap: int = 24,
) -> list[dict]:
    """
    Multi-query union retrieval for one article.

    Runs several semantically-distinct queries through docs_manager and unions
    the returned chunks, deduped by chunk index, sorted by best (lowest)
    distance, capped at union_cap.

    Queries issued:
      - "<title> (<group>)"                       — article identity
      - each section heading in current_html      — existing-section freshness
      - "<title> <group> overview details fees    — broad facet expansion that
         limits options requirements"                surfaces adjacent subtopics
                                                      not yet in the article

    Returns the same chunk dict shape as query_chunks: {text, distance, index}.
    Falls back gracefully — any individual query that errors or returns nothing
    just contributes no chunks; the union is best-effort.
    """
    queries: list[str] = [f"{title} ({group_name})"]

    for heading in _extract_article_headings(current_html or ""):
        # Pair the heading with the article title so retrieval stays in-domain
        # ("Fees" alone is ambiguous across the docs; "Fees Launching a Token"
        # disambiguates toward the right chunks).
        queries.append(f"{heading} {title}")

    # Broad facet expansion — the catch-all that surfaces brand-new subtopics
    # the article doesn't mention yet. Generic facet terms ("fees", "limits",
    # "requirements", etc.) bias retrieval toward the kinds of details support
    # articles need to stay complete, without naming any specific subtopic.
    queries.append(
        f"{title} {group_name} overview details fees limits "
        f"requirements options eligibility configuration"
    )

    # Issue all queries, union by chunk index keeping the best distance seen.
    best_by_index: dict = {}
    for q in queries:
        try:
            chunks = await docs_manager.query_chunks(q, top_k=per_query_k)
        except Exception:
            log.exception(f"_retrieve_union: query_chunks failed for {q!r}")
            continue
        for c in chunks or []:
            idx = c.get("index")
            # Chunks without a stable index can't be deduped reliably; key them
            # on their text instead so we still don't double-feed identical text.
            key = idx if idx is not None else ("txt:" + (c.get("text") or "")[:64])
            dist = c.get("distance")
            prev = best_by_index.get(key)
            if prev is None or (
                dist is not None
                and prev.get("distance") is not None
                and dist < prev["distance"]
            ):
                best_by_index[key] = c

    # Sort by distance ascending (None distances sink to the end), cap.
    unioned = sorted(
        best_by_index.values(),
        key=lambda c: (c.get("distance") is None, c.get("distance") or 0.0),
    )
    return unioned[:union_cap]


# ─── Header parser (Pass 2 input prep) ───────────────────────────────────────
#
# We deliberately only look at H1 and H2 because the docs are inconsistent
# below that, AND we filter aggressively to skip the known noise patterns:
#   - headers inside ``` code fences (the docs concatenator includes them)
#   - headers that look like example placeholders ('My Skill', 'Your X')
#   - headers that look like JSON output ('→ {...}')
#   - headers that look like sequential step titles ('Step 1 — ...', '1. ...')

_HEADER_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*$", re.MULTILINE)

# Patterns that look like an H1/H2 but aren't a real section topic.
_NOISE_PATTERNS = [
    re.compile(r"^\s*my\s+(skill|workflow|task|agent|project)\b", re.IGNORECASE),
    re.compile(r"^\s*your\s+\w+\s+name\b", re.IGNORECASE),
    re.compile(r"^\s*→"),
    re.compile(r"^\s*\{"),
    re.compile(r"^\s*step\s+\d+[\s—:.-]", re.IGNORECASE),
    re.compile(r"^\s*\d+\.\s"),                          # numbered list items
    re.compile(r"^\s*(option|example)\s+\d+", re.IGNORECASE),
    re.compile(r"^\s*(interactive|headless|with\s+fee)", re.IGNORECASE),
    re.compile(r"^\s*(send|set|claim|scan|log\s+in)\s", re.IGNORECASE),
]


def _strip_code_blocks(text: str) -> str:
    """
    Remove fenced code blocks so headers inside them don't get parsed as
    real sections. We do this *before* header regex matches.
    """
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _is_noise_header(text: str) -> bool:
    """True if a header looks like a code example or step title, not a real section."""
    if len(text) > 80:
        return True   # real section names are short; long ones are usually inline
    for p in _NOISE_PATTERNS:
        if p.match(text):
            return True
    return False


def extract_topical_sections(docs_text: str) -> list[dict]:
    """
    Return a list of { header, level, body } for sections that look like
    real docs topics (filtered to drop the known noise).

    Used by Pass 2 to give the LLM a structural overview of the docs.
    Body is truncated to ~600 chars per section so the full list fits
    comfortably in one LLM call.
    """
    cleaned = _strip_code_blocks(docs_text)
    matches = list(_HEADER_RE.finditer(cleaned))
    sections: list[dict] = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        text = m.group(2).strip()
        if _is_noise_header(text):
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        body = cleaned[start:end].strip()
        sections.append({
            "header": text,
            "level": level,
            "body_preview": body[:600],
        })
    return sections


# ─── House voice (the prompts reference this) ────────────────────────────────

_HOUSE_VOICE = """\
HOUSE VOICE & STYLE — Bankr help center articles

COMPLETENESS IS THE #1 RULE. These articles are the first and often ONLY place
Bankr's support AI looks — it answers from them and stops. An article that omits
a documented detail causes a wrong or incomplete answer to reach a real user.
Therefore your job is a LOSSLESS TRANSFORMATION of the source docs into help-
article form, NOT a summary. Reframe for a support reader, but DROP NOTHING that
matters.

Non-negotiable: every one of the following that appears in the source excerpts
MUST appear in the article body —
  - Limitations and restrictions ("cannot", "only", "not supported", "no longer").
  - Irreversible or hard-to-reverse actions, and anything fixed/locked at a point
    in time ("fixed at launch", "can't be reassigned", "permanent").
  - Anything that affects money, ownership, access, or eligibility (who can/can't
    claim, what transfers and what does NOT transfer, fees, cliffs, vesting,
    spending limits, daily caps).
  - Conditions, prerequisites, and gotchas ("you must have X first", "requires
    ETH for gas", "only the beneficiary wallet can…").
  - Exceptions and special cases ("Partner/org launches do NOT include…").
Omitting any such caveat is a DEFECT, not a stylistic choice. When in doubt,
include it. It is far better to be thorough than tidy.

STYLE (apply on top of completeness, never at its expense):
- Plain language, not developer docs. Explain like you're talking to a smart
  friend, not someone reading an API reference — but a thorough friend who warns
  you about the sharp edges.
- Title is a real user question: "How Do I Place a Trade?" not "Trading
  Documentation."
- The title goes in the JSON "title" field, NEVER inside the contentHtml.
  Do not put <h1> at the start of contentHtml — the title is rendered
  separately by the help center. Starting contentHtml with <h1> creates a
  duplicate heading on the published page. Start the body with the first
  real content paragraph (a <p>) or an intro <h2> for a subsection.
- Practical numbered steps the user can follow.
- Surface important caveats visibly — give limitations and "cannot/does not"
  facts their own clear sentence or a dedicated "Important Limitations" /
  "Things to know" section, rather than burying them mid-paragraph.
- Include example prompts the user can copy/paste into Bankr where useful.
- Link to docs.bankr.bot at the end for technical deep-dives.
- Common troubleshooting tips where relevant.
- Code examples minimal — only when truly necessary (a user-facing CLI command
  is fine; an internal contract call should be described in plain terms with a
  pointer to the docs, not omitted).
- Tone: friendly, direct, confident — like a knowledgeable friend. Not
  corporate, not overly casual.
- Output HTML, not markdown. Use simple, semantic HTML: <p>, <ol>, <ul>,
  <li>, <h2>, <h3>, <strong>, <em>, <code>, <pre><code>...</code></pre>,
  <a href="...">. No <div>, no inline styles, no scripts."""


# ─── Robust JSON parse (handles chatty models like GLM) ──────────────────────

def _sanitize_proposed_html(html: str) -> str:
    """
    Defense-in-depth cleanup for LLM-generated article bodies.

    The LLM is told (twice — in the house-voice block and in the write
    prompt) not to put the article title in the body, because Plain renders
    the title field separately above the body. Despite that, some runs
    still produce contentHtml that starts with an <h1>Title</h1> tag, which
    creates a duplicate heading on the published page.

    This function strips a leading <h1>…</h1> if present. Only the *first*
    one, only if it's at the very start (after optional whitespace), so we
    don't accidentally remove a meaningful sub-heading deeper in the body.
    The article's title is carried separately in ProposalItem.title.
    """
    if not html:
        return html
    # Match a leading <h1>...</h1> with optional surrounding whitespace
    return re.sub(
        r"^\s*<h1[^>]*>.*?</h1>\s*",
        "",
        html,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _extract_json_object(text: str) -> Optional[dict]:
    """
    Pull a JSON object out of LLM output that may include preamble prose,
    markdown fences, or both. Returns the parsed dict, or None if no valid
    JSON object can be found.

    Strategy:
      1. Strip ```json / ``` fences if present.
      2. Try to parse the whole thing as JSON.
      3. If that fails, find the first '{' and try to parse from there to
         the matching balanced '}'. Handles "Looking at the changes... { ... }"
         style chatty model output.
    """
    if not text:
        return None
    t = text.strip()
    # 1. strip markdown fences (with or without language tag)
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    # 2. straight parse
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # 3. find first balanced object
    start = t.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(t)):
            c = t[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = t[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        # try next opening brace if the current one didn't yield valid JSON
        start = t.find("{", start + 1)
    return None


# ─── Pass 1: Freshness check (per article) ───────────────────────────────────

def _pass1_system_prompt() -> str:
    return f"""You are reviewing one Bankr help center article against the most relevant chunks of the current Bankr documentation.

You will receive:
- The article's current title, slug, category, and HTML body.
- A set of documentation excerpts retrieved as most relevant to this article's
  topic. These are gathered by MULTIPLE queries (the article's title, each of
  its section headings, and a broad subject sweep), so the set may include
  excerpts about subtopics the article does NOT yet mention. That is
  intentional — part of your job is spotting documented subtopics the article
  is missing.

These help center articles are the FIRST place Bankr's support AI looks, and it
stops once it finds an answer here — so an article that omits a documented
detail causes the support AI to give an incomplete answer. Completeness matters
as much as accuracy.

Decide which one of these applies to this article:
- "unchanged"    — the current article still accurately AND completely reflects
                   the docs. Pick this only if there is no substantive content
                   drift and no documented subtopic is missing. Cosmetic
                   differences (whitespace, ordering, small wording) do not
                   count — ignore them.
- "needs_update" — the article is missing information that the docs cover, has
                   outdated information, or could be meaningfully more complete.
                   A documented subtopic present in the excerpts but absent from
                   the article body is a needs_update (e.g. the docs describe a
                   fee/vesting/limit/eligibility detail the article never
                   mentions). Be honest but conservative about cosmetic noise —
                   but treat genuinely missing documented content as a real
                   miss, not a nitpick.
- "orphan"       — the retrieved docs are weak matches (the article's topic
                   isn't really documented anymore, OR the article is about
                   something Bankr no longer does). The reviewer will look at
                   this manually.

If you pick "needs_update", ALSO produce the full new article: a title (real
user question), a one-sentence description for search, and the HTML body. The
new body must fold in every documented detail from the excerpts that belongs in
this article — don't just patch the one thing you noticed; make it complete.

{_HOUSE_VOICE}

Respond ONLY with a single JSON object. No preamble, no markdown fence:
{{
  "decision":  "unchanged" | "needs_update" | "orphan",
  "reason":    "one or two sentences explaining the decision",
  "new_article": null  OR  {{
    "title":       "How Do I ...?",
    "description": "One-sentence summary for search.",
    "contentHtml": "<p>...</p><ol><li>...</li></ol>..."
  }}
}}
"""


# ─── Completeness audit (catches summarization drops) ────────────────────────
#
# Even with strong retrieval and a completeness-first prompt, an LLM rewriting
# docs into an article will sometimes drop a caveat it judged less important —
# and the dropped ones are disproportionately the dangerous edge cases (e.g.
# "the vested allocation can't be reassigned"). Because Plain's AI answers from
# these articles and stops, a dropped caveat is a wrong answer shipped to a user.
#
# The audit is a focused second LLM pass: given the SOURCE chunks and the
# GENERATED article, list every documented caveat / limitation / restriction /
# money-or-access-affecting fact that is present in the chunks but ABSENT from
# the article. If gaps are found we attempt ONE auto-revision that folds them
# in; any gaps still missing after revision are attached to the proposal item
# so a human sees them in the review UI before publishing.

def _audit_system_prompt() -> str:
    return """You are auditing a drafted Bankr help center article for COMPLETENESS against the source documentation excerpts it was written from.

You will receive:
- "source_chunks": the documentation excerpts the article should faithfully cover.
- "article_html": the drafted article body.

Find every fact in source_chunks that a support reader would need but that is
MISSING from article_html. Focus hard on the high-stakes kinds of facts:
- Limitations / restrictions ("cannot", "only", "not supported", "no longer").
- Irreversible or locked-at-a-point-in-time facts ("fixed at launch", "can't be
  reassigned", "permanent").
- Anything affecting money, ownership, access, or eligibility (who can/can't do
  something, what transfers and what does NOT, fees, cliffs, vesting, caps).
- Prerequisites and gotchas; exceptions and special cases.

Do NOT report:
- Stylistic differences, wording, ordering, or formatting.
- Developer/API minutiae that genuinely doesn't belong in a user help article
  (raw endpoint schemas, internal contract ABIs) UNLESS the user-facing
  consequence is missing (the consequence must be stated even if the mechanism
  isn't).
- Facts already present in the article, even if phrased differently.

Respond ONLY with a single JSON object, no preamble, no markdown fence:
{
  "missing": [
    {
      "fact": "Short statement of the missing fact, in plain language.",
      "severity": "high" | "medium" | "low",
      "why": "One short clause on why a user needs this."
    }
  ]
}
If nothing material is missing, return {"missing": []}."""


async def _audit_article_completeness(
    router: "LLMRouter",
    *,
    article_html: str,
    docs_chunks: list[dict],
) -> list[dict]:
    """
    Run the completeness audit. Returns a list of missing-fact dicts
    (possibly empty). Best-effort: on any LLM/parse failure returns [] so the
    audit never blocks a sync — it can only ADD safety, never remove output.
    """
    if not article_html or not docs_chunks:
        return []
    payload = {
        "source_chunks": [
            {"chunk_index": c.get("index"), "text": c["text"]}
            for c in docs_chunks
        ],
        "article_html": article_html,
    }
    try:
        resp = await router.chat(
            messages=[{"role": "user",
                       "content": json.dumps(payload, ensure_ascii=False)}],
            system=_audit_system_prompt(),
            temperature=0.0,
        )
    except Exception:
        log.exception("_audit_article_completeness: LLM call failed")
        return []
    parsed = _extract_json_object(str(resp))
    if not parsed or "missing" not in parsed:
        return []
    missing = parsed.get("missing") or []
    # Normalize / guard shape.
    out = []
    for m in missing:
        if not isinstance(m, dict):
            continue
        fact = str(m.get("fact", "")).strip()
        if not fact:
            continue
        sev = str(m.get("severity", "medium")).lower()
        if sev not in ("high", "medium", "low"):
            sev = "medium"
        out.append({"fact": fact, "severity": sev,
                    "why": str(m.get("why", "")).strip()})
    return out


def _revision_system_prompt() -> str:
    return f"""You are REVISING a Bankr help center article to fold in documented facts that an audit found MISSING. Keep everything already correct in the article; ADD the missing facts in the right places, surfaced clearly (a dedicated limitation/caveat belongs in a visible sentence or an "Important Limitations" / "Things to know" section, not buried).

You will receive:
- "article_html": the current article body.
- "missing_facts": the facts to incorporate.
- "source_chunks": the source docs, for accurate wording of the added facts.

Produce the full revised article. Do not drop anything that was already there.

{_HOUSE_VOICE}

Respond ONLY with a single JSON object, no preamble, no markdown fence:
{{
  "title":       "How Do I ...?",
  "description": "One-sentence summary for search.",
  "contentHtml": "<p>...</p>..."
}}"""


async def _revise_article_with_missing(
    router: "LLMRouter",
    *,
    title: str,
    description: Optional[str],
    article_html: str,
    missing: list[dict],
    docs_chunks: list[dict],
) -> Optional[dict]:
    """
    One auto-revision pass that folds missing facts into the article. Returns a
    new_article dict {title, description, contentHtml} or None on failure (caller
    keeps the pre-revision article and surfaces the gaps in the review UI).
    """
    if not missing:
        return None
    payload = {
        "article_html": article_html,
        "missing_facts": missing,
        "source_chunks": [
            {"chunk_index": c.get("index"), "text": c["text"]}
            for c in docs_chunks
        ],
        "current_title": title,
        "current_description": description or "",
    }
    try:
        resp = await router.chat(
            messages=[{"role": "user",
                       "content": json.dumps(payload, ensure_ascii=False)}],
            system=_revision_system_prompt(),
            temperature=0.1,
        )
    except Exception:
        log.exception("_revise_article_with_missing: LLM call failed")
        return None
    parsed = _extract_json_object(str(resp))
    if not parsed or not parsed.get("contentHtml"):
        return None
    return {
        "title":       str(parsed.get("title") or title),
        "description": (str(parsed.get("description"))
                        if parsed.get("description") else description),
        "contentHtml": str(parsed["contentHtml"]),
    }


async def _judge_one_article(
    router: "LLMRouter",
    article_entry: dict,
    current_plain: Optional[dict],
    docs_chunks: list[dict],
) -> dict:
    """
    Run Pass 1 for one article. Returns:
      {
        "decision":   "unchanged" | "needs_update" | "orphan" | "error",
        "reason":     str,
        "new_article": dict | None,
        "tokens_in":  int,
        "tokens_out": int,
      }
    """
    current_html = (current_plain or {}).get("contentHtml") or ""
    # Use the article's current title + slug + group as the search subject
    user_payload = {
        "article": {
            "slug":         article_entry["slug"],
            "current_title": (current_plain or {}).get("title") or article_entry["title"],
            "category":     article_entry["group_name"],
            "current_html": current_html,
        },
        "relevant_docs_chunks": [
            {"chunk_index": c.get("index"), "text": c["text"]}
            for c in docs_chunks
        ],
    }
    resp: LLMResponse = await router.chat(
        messages=[{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        system=_pass1_system_prompt(),
        temperature=0.15,
    )
    tin  = getattr(resp, "tokens_in", 0) or 0
    tout = getattr(resp, "tokens_out", 0) or 0
    ok   = getattr(resp, "ok", True)
    if not ok:
        return {"decision": "error", "reason": "LLM call failed",
                "new_article": None, "tokens_in": tin, "tokens_out": tout}

    parsed = _extract_json_object(str(resp))
    if not parsed or "decision" not in parsed:
        log.error(
            f"Pass1 LLM returned unparseable output for {article_entry['slug']}: "
            f"{str(resp)[:200]!r}"
        )
        return {"decision": "error", "reason": "LLM returned unparseable output",
                "new_article": None, "tokens_in": tin, "tokens_out": tout}

    decision = parsed.get("decision", "unchanged")
    if decision not in ("unchanged", "needs_update", "orphan"):
        decision = "unchanged"
    return {
        "decision":   decision,
        "reason":     str(parsed.get("reason", "")).strip(),
        "new_article": parsed.get("new_article") if decision == "needs_update" else None,
        "tokens_in":  tin,
        "tokens_out": tout,
    }


# ─── Pass 2: Gap detection (one call across the whole docs) ──────────────────

def _pass2_system_prompt() -> str:
    return f"""You are reviewing the structural top-level sections of the Bankr documentation against the current set of 22 help center articles, including the actual content each article covers.

Your job: identify documentation sections that describe a meaningful topic NOT covered by any existing help center article. These are candidates for new articles.

CRITICAL: Existing articles cover MORE than their titles suggest. A topic is "covered" if it is meaningfully discussed in the body of an existing article, not only if it's named in the title. Examples of correct judgment:
- Docs section "Hyperliquid Trading" + existing "Leveraged Trading" article whose body covers Hyperliquid as the leverage venue → COVERED, do not suggest.
- Docs section "Setting up MCP servers" + existing "Skills, MCP Servers & Env Vars" article → COVERED, do not suggest.
- Docs section "Token sniping" + no existing article mentions sniping → NOT COVERED, suggest.

You will receive:
- existing_articles: each with title, slug, category, and a body excerpt showing
  what the article actually covers. Read the excerpts before deciding.
- docs_sections: each with header and a short body preview.

Be aggressive about identifying coverage. Default to "covered" unless the docs
section describes a topic substantially absent from every existing article.
Only suggest a new article when:
- No existing article's body covers the topic (titles alone don't count)
- The topic is distinct enough that a user would search for it separately
  (not just one paragraph inside a broader existing topic)
- The topic is something a customer would care about (not internal,
  not developer-only API minutiae)

If multiple docs sections describe one new topic, fold them into one suggestion.

{_HOUSE_VOICE}

Respond ONLY with a single JSON object. No preamble, no markdown fence:
{{
  "new_article_suggestions": [
    {{
      "suggested_slug":   "kebab-case-slug",
      "suggested_title":  "How Do I ...?",
      "suggested_category": "Getting Started" | "Trading & Orders" | "Wallet & Portfolio" | "Token Launching" | "Automations" | "Bankr Club & Billing" | "Apps & Extensions" | "Security" | "Troubleshooting",
      "reason":           "One-line rationale. Cite which existing articles you considered and why they don't cover this — proves you checked bodies, not just titles.",
      "docs_sections":    ["Section header 1", "Section header 2"]
    }}
  ]
}}

If no new articles are warranted, return: {{ "new_article_suggestions": [] }}.
"""


async def _detect_gaps(
    router: LLMRouter,
    docs_sections: list[dict],
    existing_articles: list[dict],
) -> tuple[list[dict], int, int]:
    """
    Run Pass 2. Returns (suggestions, tokens_in, tokens_out).

    existing_articles items are expected to be enriched with a body_excerpt
    field: a plain-text view (HTML stripped, truncated to ~800 chars) of
    what each article actually covers. This is what lets the LLM judge
    "Hyperliquid is covered by Leveraged Trading" — without the excerpt
    the LLM only sees titles and over-suggests new articles for topics
    that are already covered in bodies.
    """
    user_payload = {
        "existing_articles": [
            {
                "title":         a["title"],
                "slug":          a["slug"],
                "category":      a["group_name"],
                "body_excerpt":  a.get("body_excerpt", ""),
            }
            for a in existing_articles
        ],
        "docs_sections": [
            {"header": s["header"], "level": s["level"], "body_preview": s["body_preview"]}
            for s in docs_sections
        ],
    }
    resp: LLMResponse = await router.chat(
        messages=[{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
        system=_pass2_system_prompt(),
        temperature=0.15,
    )
    tin  = getattr(resp, "tokens_in", 0) or 0
    tout = getattr(resp, "tokens_out", 0) or 0
    ok   = getattr(resp, "ok", True)
    if not ok:
        log.error("Pass2 LLM call failed")
        return [], tin, tout

    parsed = _extract_json_object(str(resp))
    if not parsed:
        log.error(f"Pass2 LLM returned unparseable output: {str(resp)[:300]!r}")
        return [], tin, tout

    suggestions = parsed.get("new_article_suggestions") or []
    if not isinstance(suggestions, list):
        log.error("Pass2 LLM returned non-list new_article_suggestions")
        return [], tin, tout
    return suggestions, tin, tout


# ─── Pipeline entry point ────────────────────────────────────────────────────

async def propose(
    *,
    docs_manager: SemanticDocsManager,
    plain_client: PlainClient,
    router: Optional[LLMRouter] = None,
    docs_top_k: int = 8,
    progress_cb=None,
) -> Proposal:
    """
    Run the two-pass propose pipeline end to end.

    Args:
      docs_manager  - the SemanticDocsManager the caller has already prepared
                      (await ensure_ready() before passing). We use it for
                      both Pass 1 (semantic chunks per article) and Pass 2
                      (raw_content for structural section extraction).
      plain_client  - authenticated PlainClient. We only read; no writes here.
      router        - LLMRouter. Defaults to a fresh LLMRouter().
      docs_top_k    - chunks to retrieve per article for Pass 1. Default 8.
      progress_cb   - optional async callable `progress_cb(stage: str)` invoked
                      at meaningful checkpoints. The admin panel uses this to
                      surface live progress in the status panel during the
                      ~1-3 min run. Failures in the callback are swallowed —
                      progress reporting never breaks the pipeline.

    Returns: Proposal (see dataclass). Never raises — per-article failures
    are captured as kind='error' items.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    router = router or LLMRouter()
    notes: list[str] = []
    total_in = 0
    total_out = 0

    async def _emit(stage: str):
        """Fire progress_cb safely. Never raises."""
        if progress_cb is None:
            return
        try:
            await progress_cb(stage)
        except Exception as e:
            log.warning(f"progress_cb failed (ignored): {e}")

    # ── Sanity check the docs ─────────────────────────────────────────────
    docs_text = docs_manager.raw_content
    if not docs_text:
        return Proposal(
            decision="error",
            generated_at=started_at,
            error="Docs not loaded — call docs_manager.ensure_ready() before propose().",
        )
    docs_hash = hashlib.sha256(docs_text.encode("utf-8")).hexdigest()

    # ── Fetch current Plain article state ────────────────────────────────
    plain_arts = await plain_client.get_help_center_articles(plain_articles.HELP_CENTER_ID)
    if plain_arts is None:
        return Proposal(
            decision="error",
            generated_at=started_at,
            docs_hash=docs_hash,
            error="Failed to fetch articles from Plain — see logs.",
        )
    plain_by_slug = {a["slug"]: a for a in plain_arts}
    notes.append(f"Fetched {len(plain_arts)} live article(s) from Plain.")

    # ── PASS 1: Freshness check, per article ─────────────────────────────
    log.info(f"Pass 1: judging freshness of each of {len(plain_arts)} articles...")
    total_articles = len(plain_arts)
    pass1_done = 0
    pass1_lock = asyncio.Lock()
    await _emit(f"Pass 1: judging articles (0/{total_articles})")

    # Normalize Plain's article shape into the dict shape Pass 1 expects.
    # The article-map in plain_articles.py used to be the iteration source,
    # but that's brittle — the map immediately falls out of date the moment
    # this very feature publishes a new article through Plain. Live fetch
    # is the only durable source of truth for "what articles exist".
    live_entries: list[dict] = []
    for a in plain_arts:
        grp = a.get("articleGroup") or {}
        live_entries.append({
            "slug":       a.get("slug") or "",
            "title":      a.get("title") or "",
            "plain_id":   a.get("id") or "",
            "group_id":   grp.get("id") or "",
            "group_name": grp.get("name") or "(Uncategorized)",
            "status":     a.get("status") or "",
        })

    async def _judge_with_chunks(entry):
        nonlocal pass1_done
        # Multi-query union retrieval (see _retrieve_union). The old single
        # "<title> (<group>)" query anchored on article identity and missed
        # NEW subtopics the docs grew (e.g. creator vesting added under token
        # launching never surfaced for the query "Launching a Token"). Because
        # these articles are the authoritative layer Plain's AI reads first,
        # a recall miss here silently ships a stale answer. The union widens
        # retrieval across the title, each existing section heading, and a
        # broad facet expansion so adjacent new subtopics get pulled in too.
        current_plain = plain_by_slug.get(entry["slug"])
        chunks = await _retrieve_union(
            docs_manager,
            title=entry["title"],
            group_name=entry["group_name"],
            current_html=(current_plain or {}).get("contentHtml"),
            per_query_k=docs_top_k,
        )
        result = await _judge_one_article(
            router,
            entry,
            current_plain,
            chunks,
        )
        async with pass1_lock:
            pass1_done += 1
            done_now = pass1_done
        # Emit outside the lock to avoid serializing Redis I/O
        await _emit(f"Pass 1: judging articles ({done_now}/{total_articles})")
        return entry, chunks, result

    # Bound concurrency so we don't fire N LLM calls in parallel and trip
    # rate limits on Bankr or Ollama Cloud. 3 at a time is plenty fast and
    # very polite.
    sem = asyncio.Semaphore(3)

    async def _bounded(entry):
        async with sem:
            return await _judge_with_chunks(entry)

    pass1_results = await asyncio.gather(*[_bounded(e) for e in live_entries])

    # ── Completeness audit (Pass 1.5) ────────────────────────────────────
    # For every article the LLM decided to rewrite, audit the generated body
    # against the source chunks for dropped caveats/limitations, attempt one
    # auto-revision to fold gaps back in, and keep any residual gaps to show in
    # the review UI. Best-effort and bounded; failures never block the sync.
    audit_sem = asyncio.Semaphore(3)

    async def _audit_and_maybe_revise(entry, chunks, result):
        """Returns (revised_new_article_or_None, residual_flags)."""
        if result.get("decision") != "needs_update":
            return None, []
        new_article = result.get("new_article") or {}
        html = new_article.get("contentHtml")
        if not html:
            return None, []
        async with audit_sem:
            missing = await _audit_article_completeness(
                router, article_html=str(html), docs_chunks=chunks,
            )
            if not missing:
                return None, []
            await _emit(
                f"Audit: {entry['slug']} missing {len(missing)} documented "
                f"fact(s) — revising"
            )
            revised = await _revise_article_with_missing(
                router,
                title=str(new_article.get("title") or entry["title"]),
                description=(str(new_article.get("description"))
                            if new_article.get("description") else None),
                article_html=str(html),
                missing=missing,
                docs_chunks=chunks,
            )
            if not revised:
                # Revision failed → keep original body, surface ALL gaps.
                return None, missing
            # Re-audit the revised body so the reviewer only sees what's STILL
            # missing after the fix (usually nothing).
            residual = await _audit_article_completeness(
                router, article_html=revised["contentHtml"], docs_chunks=chunks,
            )
            return revised, residual

    audit_outcomes = await asyncio.gather(*[
        _audit_and_maybe_revise(entry, chunks, result)
        for entry, chunks, result in pass1_results
    ])

    items: list[ProposalItem] = []
    for (entry, chunks, result), (revised, residual) in zip(pass1_results, audit_outcomes):
        total_in  += result["tokens_in"]
        total_out += result["tokens_out"]

        current_html = (plain_by_slug.get(entry["slug"]) or {}).get("contentHtml")
        # Best chunk's text serves as a docs_excerpt preview in the review UI
        excerpt = (chunks[0]["text"][:400] + "…") if chunks else None

        decision = result["decision"]
        new_article = result.get("new_article") or {}
        # If the audit produced a revised (more complete) article, prefer it.
        if revised:
            new_article = revised
        raw_html = new_article.get("contentHtml")

        item = ProposalItem(
            kind=decision,
            slug=entry["slug"],
            title=str(new_article.get("title") or entry["title"]),
            plain_id=entry["plain_id"],
            group_id=entry["group_id"],
            group_name=entry["group_name"],
            status=entry.get("status") or None,
            current_html=current_html,
            # Strip any leading <h1>Title</h1> the LLM may have included
            # despite the prompt — see _sanitize_proposed_html docstring.
            proposed_html=_sanitize_proposed_html(str(raw_html)) if raw_html else None,
            proposed_description=str(new_article.get("description")) if new_article.get("description") else None,
            reason=result.get("reason", ""),
            docs_excerpt=excerpt,
            audit_flags=residual or [],
        )
        items.append(item)

    # Surface audit residue in the run notes so it's visible even without
    # opening each row.
    flagged = [(it.slug, it.audit_flags) for it in items if it.audit_flags]
    if flagged:
        total_facts = sum(len(f) for _, f in flagged)
        notes.append(
            f"Completeness audit: {total_facts} documented fact(s) still missing "
            f"after auto-revision across {len(flagged)} article(s) — see per-row "
            f"audit warnings before publishing."
        )


    # ── PASS 2: Gap detection ────────────────────────────────────────────
    log.info("Pass 2: scanning docs for topics not covered by existing articles...")
    await _emit("Pass 2: scanning docs for new-topic gaps")
    sections = extract_topical_sections(docs_text)
    notes.append(
        f"Filtered docs to {len(sections)} topical H1/H2 section(s) "
        f"(from raw 1,000+ headers; dropped code-block + placeholder noise)."
    )

    # Build the enriched existing-articles view that Pass 2 needs to judge
    # coverage by *what each article actually contains*, not just by title.
    # The body_excerpt is what the article will look like after this sync:
    #   - For needs_update items: the freshly-proposed new body
    #   - For unchanged items:    the current body Plain has today
    # Either way, this is the "effective coverage" view — the state of the
    # help center as it would exist if every Pass 1 proposal were published.
    # That's the right baseline for "do we still need a new article?".
    items_by_slug = {it.slug: it for it in items}
    enriched_existing: list[dict] = []
    for entry in live_entries:
        it = items_by_slug.get(entry["slug"])
        body_html = ""
        if it:
            body_html = it.proposed_html or it.current_html or ""
        enriched_existing.append({
            "slug":         entry["slug"],
            "title":        entry["title"],
            "group_name":   entry["group_name"],
            "body_excerpt": _html_to_text_excerpt(body_html, max_chars=800),
        })

    suggestions, p2_in, p2_out = await _detect_gaps(
        router,
        docs_sections=sections,
        existing_articles=enriched_existing,
    )
    total_in  += p2_in
    total_out += p2_out

    for sug in suggestions:
        suggested_category = str(sug.get("suggested_category") or "")
        # The LLM is constrained by prompt to return one of our 9 known
        # category names. Resolve it to the actual Plain group ID so the
        # publish step lands new articles in the right category — without
        # this they'd publish uncategorized at the help-center root.
        resolved_group_id = plain_articles.group_id_by_name(suggested_category) or ""
        if suggested_category and not resolved_group_id:
            log.warning(
                f"Pass 2 returned unknown category {suggested_category!r} for "
                f"slug {sug.get('suggested_slug')!r} — article would publish uncategorized"
            )
        items.append(ProposalItem(
            kind="new_topic",
            slug=str(sug.get("suggested_slug") or ""),
            title=str(sug.get("suggested_title") or ""),
            group_id=resolved_group_id,
            group_name=suggested_category,
            reason=str(sug.get("reason") or ""),
            docs_excerpt=(
                "Docs sections: " + ", ".join(sug.get("docs_sections") or [])
                if sug.get("docs_sections") else None
            ),
        ))

    summary = {
        "unchanged":    sum(1 for it in items if it.kind == "unchanged"),
        "needs_update": sum(1 for it in items if it.kind == "needs_update"),
        "orphans":      sum(1 for it in items if it.kind == "orphan"),
        "new_topics":   sum(1 for it in items if it.kind == "new_topic"),
        "errors":       sum(1 for it in items if it.kind == "error"),
    }

    return Proposal(
        decision="ready_for_review",
        summary=summary,
        docs_hash=docs_hash,
        items=items,
        generated_at=started_at,
        tokens_in=total_in,
        tokens_out=total_out,
        notes=notes,
    )


# ─── New-topic body generation (called from the review UI) ───────────────────
#
# The propose() pipeline returns new-topic suggestions with no body — only
# title + rationale + suggested category. If an admin decides one of those
# suggestions is worth creating as a real article, they click "Generate
# body" on its card, which calls this function. It's a single LLM call
# with full semantic doc context, structured identically to Pass 1's
# "write the new article" path so the output matches house voice exactly.

def _generate_body_system_prompt() -> str:
    return f"""You are writing a brand-new Bankr help center article from scratch.

You will receive:
  - The proposed article title (a real user question).
  - The suggested category.
  - Relevant docs chunks retrieved from docs.bankr.bot.

Write a complete article body that answers the question, grounded entirely
in the docs provided. If something isn't covered by the docs, omit it —
don't invent.

{_HOUSE_VOICE}

Return ONLY a JSON object (no preamble, no markdown fence) shaped exactly:
{{
  "title": "(echo the title back, possibly polished — but keep it as a real
            user question, not a heading)",
  "description": "(one-sentence summary, ≤140 chars, for SEO + previews)",
  "contentHtml": "(the body, in semantic HTML per the rules above)"
}}"""


async def generate_new_topic_body(
    *,
    docs_manager: SemanticDocsManager,
    title: str,
    category: str,
    router: Optional[LLMRouter] = None,
    docs_top_k: int = 8,
) -> dict:
    """
    Generate a complete article body for one new-topic suggestion.

    Returns:
      {
        "ok": bool,
        "title": str,           # the LLM's polished title, or echo
        "description": str,
        "content_html": str,    # sanitized (no leading h1)
        "tokens_in": int,
        "tokens_out": int,
        "error": str | None,
      }

    Never raises — failures are surfaced via ok=False + error.
    """
    router = router or LLMRouter()
    query = f"{title} ({category})" if category else title
    try:
        chunks = await docs_manager.query_chunks(query, top_k=docs_top_k)
    except Exception as e:
        return {"ok": False, "title": title, "description": "", "content_html": "",
                "tokens_in": 0, "tokens_out": 0,
                "error": f"docs query failed: {e}"}

    payload = {
        "title":    title,
        "category": category,
        "relevant_docs_chunks": [
            {"chunk_index": c.get("index"), "text": c["text"]} for c in chunks
        ],
    }
    try:
        resp = await router.chat(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system=_generate_body_system_prompt(),
            temperature=0.2,
        )
    except Exception as e:
        return {"ok": False, "title": title, "description": "", "content_html": "",
                "tokens_in": 0, "tokens_out": 0,
                "error": f"LLM call failed: {e}"}

    tin  = getattr(resp, "tokens_in", 0) or 0
    tout = getattr(resp, "tokens_out", 0) or 0
    if not getattr(resp, "ok", True):
        return {"ok": False, "title": title, "description": "", "content_html": "",
                "tokens_in": tin, "tokens_out": tout,
                "error": "LLM returned not-ok"}

    parsed = _extract_json_object(str(resp))
    if not parsed or "contentHtml" not in parsed:
        log.error(f"generate_new_topic_body: unparseable LLM output: {str(resp)[:200]!r}")
        return {"ok": False, "title": title, "description": "", "content_html": "",
                "tokens_in": tin, "tokens_out": tout,
                "error": "LLM returned unparseable output"}

    polished_title = (parsed.get("title") or title).strip()
    description    = (parsed.get("description") or "").strip()
    content_html   = _sanitize_proposed_html(parsed.get("contentHtml") or "")

    return {
        "ok": True,
        "title": polished_title,
        "description": description,
        "content_html": content_html,
        "tokens_in": tin,
        "tokens_out": tout,
        "error": None,
    }


# ─── Publish: push selected items to Plain ───────────────────────────────────
#
# Called from the review page when the admin clicks "Publish N articles."
# Each item is one upsert. We continue on per-item error, and the caller
# (admin_routes) is responsible for saving the structured result via
# redis_pubsub.save_publish_result so the results page can render it.
#
# PublishItem is the shape the caller assembles after composing edits onto
# the proposal — we don't do that composition here, because the same
# function will be used in tests with handcrafted inputs.

@dataclass
class PublishItem:
    """One row to push to Plain. Caller composes edits onto proposal first."""
    slug:          str
    title:         str
    content_html:  str
    is_new:        bool                  # True → create, False → update existing
    plain_id:      Optional[str] = None  # required when is_new=False
    description:   Optional[str] = None
    group_id:      Optional[str] = None
    status:        Optional[str] = None  # existing status to preserve on update
                                         # (Plain requires status on EVERY upsert,
                                         # despite introspecting as optional)


async def publish_items(
    *,
    plain_client: PlainClient,
    items: list[PublishItem],
) -> list[dict]:
    """
    Upsert each PublishItem into Plain.

    Returns a list of per-item result dicts:
      {"slug":..., "ok": bool, "plain_id_after":..., "error": str|None,
       "is_new": bool}

    Continues on per-item errors — one failure doesn't abort the rest.
    """
    out: list[dict] = []
    for it in items:
        # Validate locally before hitting Plain. Catching obvious errors
        # here makes the failure modes cleaner on the results page.
        if not it.is_new and not it.plain_id:
            out.append({"slug": it.slug, "ok": False,
                        "plain_id_after": None, "is_new": False,
                        "error": "Update requested but no plain_id provided."})
            continue
        if not it.title or not it.content_html:
            out.append({"slug": it.slug, "ok": False,
                        "plain_id_after": it.plain_id, "is_new": it.is_new,
                        "error": "Empty title or content_html — refusing to publish."})
            continue

        try:
            article = await plain_client.upsert_help_center_article(
                help_center_id   = plain_articles.HELP_CENTER_ID,
                title            = it.title,
                content_html     = it.content_html,
                article_id       = it.plain_id if not it.is_new else None,
                article_group_id = it.group_id,
                description      = it.description,
                slug             = it.slug if it.is_new else None,
                # Plain requires `status` on EVERY upsert, including updates
                # (it introspects as optional but the runtime validator rejects
                # its absence with input_validation/REQUIRED). New articles go
                # in as DRAFT so a human reviews the live preview before
                # publishing. Updates echo back the article's CURRENT status so
                # editing a live (PUBLISHED) article doesn't silently unpublish
                # it; the `or "DRAFT"` is a safety net if status didn't come
                # through — fail safe toward draft, never accidental publish.
                status           = "DRAFT" if it.is_new else (it.status or "DRAFT"),
            )
            # PlainClient.upsert_help_center_article returns the article dict
            # directly ({id, slug, title, status}) on success, None on any
            # error (the client logs GraphQL errors verbatim before returning).
            if article and article.get("id"):
                out.append({
                    "slug": it.slug, "ok": True, "is_new": it.is_new,
                    "plain_id_after": article["id"],
                    "error": None,
                })
            else:
                # plain_client returned None → it already logged the cause.
                # The api logs are the source of truth here.
                out.append({
                    "slug": it.slug, "ok": False, "is_new": it.is_new,
                    "plain_id_after": it.plain_id,
                    "error": "Plain upsert returned no article — see api logs.",
                })
        except Exception as e:
            log.exception(f"publish_items failed for {it.slug}")
            out.append({
                "slug": it.slug, "ok": False, "is_new": it.is_new,
                "plain_id_after": it.plain_id,
                "error": f"Exception: {type(e).__name__}: {e}",
            })

    return out
