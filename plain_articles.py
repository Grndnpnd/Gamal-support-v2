"""
plain_articles.py
-----------------
Plain help-center configuration and category lookup.

WHAT THIS FILE OWNS (still active):
  - HELP_CENTER_ID         — the public Bankr help center
  - GROUPS / _GROUP_NAMES  — category ID ↔ name mappings (Plain has no
                             discovery query for these, so we hardcode)
  - group_id_by_name()     — Pass 2 LLM returns a category name string;
                             the publish step needs the hcag_... ID

WHAT THIS FILE NO LONGER DRIVES (kept for now but unused by the pipeline):
  - ARTICLES list and the iter_articles / get_by_slug / get_by_plain_id /
    slug_to_plain_id_map / slugs_missing_plain_id / resolve_missing_ids
    helpers.

  The sync pipeline used to iterate over ARTICLES to decide which articles
  to judge. That broke whenever a new article was published through this
  very feature — the static list immediately fell out of date and the
  newly-published article wouldn't appear in the next proposal.

  As of 2026-06-01, article_sync.propose() iterates over the live Plain
  fetch (plain_client.get_help_center_articles) and treats that as the
  single source of truth for "what articles exist". The ARTICLES list
  below is now dead code, left in place to avoid bundling code-removal
  with the bug fix. A future commit can delete it cleanly.
"""

# Help center the articles live in (the public Bankr help center).
# Hard-coded here so callers don't need to pass it through.
from typing import Optional

HELP_CENTER_ID = "hc_01KNJE5VXXTKN1A96NE0KFRNRK"

# Article-group IDs (Plain's "categories"). Confirmed live via Plain GraphQL.
GROUPS = {
    "getting_started":   "hcag_01KRY61SB829A7RZPFPCWNXZN2",
    "trading_orders":    "hcag_01KRY626XK9F6XD4S2WMZHH8HJ",
    "wallet_portfolio":  "hcag_01KRY62G9QSG05MPEFMH3EEKJ0",
    "token_launching":   "hcag_01KRY62R7GY55R0QJZZB81MJS7",
    "automations":       "hcag_01KRY631K490Q2WXFQHBJC4NE2",
    "bankr_club":        "hcag_01KRY64BTWK9DH6SB4XTH5JAB0",
    "apps_extensions":   "hcag_01KRY64T51S3VPJHZPQPF9ZG30",
    "security":          "hcag_01KRY65137AA4TKF84R5FTXEAR",
    "troubleshooting":   "hcag_01KRY659EPMRWYSW9023NAW5CJ",
}
_GROUP_NAMES = {
    "getting_started":   "Getting Started",
    "trading_orders":    "Trading & Orders",
    "wallet_portfolio":  "Wallet & Portfolio",
    "token_launching":   "Token Launching",
    "automations":       "Automations",
    "bankr_club":        "Bankr Club & Billing",
    "apps_extensions":   "Apps & Extensions",
    "security":          "Security",
    "troubleshooting":   "Troubleshooting",
}


def _entry(slug, title, plain_id, group_key, docs_sections):
    """Internal constructor — keeps the table below readable."""
    return {
        "slug":          slug,
        "title":         title,
        "plain_id":      plain_id,
        "group_id":      GROUPS[group_key],
        "group_name":    _GROUP_NAMES[group_key],
        "docs_sections": docs_sections,
    }


# ── The 22 articles ──────────────────────────────────────────────────────────
# Source of truth: live Plain help-center fetch via
#   PlainClient.get_help_center_articles(HELP_CENTER_ID).
# This map was reconciled against Plain on 2026-06-01 — every article below
# corresponds to a real article that currently exists in Plain. No placeholders.
#
# Titles match Plain exactly (not the user-question-style rewrites we used
# earlier). Rationale: title changes affect URL/SEO/customer mental models and
# should be deliberate per-article decisions made in Plain's UI, not side
# effects of a body-sync run. The propose pipeline judges body content only.
#
# If articles are added/removed in Plain, re-run the dump script and update
# this list:
#     railway run python dump.py
# (Or the diagnostic at scripts/test_plain_articles_read.py — same data.)

