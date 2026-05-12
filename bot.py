"""
Bankr Support Bot
-----------------
- Monitors messages for support intent using keyword/pattern matching
- Proactively reaches out to users who seem to need help
- Responds when directly @mentioned
- Maintains per-user conversation history
- Uses semantic search (ChromaDB + MiniLM) to inject only relevant doc chunks
- Escalates unresolvable issues to Plain by creating a support thread
- Opens a private Discord thread for the ticket so the user can reply without
  leaving Discord; agent replies are relayed back via webhook_server.py
"""

import discord
import asyncio
import aiohttp
import random
import re
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv
import os

from plain_client import PlainClient
from shared import SemanticDocsManager, OllamaClient
from redis_map import set_thread_link, is_using_redis

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
BANKR_LLM_KEY    = os.getenv("BANKR_LLM_KEY", "")
BANKR_LLM_MODEL  = os.getenv("BANKR_LLM_MODEL", "gemini-3-flash")

DOCS_URL              = os.getenv("DOCS_URL", "https://docs.bankr.bot/llms-full.txt")
DOCS_REFRESH_HOURS    = int(os.getenv("DOCS_REFRESH_HOURS", "12"))

MONITORED_CHANNEL_IDS = [int(x) for x in os.getenv("MONITORED_CHANNEL_IDS", "").split(",") if x.strip()]

CONVERSATION_TTL_MINUTES = int(os.getenv("CONVERSATION_TTL_MINUTES", "30"))
REFLAG_COOLDOWN_MINUTES  = int(os.getenv("REFLAG_COOLDOWN_MINUTES", "15"))

CHUNK_SIZE          = int(os.getenv("CHUNK_SIZE", "600"))
CHUNK_OVERLAP       = int(os.getenv("CHUNK_OVERLAP", "80"))
TOP_K_CHUNKS        = int(os.getenv("TOP_K_CHUNKS", "6"))
MAX_RETRIEVED_CHARS = int(os.getenv("MAX_RETRIEVED_CHARS", "8000"))

# Plain integration
PLAIN_API_KEY          = os.getenv("PLAIN_API_KEY", "")
PLAIN_LABEL_TYPE_ID    = os.getenv("PLAIN_LABEL_TYPE_ID", "")
# Internal URL of webhook_server — used to cancel Plain-triggered deletions via !keep
# On Railway set this to the internal private URL of the webhook service
# e.g. http://webhook.railway.internal:8080  or leave blank to skip cross-process cancel
WEBHOOK_SERVER_URL     = os.getenv("WEBHOOK_SERVER_URL", "")

# ─── Thread Map Persistence ───────────────────────────────────────────────────
# Uses Redis when REDIS_URL is set (production/Railway), in-memory dict otherwise (local dev).
# See redis_map.py for full details.

async def register_thread_link(plain_thread_id: str, discord_thread_id: int):
    """Persist plain_thread_id → discord_thread_id so the webhook server can route replies."""
    await set_thread_link(plain_thread_id, discord_thread_id)


# ─── Intent Detection ─────────────────────────────────────────────────────────

