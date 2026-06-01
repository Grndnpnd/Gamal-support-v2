"""
plain_articles.py
-----------------
Hard-coded map of the 22 customer-facing help center articles at help.bankr.bot.

Each article entry carries:
  - slug                 — URL slug (the identity Plain uses)
  - title                — customer-facing title, phrased as a real user
                           question ("How Do I Place a Trade?" not "Trading
                           Documentation"). See WRITING GUIDELINES below.
  - plain_id             — Plain article ID (hca_...). Lets the propose
                           pipeline target an existing article for update
                           without a slug→ID lookup round-trip.
  - group_id             — Plain article-group ID (hcag_...). Required when
                           creating a brand-new article.
  - group_name           — human label for the group, used in the proposal UI.
  - docs_sections        — list of strings to match against docs.bankr.bot
                           section headers, so the propose pipeline knows
                           which slice(s) of the docs feed this article. Match
                           is case-insensitive substring against header text.

The IDs in this file were captured live from Plain via the read-side test
(test_plain_articles_read.py) on 2026-06-01. If Plain article IDs ever change
(unlikely — they're immutable once created), re-run that test and update here.

Adding / removing articles is rare and requires a code change here +
a deploy. That tradeoff is acceptable: 22 articles maintained by a small
team isn't worth a config UI for.

WRITING GUIDELINES (used by the propose LLM prompt — single source of truth):
  - Plain language, not developer docs. "Smart friend explaining" not "API
    reference for developers."
  - Title is a real user question. "How Do I Place a Trade?" / "What Are
    DCA and TWAP Orders?" not "Trading Documentation."
  - Practical numbered steps the user can follow.
  - Example prompts users can copy/paste into Bankr.
  - Link to docs.bankr.bot at the end for technical deep dives.
  - Common troubleshooting tips where relevant.
  - Code examples minimal — only when truly necessary.
  - Tone: friendly, direct, confident — knowledgeable friend, not corporate.
"""

# Help center the articles live in (the public Bankr help center).
# Hard-coded here so callers don't need to pass it through.
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
# Plain IDs (hca_...) confirmed live. 16 articles have their real Plain IDs
# baked in below. 6 articles still carry "hca_NEEDS_LOOKUP" because they
# don't yet exist in Plain (the bootstrap couldn't resolve them on the first
# real propose run): openclaw, bankr-skills, account-security, scams-and-
# safety, login-issues, transaction-issues. The propose pipeline correctly
# flags these as needing creation rather than update; the publish step
# (stage 3) calls upsertHelpCenterArticle with no helpCenterArticleId so
# Plain creates them fresh, and a follow-up commit can backfill the real
# IDs here from the first successful publish.

ARTICLES = [
    # ── Getting Started (4) ──────────────────────────────────────────────────
    _entry("what-is-bankr",
           "What Is Bankr?",
           "hca_01KRY66XFGQK2VXDDYG2BZZ2X5",
           "getting_started",
           ["introduction", "what is bankr", "overview"]),
    _entry("quick-start",
           "How Do I Get Started with Bankr?",
           "hca_01KRY67H3KQ2J3RE8ZAP7B6SWT",
           "getting_started",
           ["quick start", "getting started", "first steps"]),
    _entry("supported-chains",
           "What Blockchains Does Bankr Support?",
           "hca_01KRY688A4ZT6KT38DX9RCJXW3",
           "getting_started",
           ["supported chains", "blockchains", "networks", "chains"]),
    _entry("memory-and-storage",
           "How Does Bankr Remember My Preferences?",
           "hca_01KRY6DARP2VFQSB245F7EPEHR",
           "getting_started",
           ["memory", "preferences", "storage", "remembers"]),

    # ── Trading & Orders (5) ─────────────────────────────────────────────────
    _entry("how-to-trade",
           "How Do I Place a Trade?",
           "hca_01KRY68Q51SDZ01WJWD2M0Y9P9",
           "trading_orders",
           ["trading", "swaps", "place a trade", "how to trade"]),
    _entry("limit-and-stop-orders",
           "How Do Limit and Stop Orders Work?",
           "hca_01KRY694K4BJ8PDXWSTK01WP4G",
           "trading_orders",
           ["limit orders", "stop orders", "stop loss", "take profit"]),
    _entry("dca-and-twap",
           "What Are DCA and TWAP Orders?",
           "hca_01KRY69HFBJ5M1WRDN4WJF16Z7",
           "trading_orders",
           ["dca", "dollar cost averaging", "twap", "time-weighted"]),
    _entry("leveraged-trading",
           "How Does Leveraged Trading Work?",
           "hca_01KRY69ZFKJQMHFF1HMF583WK5",
           "trading_orders",
           ["leverage", "leveraged trading", "perps", "perpetuals", "hyperliquid"]),
    _entry("polymarket",
           "How Do I Use Polymarket with Bankr?",
           "hca_01KRY6AA21BDWF5H7GFV3G9W3J",
           "trading_orders",
           ["polymarket", "prediction markets"]),

    # ── Wallet & Portfolio (3) ───────────────────────────────────────────────
    _entry("wallet-and-balances",
           "How Do I Check My Wallet and Balances?",
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
           "How Do I Launch a Token?",
           "hca_01KRY6BDHX8H4J3Z71AWPFHV09",
           "token_launching",
           ["token launch", "launching a token", "deploy token", "create token"]),

    # ── Automations (1) ──────────────────────────────────────────────────────
    _entry("automations",
           "How Do Automations Work?",
           "hca_01KRY6BY4SBJ6V66Y4A0M1ZT8H",
           "automations",
           ["automations", "automation", "scheduled", "recurring"]),

    # ── Bankr Club & Billing (2) ─────────────────────────────────────────────
    _entry("bankr-club-and-max-mode",
           "What Is Bankr Club and Max Mode?",
           "hca_01KRY6CQQV45YESM63ZWH65APM",
           "bankr_club",
           ["bankr club", "max mode", "club membership", "premium"]),
    _entry("llm-gateway",
           "What Is the LLM Gateway?",
           "hca_01KRY6GVRAW4DHHBSM0N1YHKQN",
           "bankr_club",
           ["llm gateway", "llm", "ai gateway", "api"]),

    # ── Apps & Extensions (2) ────────────────────────────────────────────────
    _entry("openclaw",
           "What Is OpenClaw?",
           "hca_NEEDS_LOOKUP",
           "apps_extensions",
           ["openclaw", "open claw"]),
    _entry("bankr-skills",
           "How Do I Use Bankr Skills?",
           "hca_NEEDS_LOOKUP",
           "apps_extensions",
           ["bankr skill", "skills", "skill package"]),

    # ── Security (2) ─────────────────────────────────────────────────────────
    _entry("account-security",
           "How Do I Keep My Bankr Account Secure?",
           "hca_NEEDS_LOOKUP",
           "security",
           ["security", "account security", "2fa", "two factor"]),
    _entry("scams-and-safety",
           "How Do I Avoid Scams and Stay Safe?",
           "hca_NEEDS_LOOKUP",
           "security",
           ["scams", "safety", "phishing", "fraud"]),

    # ── Troubleshooting (2) ──────────────────────────────────────────────────
    _entry("login-issues",
           "Why Can't I Sign In to Bankr?",
           "hca_NEEDS_LOOKUP",
           "troubleshooting",
           ["login", "sign in", "401", "can't access", "locked out"]),
    _entry("transaction-issues",
           "Why Did My Transaction Fail?",
           "hca_NEEDS_LOOKUP",
           "troubleshooting",
           ["transaction failed", "tx failed", "swap failed", "errors"]),
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