ARTICLES = [
    # ── Getting Started (4) ──────────────────────────────────────────────────
    _entry("what-is-bankr",
           "What is Bankr?",
           "hca_01KRY66XFGQK2VXDDYG2BZZ2X5",
           "getting_started",
           ["introduction", "what is bankr", "overview"]),
    _entry("quick-start",
           "Quick Start Guide",
           "hca_01KRY67H3KQ2J3RE8ZAP7B6SWT",
           "getting_started",
           ["quick start", "getting started", "first steps"]),
    _entry("supported-chains",
           "Supported Blockchains",
           "hca_01KRY688A4ZT6KT38DX9RCJXW3",
           "getting_started",
           ["supported chains", "blockchains", "networks", "chains"]),
    _entry("memory-and-storage",
           "Memory & File Storage",
           "hca_01KRY6DARP2VFQSB245F7EPEHR",
           "getting_started",
           ["memory", "preferences", "storage", "file storage", "remembers"]),

    # ── Trading & Orders (5) ─────────────────────────────────────────────────
    _entry("how-to-trade",
           "How to Trade (Swaps)",
           "hca_01KRY68Q51SDZ01WJWD2M0Y9P9",
           "trading_orders",
           ["trading", "swaps", "place a trade", "how to trade"]),
    _entry("limit-and-stop-orders",
           "Limit Orders & Stop Orders",
           "hca_01KRY694K4BJ8PDXWSTK01WP4G",
           "trading_orders",
           ["limit orders", "stop orders", "stop loss", "take profit"]),
    _entry("dca-and-twap",
           "DCA & TWAP Orders",
           "hca_01KRY69HFBJ5M1WRDN4WJF16Z7",
           "trading_orders",
           ["dca", "dollar cost averaging", "twap", "time-weighted"]),
    _entry("leveraged-trading",
           "Leveraged Trading",
           "hca_01KRY69ZFKJQMHFF1HMF583WK5",
           "trading_orders",
           ["leverage", "leveraged trading", "perps", "perpetuals", "hyperliquid"]),
    _entry("polymarket",
           "Prediction Markets (Polymarket)",
           "hca_01KRY6AA21BDWF5H7GFV3G9W3J",
           "trading_orders",
           ["polymarket", "prediction markets"]),

    # ── Wallet & Portfolio (3) ───────────────────────────────────────────────
    _entry("wallet-and-balances",
           "Your Wallet & Balances",
           "hca_01KRY6AVJDGSR4SBE40HKGM3FR",
           "wallet_portfolio",
           ["wallet", "balances", "portfolio overview"]),
    _entry("transfers",
           "How Do I Send Crypto to Someone?",
           "hca_01KRYJFR4PPDHSZDDXR06AWY7P",
           "wallet_portfolio",
           ["transfer", "send crypto", "send funds"]),
    _entry("nfts",
           "How Do I Buy, Sell, or Mint NFTs?",
           "hca_01KRYJGAX2K2XYB5RR3CSAR6YP",
           "wallet_portfolio",
           ["nft", "nfts", "mint", "buy nft", "sell nft"]),

    # ── Token Launching (1) ──────────────────────────────────────────────────
    _entry("launching-a-token",
           "Launching a Token",
           "hca_01KRY6BDHX8H4J3Z71AWPFHV09",
           "token_launching",
           ["token launch", "launching a token", "deploy token", "create token"]),

    # ── Automations (1) ──────────────────────────────────────────────────────
    _entry("automations",
           "Setting Up Automations",
           "hca_01KRY6BY4SBJ6V66Y4A0M1ZT8H",
           "automations",
           ["automations", "automation", "scheduled", "recurring"]),

    # ── Bankr Club & Billing (2) ─────────────────────────────────────────────
    _entry("bankr-club-and-max-mode",
           "Bankr Club & Max Mode",
           "hca_01KRY6CQQV45YESM63ZWH65APM",
           "bankr_club",
           ["bankr club", "max mode", "club membership", "premium"]),
    _entry("llm-gateway",
           "LLM Gateway & Credits",
           "hca_01KRY6GVRAW4DHHBSM0N1YHKQN",
           "bankr_club",
           ["llm gateway", "llm", "ai gateway", "credits", "api"]),

    # ── Apps & Extensions (4) ────────────────────────────────────────────────
    _entry("building-apps",
           "Building Apps",
           "hca_01KRY6DWJ30WD7HDRME35F1KPS",
           "apps_extensions",
           ["building apps", "apps feature", "build app"]),
    _entry("skills-and-extensions",
           "Skills, MCP Servers & Env Vars",
           "hca_01KRY6EEJB90C8PA03MYPQAW27",
           "apps_extensions",
           ["bankr skill", "skills", "skill package", "mcp", "mcp server", "env vars", "environment variables"]),
    _entry("browser-automation",
           "Browser Automation",
           "hca_01KRY6EZJTWXTGFMSYRV0606YF",
           "apps_extensions",
           ["browser automation", "browser", "web automation"]),
    _entry("claude-plugins",
           "Using Bankr from Claude Code",
           "hca_01KRY6HDAEVEYJHS2E461VMPBS",
           "apps_extensions",
           ["claude code", "claude plugins", "using bankr from claude", "vscode"]),

    # ── Security (1) ─────────────────────────────────────────────────────────
    _entry("security",
           "Protecting Your Account",
           "hca_01KRY6FJQREDNTAB8HDZDBAYRA",
           "security",
           ["security", "account security", "protecting", "2fa", "two factor", "scams", "safety", "phishing"]),

    # ── Troubleshooting (1) ──────────────────────────────────────────────────
    _entry("troubleshooting",
           "Common Issues & Fixes",
           "hca_01KRY6G9PM5JQV91MFVJR2SEE3",
           "troubleshooting",
           ["troubleshooting", "common issues", "fixes", "login", "sign in", "transaction failed", "errors"]),
]