SUPPORT_PATTERNS = [
    r"\bhow (do|can|to|does|would|should|come)\b",
    r"\bwhy (is|does|isn'?t|doesn'?t|won'?t|can'?t|would|did|didn'?t)\b",
    r"\bwhat (is|are|does|the|if|about|happens?|should)\b",
    r"\bwhere (do|can|is|are|should|would)\b",
    r"\bwhen (do|can|will|does|should|would)\b",
    r"\bcan (i|you|we|someone|it)\b",
    r"\b(not working|doesn'?t work|won'?t work|broke|broken|failed|failing|never works?)\b",
    r"\b(errors?|issues?|bugs?|problems?|glitch(?:es)?|crash(?:es)?)\b",
    r"\b(help|stuck|confused|unsure|unclear|lost|struggling)\b",
    r"\b(can'?t|cannot|couldn'?t|won'?t|doesn'?t|didn'?t|isn'?t|wasn'?t)\b",
    r"\b(trying to|tried to|attempting to|keeps? (failing|erroring|breaking))\b",
    r"\b(no idea|don'?t understand|don'?t know|not sure|idk|no clue)\b",
    r"\b(swaps?|swapping|traded?|trades?|trading|bankr|bnkr|bot|perps?|spot|forex)\b",
    r"\b(tokens?|launches?|launching|deploys?|deploying|deployed)\b",
    r"\b(wallets?|balances?|fees?|claiming|claims?|skills?)\b",
    r"\b(openclaw|api keys?|llm gateway|llm|agents?|hyperliquid)\b",
    r"\b(solana|base|ethereum|polygon|unichain|arbitrum|world chain)\b",
    r"\b(ugh|argh|wtf|wth|omg|frustrat\w*|annoying|annoyed|pissed|fuck this|scammers)\b",
    r"[?]{2,}",
    r"\b(wrong with|is wrong|went wrong|going wrong)\b",
    # Simplified Chinese
    r"[怎如何为什么什么哪][么样能会][办做用是去]?",
    r"[能可]以?[吗嘛]",
    r"[不没][能行知道会][用]?",
    r"[帮请].*[我忙助]",
    r"[错误问题故障][了吗]?",
    r"[失败无法不行][了吗]?",
    r"[交换兑换]",
    r"[钱包余额费用]",
    r"[代币发行部署]",
    r"[链上交易买卖]",
    r"[帮助支持问题]",
    # Korean
    r"어떻게|왜|무엇|뭐|어디|언제|어떤",
    r"할 수 있나요?|할 수 없|안 되|안되",
    r"모르겠|모르|헷갈|이해가 안",
    r"도와|도움|help",
    r"오류|에러|문제|버그|고장|실패",
    r"안 됩니다|작동이 안|작동 안|실행이 안",
    r"스왑|교환|거래|매수|매도",
    r"지갑|잔액|수수료|클레임",
    r"토큰|발행|배포|런치",
    r"체인|솔라나|이더리움|폴리곤|베이스",
    # ── Bankr feature nouns — catches short direct questions that score low otherwise
    # e.g. "how do I see the leaderboard" has no negation/confusion word, just a feature name
    r"\b(leaderboard|leader board)\b",
    r"\b(staking|unstaking|stake|unstake|vault|v2 staking|staking v2)\b",
    r"\b(bankr club|club membership|club access|club chat)\b",
    r"\b(max mode|max-mode)\b",
    r"\b(beta (feature|access|flag)|beta features?)\b",
    r"\b(app tab|apps? tab|terminal sidebar|bankr terminal|bankr app)\b",
    r"\b(dca|twap|limit order|stop order|stop.?loss)\b",
    r"\b(fee sharing|fee share|trading fees?)\b",
    r"\b(bridge|bridging|cross.?chain)\b",
    r"\b(airdrop|air drop)\b",
    r"\b(nft|mint|minting)\b",
    r"\b(portfolio|p&l|pnl|profit.?loss)\b",
    r"\b(opencode|open claw|open code)\b",
    r"\b(bankr skill|skill package|install skill)\b",
    r"\b(alpha chat|alpha access)\b",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in SUPPORT_PATTERNS]

MIN_MESSAGE_LENGTH = 10
INTENT_THRESHOLD   = 2

# Ratio of non-alphanumeric characters above this = skip intent detection entirely.
# Catches Morse code, encoded strings, symbol spam — never genuine support requests.
SYMBOL_RATIO_THRESHOLD = 0.40

DISENGAGE_COMMANDS = {"!done", "!close", "!stop", "!bye", "!thanks", "!thank you"}

DISENGAGE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b(thanks?|thank you|cheers|got it|perfect|solved|worked|fixed|all good|sorted)\b",
        r"\b(bye|goodbye|cya|see ya|later)\b",
        r"\bthat('?s| is) (all|it|enough|perfect|great|helpful)\b",
        r"\bno (more )?questions?\b",
        r"\bi'?m good( now)?\b",
        r"\b(nevermind|never mind|nvm|nm)\b",
        r"\b(works? now|working now|figured it out|got it working)\b",
        r"谢谢|感谢|没问题|解决了|好的|明白了|懂了|再见",
        r"감사합니다|감사해요|고마워|해결됐|됐어요|알겠습니다|알겠어요|괜찮아|안녕",
    ]
]


def detect_support_intent(message: str) -> tuple[bool, int]:
    if len(message) < MIN_MESSAGE_LENGTH:
        return False, 0

    # Skip messages that are mostly symbols/encoded content (Morse code, spam, etc.)
    stripped = message.replace(" ", "")
    if stripped:
        non_alnum = sum(1 for c in stripped if not c.isalnum())
        symbol_ratio = non_alnum / len(stripped)
        if symbol_ratio > SYMBOL_RATIO_THRESHOLD:
            log.debug(f"Skipping symbol-heavy message (ratio={symbol_ratio:.2f}): {message[:60]}")
            return False, 0

    score = sum(1 for p in COMPILED_PATTERNS if p.search(message))
    return score >= INTENT_THRESHOLD, score


# ─── Conversation Manager ─────────────────────────────────────────────────────