# ── Lookups ──────────────────────────────────────────────────────────────────

def get_by_slug(slug: str) -> dict | None:
    """Return the article entry for a given slug, or None."""
    for a in ARTICLES:
        if a["slug"] == slug:
            return a
    return None


def get_by_plain_id(plain_id: str) -> dict | None:
    """Return the article entry for a given Plain ID, or None."""
    for a in ARTICLES:
        if a["plain_id"] == plain_id:
            return a
    return None


def iter_articles():
    """Iterate the article entries in declaration order."""
    return iter(ARTICLES)


def group_id_by_name(category_name: str) -> Optional[str]:
    """
    Map a human-readable category name (e.g. 'Apps & Extensions') back to
    its Plain article-group ID (hcag_...).

    Used when the propose pipeline's Pass 2 suggests new articles — the
    LLM returns a category name string, and we need the ID to send to
    Plain's upsert mutation. Returns None if the name doesn't match any
    known group, which lets the caller fall back to "uncategorized"
    (drafted at the root of the help center) rather than crashing.

    Match is case-sensitive against _GROUP_NAMES values. The Pass 2 prompt
    constrains the LLM to those exact strings, so a miss here usually
    indicates the LLM ignored the schema — log it.
    """
    if not category_name:
        return None
    for key, name in _GROUP_NAMES.items():
        if name == category_name:
            return GROUPS[key]
    return None


def slug_to_plain_id_map() -> dict[str, str]:
    """Return { slug: plain_id } for all articles with a real (non-placeholder) ID."""
    return {
        a["slug"]: a["plain_id"]
        for a in ARTICLES
        if a["plain_id"] != "hca_NEEDS_LOOKUP"
    }


def slugs_missing_plain_id() -> list[str]:
    """Slugs whose Plain ID hasn't been backfilled yet (placeholder values)."""
    return [a["slug"] for a in ARTICLES if a["plain_id"] == "hca_NEEDS_LOOKUP"]


# ── Bootstrap helper ─────────────────────────────────────────────────────────

def resolve_missing_ids_from_live(plain_articles: list[dict]) -> dict[str, str]:
    """
    Given the list of articles fetched live from Plain (the output of
    PlainClient.get_help_center_articles), return a map of
    {slug: plain_id} for any articles that this module still has as
    'hca_NEEDS_LOOKUP'. The first time the propose pipeline runs, the caller
    uses this to backfill — and then prints a note so the developer can
    update this file's hard-coded IDs.
    """
    placeholder_slugs = set(slugs_missing_plain_id())
    if not placeholder_slugs:
        return {}
    resolved: dict[str, str] = {}
    for a in plain_articles:
        s = a.get("slug")
        if s in placeholder_slugs:
            resolved[s] = a["id"]
    return resolved