class ConversationManager:
    def __init__(self):
        self.conversations: dict = defaultdict(
            lambda: {"history": [], "last_active": datetime.now(timezone.utc)}
        )

    def _key(self, channel_id: int, user_id: int) -> tuple:
        return (channel_id, user_id)

    def add_message(self, channel_id: int, user_id: int, role: str, content: str):
        key = self._key(channel_id, user_id)
        self.conversations[key]["history"].append({"role": role, "content": content})
        self.conversations[key]["last_active"] = datetime.now(timezone.utc)
        if len(self.conversations[key]["history"]) > 20:
            self.conversations[key]["history"] = self.conversations[key]["history"][-20:]

    def get_history(self, channel_id: int, user_id: int) -> list:
        return self.conversations[self._key(channel_id, user_id)]["history"]

    def clear(self, channel_id: int, user_id: int):
        key = self._key(channel_id, user_id)
        if key in self.conversations:
            del self.conversations[key]

    def has_active_conversation(self, channel_id: int, user_id: int) -> bool:
        key = self._key(channel_id, user_id)
        if key not in self.conversations or not self.conversations[key]["history"]:
            return False
        return datetime.now(timezone.utc) - self.conversations[key]["last_active"] < timedelta(
            minutes=CONVERSATION_TTL_MINUTES
        )

    def cleanup_expired(self):
        now = datetime.now(timezone.utc)
        expired = [
            k for k, v in self.conversations.items()
            if now - v["last_active"] > timedelta(minutes=CONVERSATION_TTL_MINUTES)
        ]
        for k in expired:
            del self.conversations[k]
        if expired:
            log.info(f"Cleaned up {len(expired)} expired conversations")


# ─── Plain Ticket Manager ─────────────────────────────────────────────────────

class PlainTicketManager:
    """
    Handles the full lifecycle of a Plain support ticket opened from Discord:
      1. Upsert customer in Plain
      2. Create the Plain thread
      3. Create a private Discord thread for the user
      4. Persist the Plain ↔ Discord thread link
      5. Forward subsequent user messages from Discord → Plain
    
    Also tracks which Discord threads are active tickets so that messages
    sent there are forwarded to Plain rather than handled by the bot's
    normal conversation flow.
    """

    def __init__(self, plain: PlainClient):
        self.plain = plain
        # discord_thread_id → plain_thread_id
        self._active_tickets: dict[int, str] = {}
        # discord_user_id → discord_thread_id (so we know if a user already has an open ticket)
        self._user_tickets: dict[int, int] = {}

    def is_ticket_thread(self, channel_id: int) -> bool:
        return channel_id in self._active_tickets

    def get_plain_thread_id(self, discord_thread_id: int) -> str | None:
        return self._active_tickets.get(discord_thread_id)

    def user_has_open_ticket(self, user_id: int) -> int | None:
        """Returns the Discord thread ID of the user's open ticket, or None."""
        return self._user_tickets.get(user_id)

    async def open_ticket(
        self,
        message: discord.Message,
        issue_summary: str,
        full_context: str,
    ) -> discord.Thread | None:
        """
        Full flow: upsert customer → create Plain thread → create Discord thread → link them.
        Returns the Discord Thread object on success, None on failure.
        """
        user = message.author

        if not self.plain.api_key:
            log.warning("PLAIN_API_KEY not set — ticket creation skipped")
            return None

        # 1. Upsert customer in Plain
        plain_customer_id = await self.plain.upsert_customer(
            discord_user_id=str(user.id),
            discord_username=str(user),
            discord_display_name=user.display_name,
        )
        if not plain_customer_id:
            log.error(f"Failed to upsert Plain customer for {user}")
            return None

        # 2. Create Plain thread
        label_ids = [PLAIN_LABEL_TYPE_ID] if PLAIN_LABEL_TYPE_ID else None
        plain_thread_id = await self.plain.create_thread(
            customer_id=plain_customer_id,
            title=f"Discord: {issue_summary[:80]}",
            message_text=full_context,
            discord_channel_id=str(message.channel.id),
            discord_message_id=str(message.id),
            discord_username=str(user),
            label_type_ids=label_ids,
        )
        if not plain_thread_id:
            log.error(f"Failed to create Plain thread for {user}")
            return None

        # 3. Create a private Discord thread on the original message
        thread_name = f"🎫 {user.display_name} – Support Ticket"
        try:
            discord_thread = await message.create_thread(
                name=thread_name[:100],
                auto_archive_duration=1440,  # 24 hours
            )
        except discord.Forbidden:
            log.error("Bot lacks permission to create threads in this channel")
            return None
        except Exception as e:
            log.error(f"Failed to create Discord thread: {e}")
            return None

        # 4. Register the link
        self._active_tickets[discord_thread.id] = plain_thread_id
        self._user_tickets[user.id] = discord_thread.id
        await register_thread_link(plain_thread_id, discord_thread.id)

        # 5. Post welcome message in the new thread
        await discord_thread.send(
            f"👋 Hey {user.mention}! A support ticket has been opened for you.\n\n"
            f"**Ticket ID:** `{plain_thread_id}`\n\n"
            f"Our team has received your report and will reply here as soon as possible. "
            f"You can send additional information or updates by typing in this thread.\n\n"
            f"_Type `!close` when your issue has been resolved._"
        )

        log.info(
            f"Ticket opened: Plain={plain_thread_id}, "
            f"Discord thread={discord_thread.id}, user={user}"
        )
        return discord_thread

    async def forward_to_plain(self, discord_thread_id: int, user: discord.User, text: str) -> bool:
        """Forward a user message from a Discord ticket thread to Plain."""
        plain_thread_id = self._active_tickets.get(discord_thread_id)
        if not plain_thread_id:
            return False

        # Prefix with user name so agents know who typed it
        formatted = f"{user.display_name}: {text}"
        return await self.plain.reply_to_thread(
            thread_id=plain_thread_id,
            text=formatted,
        )

    def close_ticket(self, discord_thread_id: int, user_id: int):
        """Remove local tracking for a resolved ticket."""
        self._active_tickets.pop(discord_thread_id, None)
        self._user_tickets.pop(user_id, None)


# ─── Bot ──────────────────────────────────────────────────────────────────────

class BankrSupportBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)

        self.docs          = SemanticDocsManager()
        self.conversations = ConversationManager()
        self.ollama        = OllamaClient()
        self.plain         = PlainClient(PLAIN_API_KEY) if PLAIN_API_KEY else None
        self.tickets       = PlainTicketManager(self.plain) if self.plain else None

        self.recently_flagged:    dict[tuple, datetime] = {}
        self._handled_message_ids: set[int]             = set()
        # Tracks users who have been asked "want a ticket?" but haven't answered yet.
        # Key: (channel_id, user_id)  Value: issue summary string
        self._pending_escalations: dict[tuple, str]     = {}
        # Tracks threads currently in the deletion countdown so !keep can cancel
        self._deletion_tasks: dict[int, asyncio.Task]    = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(
            f"Monitoring {'all channels' if not MONITORED_CHANNEL_IDS else f'channels: {MONITORED_CHANNEL_IDS}'}"
        )
        log.info(
            f"Using model: {BANKR_LLM_MODEL} via Bankr LLM Gateway "
            f"({'key set' if BANKR_LLM_KEY else 'WARNING: no BANKR_LLM_KEY set!'})"
        )
        if PLAIN_API_KEY:
            log.info("Plain integration: ENABLED")
        else:
            log.warning("Plain integration: DISABLED (PLAIN_API_KEY not set)")

        if is_using_redis():
            log.info("Thread map: Redis ✅")
        else:
            log.warning("Thread map: in-memory only — set REDIS_URL for production")

        await self.docs.ensure_ready()
        asyncio.ensure_future(self._cleanup_loop())

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(300)
            self.conversations.cleanup_expired()

            if len(self._handled_message_ids) > 1000:
                self._handled_message_ids = set(sorted(self._handled_message_ids)[-500:])

            now = datetime.now(timezone.utc)
            self.recently_flagged = {
                k: v for k, v in self.recently_flagged.items()
                if now - v < timedelta(minutes=REFLAG_COOLDOWN_MINUTES)
            }

            # Clear pending escalations for users whose conversations have expired
            # so stale ticket offers don't reactivate on a new session
            stale_pending = [
                k for k in self._pending_escalations
                if not self.conversations.has_active_conversation(k[0], k[1])
            ]
            for k in stale_pending:
                del self._pending_escalations[k]
            if stale_pending:
                log.info(f"Cleared {len(stale_pending)} stale pending escalations")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _build_system_prompt(self, relevant_docs: str) -> str:
        return f"""You are a helpful support bot for Bankr — a platform for AI agents that fund themselves through DeFi and token launching.

Your job is to help users with questions about Bankr using the relevant documentation excerpts below.

Guidelines:
- Answer based on the documentation provided. Be friendly, concise, and clear — this is Discord, not a formal ticket.
- If you show code or commands, use Discord markdown (wrap in backticks).
- Don't make up API endpoints, prices, or features that aren't in the docs.
- Keep responses short — if something needs a long explanation, break it into steps.
- Never repeat the user's question back to them. Just answer it directly.
- IMPORTANT: Detect the language of the user's message and always respond in the same language. If the user writes in Simplified Chinese (简体中文), respond entirely in Simplified Chinese. If the user writes in Korean (한국어), respond entirely in Korean. If they write in English, respond in English.

Escalation:
You have two escalation tags. Use them at the END of your response, never in the middle.

[SUGGEST_ESCALATE: <one-line summary>]
Use this when you have given the user something concrete to try but the issue may still need human help if it does not work.
Example: you diagnosed a likely cause and gave steps, but it could be a backend issue.
This tag tells the bot to add a follow-up note saying "if that doesn't fix it, reply and I'll open a ticket."
The ticket is NOT opened yet — only if the user comes back saying it didn't work.

[NEEDS_TICKET: <one-line summary>]
Use this when you have nothing useful to suggest and the issue clearly requires human investigation.
Example: an error you cannot diagnose at all, a missing feature, account-level issues.
This tag tells the bot to ask the user if they want a ticket opened. The ticket is NOT opened automatically.

Rules:
- Use [SUGGEST_ESCALATE] when you have a partial or possible answer.
- Use [NEEDS_TICKET] only when you have nothing to offer.
- Never use both tags in the same response.
- Do not escalate general how-to questions, chain support questions, or anything you can answer from the docs.
- For partnership/business inquiries: direct them to #partnership-request. Do NOT use either tag for those.

--- RELEVANT BANKR DOCUMENTATION ---
{relevant_docs}
--- END DOCUMENTATION ---"""

    def _was_recently_flagged(self, channel_id: int, user_id: int) -> bool:
        key = (channel_id, user_id)
        if key not in self.recently_flagged:
            return False
        return datetime.now(timezone.utc) - self.recently_flagged[key] < timedelta(minutes=REFLAG_COOLDOWN_MINUTES)

    def _mark_flagged(self, channel_id: int, user_id: int):
        self.recently_flagged[(channel_id, user_id)] = datetime.now(timezone.utc)

    def _is_disengaging(self, content: str) -> bool:
        low = content.strip().lower()
        if low in DISENGAGE_COMMANDS:
            return True
        if len(content) < 80:
            return any(p.search(content) for p in DISENGAGE_PATTERNS)
        return False

    async def _disengage(self, message: discord.Message):
        self.conversations.clear(message.channel.id, message.author.id)
        # Also clear any pending ticket offer so it doesn't linger
        self._pending_escalations.pop((message.channel.id, message.author.id), None)
        replies = [
            "Glad I could help! Feel free to ping me anytime 👋",
            "No problem! Come back if you have more questions 😊",
            "Happy to help! Good luck with Bankr 🚀",
            "Anytime! Feel free to tag me if anything else comes up.",
        ]
        await message.reply(random.choice(replies), mention_author=False)
        log.info(f"Disengaged from {message.author} in #{message.channel.name}")

    def _clean_content(self, message: discord.Message) -> str:
        content = message.content
        for mention in message.mentions:
            content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        return content.strip()

    # ── Escalation ─────────────────────────────────────────────────────────

    def _build_ticket_context(self, message: discord.Message, original_content: str) -> str:
        """Build conversation context string for a Plain ticket."""
        history = self.conversations.get_history(message.channel.id, message.author.id)
        context_lines = []
        for msg in history[-6:]:
            role_label = "User" if msg["role"] == "user" else "Bot"
            context_lines.append(f"**{role_label}:** {msg['content']}")
        return "\n\n".join(context_lines) if context_lines else original_content

    async def _open_ticket_for_user(
        self,
        message: discord.Message,
        issue_summary: str,
        original_content: str,
    ) -> str:
        """
        Actually opens a Plain ticket + Discord thread.
        Returns a string to append to the bot's response.
        """
        if not self.tickets:
            log.warning("Plain not configured; cannot open ticket")
            return (
                "\n\n⚠️ This looks like it needs human attention. "
                "Please open a ticket in **#bug-reports** and our team will help you."
            )

        # User already has an open ticket — forward to it instead
        existing_thread_id = self.tickets.user_has_open_ticket(message.author.id)
        if existing_thread_id:
            full_context = self._build_ticket_context(message, original_content)
            await self.tickets.forward_to_plain(existing_thread_id, message.author, original_content)
            return (
                f"\n\n📋 I've added this to your existing support ticket. "
                f"Check <#{existing_thread_id}> for updates from our team."
            )

        full_context = self._build_ticket_context(message, original_content)
        discord_thread = await self.tickets.open_ticket(
            message=message,
            issue_summary=issue_summary,
            full_context=full_context,
        )

        if discord_thread:
            return (
                f"\n\n🎫 I've opened a support ticket for you in {discord_thread.mention}. "
                f"Our team will respond there. You can also send additional details in that thread."
            )
        else:
            return (
                "\n\n⚠️ I wasn't able to create a ticket automatically. "
                "Please head to **#bug-reports** and our team will help you out."
            )

    async def _maybe_escalate(self, message: discord.Message, response: str, original_content: str) -> str:
        """
        Handles two escalation tags the LLM can emit:

        [SUGGEST_ESCALATE: summary]
          Bot gave the user something to try. Strips the tag and appends a
          soft follow-up note. Sets a pending escalation so if the user
          replies saying it didn't work, the ticket opens then.

        [NEEDS_TICKET: summary]
          Bot has nothing to suggest. Strips the tag, asks the user if they
          want a ticket opened. Ticket only opens if they say yes.
        """
        key = (message.channel.id, message.author.id)

        # ── SUGGEST_ESCALATE — gave something to try, defer ticket ──────────
        suggest_match = re.search(r"\[SUGGEST_ESCALATE:\s*(.+?)\]", response, re.IGNORECASE)
        if suggest_match:
            issue_summary = suggest_match.group(1).strip()
            clean_response = re.sub(r"\s*\[SUGGEST_ESCALATE:[^\]]+\]", "", response).strip()

            # Store pending so the next message can open the ticket if needed
            self._pending_escalations[key] = issue_summary

            return (
                clean_response
                + "\n\n-# _If that doesn't fix it, just reply here and I'll open a support ticket for you._"
            )

        # ── NEEDS_TICKET — nothing to try, ask permission ───────────────────
        needs_match = re.search(r"\[NEEDS_TICKET:\s*(.+?)\]", response, re.IGNORECASE)
        if needs_match:
            issue_summary = needs_match.group(1).strip()
            clean_response = re.sub(r"\s*\[NEEDS_TICKET:[^\]]+\]", "", response).strip()

            # Store pending and wait for yes/no
            self._pending_escalations[key] = issue_summary

            return (
                clean_response
                + "\n\n💬 This looks like something our team needs to look at directly. "
                "Would you like me to open a support ticket for you? Just reply **yes** and I'll get one started."
            )

        return response

    async def _check_pending_escalation(
        self,
        message: discord.Message,
        content: str,
    ) -> bool:
        """
        Called at the start of _handle_support_message.
        If the user has a pending escalation offer and their reply is a yes,
        open the ticket immediately and return True (caller should skip normal LLM flow).
        If it's a clear no, clear the pending and return False.
        If ambiguous, return False and let normal flow continue (pending stays).
        """
        key = (message.channel.id, message.author.id)
        if key not in self._pending_escalations:
            return False

        issue_summary = self._pending_escalations[key]
        low = content.lower().strip()

        YES_SIGNALS = {"yes", "yeah", "yep", "yup", "sure", "please", "open", "ok", "okay", "go ahead", "do it", "y"}
        NO_SIGNALS  = {"no", "nope", "nah", "never mind", "nevermind", "nvm", "cancel", "don't", "dont", "no thanks", "n"}

        is_yes = low in YES_SIGNALS or any(low.startswith(y) for y in YES_SIGNALS)
        is_no  = low in NO_SIGNALS  or any(low.startswith(n) for n in NO_SIGNALS)

        if is_yes:
            del self._pending_escalations[key]
            suffix = await self._open_ticket_for_user(message, issue_summary, content)
            await message.reply(
                "On it! Opening a ticket now..." + suffix,
                mention_author=False,
            )
            log.info(f"User confirmed ticket for pending escalation: {message.author}")
            return True

        if is_no:
            del self._pending_escalations[key]
            await message.reply(
                "No problem! Feel free to come back if anything changes. 👋",
                mention_author=False,
            )
            log.info(f"User declined ticket: {message.author}")
            return True

        # Ambiguous reply — clear pending (they moved on) and let normal flow handle it
        del self._pending_escalations[key]
        return False

    # ── Ticket Thread Message Handling ─────────────────────────────────────

    async def _schedule_thread_deletion(
        self,
        thread: discord.Thread,
        triggered_by: str = "user",
        on_delete=None,
    ):
        """
        Posts a 10-second countdown warning in the thread then deletes it.
        Stores the task in _deletion_tasks so !keep can cancel it.

        on_delete: optional callback called just before deletion — used to
                   close ticket tracking AFTER the countdown, not before,
                   so that !keep messages still route through the ticket handler.
        """
        thread_id = thread.id

        async def _do_delete():
            try:
                await thread.send(
                    "🗑️ This thread will be **deleted in 10 seconds**. "
                    "Reply `!keep` to cancel."
                )
                await asyncio.sleep(10)
                # Run the on_delete callback (e.g. close_ticket) before deleting
                if on_delete:
                    try:
                        on_delete()
                    except Exception as e:
                        log.warning(f"on_delete callback error: {e}")
                try:
                    await thread.delete()
                    log.info(f"Deleted ticket thread {thread_id} (triggered by {triggered_by})")
                except discord.NotFound:
                    log.info(f"Thread {thread_id} already gone, skipping delete")
                except discord.Forbidden:
                    log.warning(f"No permission to delete thread {thread_id}")
            except asyncio.CancelledError:
                # !keep was used — post cancellation notice and exit cleanly
                try:
                    await thread.send("✅ Deletion cancelled. This thread will stay open.")
                except Exception:
                    pass
            finally:
                self._deletion_tasks.pop(thread_id, None)

        task = asyncio.ensure_future(_do_delete())
        self._deletion_tasks[thread_id] = task

    async def _handle_ticket_thread_message(self, message: discord.Message):
        """
        Called when a message is sent in a Discord thread that is an active ticket.
        Forwards the message to Plain and optionally acknowledges it.
        """
        content = message.content.strip()
        thread_id = message.channel.id

        # Cancel a pending deletion if the user replies !keep
        if content.lower() == "!keep":
            # Check bot.py-managed deletion tasks (user-triggered closes)
            task = self._deletion_tasks.get(thread_id)
            if task and not task.done():
                task.cancel()
                log.info(f"Thread deletion cancelled by {message.author} in {thread_id}")
                return

            # Check webhook_server-managed deletion tasks (Plain-triggered closes)
            # by calling the cancel-deletion endpoint
            if WEBHOOK_SERVER_URL:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{WEBHOOK_SERVER_URL}/cancel-deletion",
                            json={"thread_id": thread_id},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            data = await resp.json()
                            if data.get("cancelled"):
                                log.info(f"Plain-triggered deletion cancelled for thread {thread_id}")
                                return
                except Exception as e:
                    log.warning(f"Could not reach webhook server to cancel deletion: {e}")

            await message.reply("No deletion was scheduled.", mention_author=False)
            return

        # Close command — schedule deletion but keep ticket tracked
        # so !keep messages still route through this handler
        if content.lower() in {"!close", "!done", "!resolved"}:
            await message.reply(
                "✅ Got it — ticket closed. Thanks for reaching out!",
                mention_author=False,
            )
            log.info(f"User closed ticket in Discord thread {thread_id}")
            await self._schedule_thread_deletion(
                message.channel,
                triggered_by="user",
                on_delete=lambda: self.tickets.close_ticket(thread_id, message.author.id),
            )
            return

        # Forward to Plain
        success = await self.tickets.forward_to_plain(
            thread_id, message.author, content
        )
        if success:
            await message.add_reaction("📨")
        else:
            await message.reply(
                "⚠️ Couldn't forward your message to our support team right now. Please try again.",
                mention_author=False,
            )

    # ── Message Routing ────────────────────────────────────────────────────

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.id in self._handled_message_ids:
            return

        # ── Ticket thread messages ──────────────────────────────────────────
        # If the message is in an active Plain ticket thread, forward it.
        if self.tickets and self.tickets.is_ticket_thread(message.channel.id):
            self._handled_message_ids.add(message.id)
            await self._handle_ticket_thread_message(message)
            return

        if MONITORED_CHANNEL_IDS and message.channel.id not in MONITORED_CHANNEL_IDS:
            return

        is_mentioned = self.user in message.mentions
        is_reply_to_bot = (
            message.reference
            and message.reference.resolved
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author == self.user
        )
        has_active_convo = self.conversations.has_active_conversation(
            message.channel.id, message.author.id
        )

        # ── Case 1: Direct mention or reply to bot ──────────────────────
        if is_mentioned or is_reply_to_bot:
            if has_active_convo and self._is_disengaging(message.content):
                await self._disengage(message)
                return

            clean = self._clean_content(message)

            if not has_active_convo and clean and not detect_support_intent(clean)[0]:
                self._handled_message_ids.add(message.id)
                has_korean = bool(re.search(r'[\uac00-\ud7af]', clean))
                has_chinese = bool(re.search(r'[\u4e00-\u9fff]', clean))
                if has_korean:
                    redirect_msg = (
                        "안녕하세요! 저는 Bankr 지원 봇입니다 😊 "
                        "스왑, 지갑, 토큰 발행, API 등 Bankr 플랫폼에 관한 질문을 도와드립니다. "
                        "무엇을 도와드릴까요?"
                    )
                elif has_chinese:
                    redirect_msg = (
                        "你好！我是 Bankr 支持机器人 😊 "
                        "我专门解答关于 Bankr 平台的问题，包括代币兑换、钱包、代币发行、API 等。"
                        "有什么我可以帮你的吗？"
                    )
                else:
                    redirect_msg = (
                        "Hey! I'm the Bankr support bot — I'm here to help with questions about the platform. "
                        "Feel free to ask me anything about swaps, wallets, token launches, the API, or anything else Bankr-related! 😊"
                    )
                await message.reply(redirect_msg, mention_author=False)
                log.info(f"Non-support mention from {message.author}, sent polite redirect")
                return

            self._handled_message_ids.add(message.id)
            await self._handle_support_message(message)
            return

        # ── Case 2: Active conversation ─────────────────────────────────
        if has_active_convo:
            if self._is_disengaging(message.content):
                await self._disengage(message)
                return

            flagged, score = detect_support_intent(message.content)
            if score < 1:
                log.info(
                    f"Active convo with {message.author} but off-topic (score={score}), "
                    f"staying silent: {message.content[:60]}"
                )
                return

            self._handled_message_ids.add(message.id)
            await self._handle_support_message(message)
            return

        # ── Case 3: Passive monitoring ───────────────────────────────────
        if MONITORED_CHANNEL_IDS and message.channel.id not in MONITORED_CHANNEL_IDS:
            return

        if self._was_recently_flagged(message.channel.id, message.author.id):
            return

        flagged, score = detect_support_intent(message.content)
        if flagged:
            log.info(f"Support intent (score={score}) from {message.author}: {message.content[:80]}")
            self._mark_flagged(message.channel.id, message.author.id)
            self._handled_message_ids.add(message.id)
            await self._send_proactive_offer(message)

    async def _send_proactive_offer(self, message: discord.Message):
        """
        Proactive outreach — single message (greeting folded into the support response).
        Sets is_first_response=True so the close footer is appended.
        """
        try:
            await asyncio.sleep(1)
            await self._handle_support_message(message, is_proactive=True)
        except discord.errors.DiscordServerError as e:
            log.warning(f"Discord server error in proactive offer (skipping): {e}")
        except Exception as e:
            log.error(f"Unexpected error in proactive offer: {e}")

    async def _handle_support_message(
        self,
        message: discord.Message,
        is_proactive: bool = False,
    ):
        """
        Core response handler.

        is_proactive: True when called from _send_proactive_offer. Prepends a
                      greeting so the bot introduction and answer arrive as one
                      message instead of two.

        First-response footer: appended whenever this is the opening message of
        a new conversation, reminding the user how to close the session.
        """
        content = self._clean_content(message)

        # Determine if this is the opening message of a new conversation
        # (before we add to history, so history is still empty)
        is_first_response = not self.conversations.has_active_conversation(
            message.channel.id, message.author.id
        )

        if not content:
            await message.reply("Hey! What can I help you with? 😊", mention_author=False)
            return

        # Check if the user is responding to a pending ticket offer (yes/no)
        # If handled, skip the normal LLM flow entirely
        if await self._check_pending_escalation(message, content):
            return

        self.conversations.add_message(message.channel.id, message.author.id, "user", content)

        try:
            async with message.channel.typing():
                relevant_docs = await self.docs.query(content)
                system        = self._build_system_prompt(relevant_docs)
                history       = self.conversations.get_history(message.channel.id, message.author.id)
                response      = await self.ollama.chat(history, system=system)
        except discord.errors.DiscordServerError as e:
            log.warning(f"Discord 503 on typing indicator, continuing without it: {e}")
            relevant_docs = await self.docs.query(content)
            system        = self._build_system_prompt(relevant_docs)
            history       = self.conversations.get_history(message.channel.id, message.author.id)
            response      = await self.ollama.chat(history, system=system)

        # Check for escalation signal from the LLM
        response = await self._maybe_escalate(message, response, content)

        # Prepend greeting for proactive outreach (single message instead of two)
        if is_proactive:
            greeting = f"Hey {message.author.mention}! 👋 I'm the Bankr support bot — \n\n"
            response = greeting + response

        # Append close footer on the first message of every new conversation
        if is_first_response:
            footer = "\n\n-# _If this solved your issue, reply_ `!close` _or say thanks to end this conversation._"
            response = response + footer

        self.conversations.add_message(message.channel.id, message.author.id, "assistant", response)

        if len(response) <= 1900:
            await message.reply(response, mention_author=False)
        else:
            chunks = [response[i:i + 1900] for i in range(0, len(response), 1900)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.reply(chunk, mention_author=False)
                else:
                    await message.channel.send(chunk)

        log.info(f"Responded to {message.author} in #{message.channel.name}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env file")
    if not BANKR_LLM_KEY:
        log.warning("BANKR_LLM_KEY not set — LLM calls will fail with 401!")
    if not PLAIN_API_KEY:
        log.warning("PLAIN_API_KEY not set — ticket escalation will be disabled")

    bot = BankrSupportBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
