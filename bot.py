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
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv
import os

from plain_client import PlainClient
from shared import SemanticDocsManager, OllamaClient
from llm_router import LLMRouter
from redis_map import (
    set_thread_link, is_using_redis,
    save_active_ticket, delete_active_ticket, load_active_tickets,
)
from redis_overrides import find_matching_override, record_override_hit
from redis_settings import get_settings
from redis_pubsub import listen_for_reindex, set_reindex_status, heal_stale_reindex_status
import db

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
# How long we remember having sent someone the polite redirect. If they mention
# or reply to the bot again inside this window, we treat the persistence itself
# as support intent and answer instead of re-serving the same canned line.
# Deliberately longer than CONVERSATION_TTL_MINUTES: the failure we saw had a
# user come back 25 minutes after a redirect and get redirected again.
REDIRECT_MEMORY_MINUTES = int(os.getenv("REDIRECT_MEMORY_MINUTES", "120"))
REFLAG_COOLDOWN_MINUTES  = int(os.getenv("REFLAG_COOLDOWN_MINUTES", "15"))

CHUNK_SIZE          = int(os.getenv("CHUNK_SIZE", "600"))
CHUNK_OVERLAP       = int(os.getenv("CHUNK_OVERLAP", "80"))
TOP_K_CHUNKS        = int(os.getenv("TOP_K_CHUNKS", "6"))
MAX_RETRIEVED_CHARS = int(os.getenv("MAX_RETRIEVED_CHARS", "8000"))

# Plain integration
PLAIN_API_KEY          = os.getenv("PLAIN_API_KEY", "")
PLAIN_LABEL_TYPE_ID    = os.getenv("PLAIN_LABEL_TYPE_ID", "")
# Role names to add to every ticket thread (comma-separated)
# e.g. MOD_ROLE_NAME=Moderator,Admin
MOD_ROLE_NAME          = os.getenv("MOD_ROLE_NAME", "Moderator")
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
    # ── Transaction / funds-movement vocabulary ───────────────────────────
    # Added 2026-07-19 after a user asking "i have some missing coins in this
    # transaction 0x..." scored ZERO and got the canned redirect. None of
    # "transaction", "coins", "missing", or a raw tx hash were recognized,
    # even though a pasted hash is about as strong a support signal as exists.
    r"\b(transactions?|txn?s?|tx hash|hashe?s?)\b",
    r"\b0x[a-fA-F0-9]{16,}\b",                     # pasted tx hash / address
    r"\b(missing|lost|disappeared|vanished|unaccounted)\b",
    r"\b(never (got|received|arrived|showed|came)|didn'?t (get|receive|arrive|show|come)|hasn'?t (arrived|shown|come))\b",
    r"\b(coins?|funds?|money|deposits?|withdrawals?)\b",
    r"\b(sent|sending|received|receiving|transfers?|transferred)\b",
    r"\b(pending|stuck|failed|reverted|dropped)\b",
    r"\b(usdc|usdt|weth|eth|dai)\b",
    # Typo-tolerant swap: the original \bswaps?\b missed "swaped"/"swapd".
    r"\bswap\w*\b",
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
            lambda: {
                "history": [],
                "last_active": datetime.now(timezone.utc),
                # session_id groups all messages of one back-and-forth for the
                # admin transcript view. Minted lazily on first access (see
                # get_session_id) and discarded when the conversation is
                # cleared or expires — so a user returning after the TTL
                # starts a fresh session, which is the intended behavior for
                # non-escalated chats.
                "session_id": None,
            }
        )

    def _key(self, channel_id: int, user_id: int) -> tuple:
        return (channel_id, user_id)

    def add_message(self, channel_id: int, user_id: int, role: str, content: str):
        key = self._key(channel_id, user_id)
        self.conversations[key]["history"].append({"role": role, "content": content})
        self.conversations[key]["last_active"] = datetime.now(timezone.utc)
        if len(self.conversations[key]["history"]) > 20:
            self.conversations[key]["history"] = self.conversations[key]["history"][-20:]

    def get_session_id(self, channel_id: int, user_id: int) -> str:
        """
        Return the session id for this conversation, minting one on first
        call. All messages logged during the same conversation (before its
        TTL expires or it's cleared) share this id, so the admin panel can
        reassemble the full transcript.

        Non-escalated conversations keep their minted 'sess_...' id. When a
        conversation escalates, set_session_id swaps it to a ticket-anchored
        id so the ticket case stays one transcript across TTL gaps.
        """
        key = self._key(channel_id, user_id)
        conv = self.conversations[key]
        if not conv.get("session_id"):
            conv["session_id"] = f"sess_{uuid.uuid4().hex[:20]}"
        return conv["session_id"]

    def set_session_id(self, channel_id: int, user_id: int, session_id: str):
        """
        Override a conversation's session id. Used on escalation to re-anchor
        the session to the ticket ('ticket_<discord_thread_id>'), so messages
        from here on — and, via db.rekey_session, the earlier ones — all group
        under the ticket.
        """
        key = self._key(channel_id, user_id)
        self.conversations[key]["session_id"] = session_id

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
        # discord_thread_id → plain_customer_id. Captured at ticket creation
        # so forward_to_plain can call plain.send_chat (chat-channel reply)
        # instead of plain.reply_to_thread (email-channel reply that bounces
        # against our fake `discord_xxx@discord.invalid` addresses and ends
        # up suppression-listed). Per Plain's custom-channels docs:
        #   "If you don't have a way of pairing a customer with their real
        #    email address on creation … use sendChat for replies instead
        #    of replyToThread."
        # See plain_client.send_chat for the full rationale.
        #
        # Tickets opened before this dict existed have no entry — those keep
        # working via the old reply_to_thread path until they close naturally.
        self._thread_customers: dict[int, str] = {}

    def is_ticket_thread(self, channel_id: int) -> bool:
        return channel_id in self._active_tickets

    def get_plain_thread_id(self, discord_thread_id: int) -> str | None:
        return self._active_tickets.get(discord_thread_id)

    def user_has_open_ticket(self, user_id: int) -> int | None:
        """Returns the Discord thread ID of the user's open ticket, or None."""
        return self._user_tickets.get(user_id)

    async def user_has_open_ticket_validated(
        self,
        user_id: int,
        bot_client: discord.Client,
    ) -> int | None:
        """
        Same as user_has_open_ticket, but verifies the Discord thread still
        exists before returning the id. If our state says the user has a
        ticket but the Discord thread is gone (deleted manually, or our
        cleanup was incomplete in some past state), this self-heals by
        clearing the stale entry and returning None.

        Background: in production we observed `user_tickets` entries
        persisting past their Discord thread's deletion — the webhook
        server used to delete the Discord thread without cleaning the
        Redis hash on agent-resolved tickets. Users coming back later
        would get told they had an open ticket in <#deleted> which Discord
        rendered as ⁠unknown. The cross-process cleanup is fixed at the
        source (webhook_server.py now calls delete_active_ticket on
        resolution), but this validation gives defense-in-depth against
        any other path that could leave the same state.

        Cost: one fetch_channel call when the user has an apparent open
        ticket — small, and only on the ticket-creation path. Returns None
        with no Discord call if the user has no apparent ticket at all.
        """
        thread_id = self._user_tickets.get(user_id)
        if thread_id is None:
            return None

        # Try the cache first (free), then fetch (one API call) before giving up.
        channel = bot_client.get_channel(thread_id)
        if channel is None:
            try:
                channel = await bot_client.fetch_channel(thread_id)
            except discord.NotFound:
                # The thread is genuinely gone. Self-heal.
                log.info(
                    f"Stale ticket state for user {user_id} → thread {thread_id} "
                    f"(Discord thread no longer exists); cleaning up"
                )
                await self.close_ticket(thread_id, user_id)
                return None
            except discord.Forbidden:
                # We can't see the thread for permissions reasons, but it
                # might still exist. Don't clean state on a permissions
                # error — that could erase a real ticket. Return the id
                # and let the caller treat the user as having an open
                # ticket (which is at worst conservative).
                log.warning(
                    f"Couldn't verify thread {thread_id} for user {user_id} "
                    f"(forbidden); assuming ticket still open"
                )
                return thread_id
            except Exception as e:
                # Network blip, rate limit, anything else. Same conservative
                # call as Forbidden — don't erase state on transient errors.
                log.warning(
                    f"Couldn't verify thread {thread_id} for user {user_id} "
                    f"({type(e).__name__}: {e}); assuming ticket still open"
                )
                return thread_id

        # Channel exists — ticket is real.
        return thread_id

    async def open_ticket(
        self,
        message: discord.Message,
        issue_summary: str,
        full_context: str,
        bot_user: discord.ClientUser | None = None,
        for_user: discord.Member | discord.User | None = None,
    ) -> discord.Thread | None:
        """
        Full flow: upsert customer → create Plain thread → create Discord thread → link them.
        Returns the Discord Thread object on success, None on failure.

        for_user: when provided, the ticket is opened on behalf of this user
        instead of message.author. Used by the !openticket moderator command
        so the Plain customer record + welcome ping go to the target user, not
        the moderator who typed the command. message.channel is still used to
        anchor the Discord thread (so it spawns in whatever channel the mod ran
        the command in), and message.id is still used for the thread-field
        metadata link back to the originating message.
        """
        user = for_user or message.author

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
            discord_thread = await message.channel.create_thread(
                name=thread_name[:100],
                auto_archive_duration=1440,  # 24 hours
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
        except discord.Forbidden:
            log.error("Bot lacks permission to create threads in this channel")
            return None
        except Exception as e:
            log.error(f"Failed to create Discord thread: {e}")
            return None

        # 4. Register the link in memory and persist to Redis
        self._active_tickets[discord_thread.id] = plain_thread_id
        self._user_tickets[user.id] = discord_thread.id
        self._thread_customers[discord_thread.id] = plain_customer_id
        await register_thread_link(plain_thread_id, discord_thread.id)
        await save_active_ticket(
            discord_thread.id,
            plain_thread_id,
            user.id,
            plain_customer_id=plain_customer_id,
        )

        # 5. Add configured roles to the thread so they have visibility
        # MOD_ROLE_NAME supports comma-separated values e.g. "Moderator,Admin"
        guild = message.guild
        if guild:
            role_names = [r.strip() for r in MOD_ROLE_NAME.split(",") if r.strip()]
            for role_name in role_names:
                role = discord.utils.get(guild.roles, name=role_name)
                if role:
                    added = 0
                    for member in role.members:
                        if (bot_user is None or member != bot_user) and not member.bot:
                            try:
                                await discord_thread.add_user(member)
                                added += 1
                            except Exception as e:
                                log.warning(f"Could not add {member} ({role_name}) to ticket thread: {e}")
                    log.info(f"Added {added} member(s) from '{role_name}' to ticket thread {discord_thread.id}")
                else:
                    log.warning(f"Role '{role_name}' not found in guild — skipping")

        # 6. Post welcome message in the new thread
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
        """
        Forward a user message from a Discord ticket thread to Plain.

        Path selection:
          - If we have the Plain customer_id for this ticket (every ticket
            opened after the 2026-06-02 fix), call `send_chat`. That appends
            a chat-style message with no SMTP send, sidestepping the bounce-
            then-suppress problem that broke email-channel replies.
          - Otherwise (pre-fix tickets restored from Redis without a stored
            customer_id), fall back to the old `reply_to_thread` path so
            in-flight tickets keep working until they close.

        Returns True on a successful relay, False on failure.
        """
        plain_thread_id = self._active_tickets.get(discord_thread_id)
        if not plain_thread_id:
            return False

        # Prefix with [discord-relay] so webhook_server.py can identify and skip
        # this message when Plain echoes it back, preventing an echo loop.
        # Also prefix with user name so agents know who typed it in Plain.
        formatted = f"[discord-relay] {user.display_name}: {text}"

        customer_id = self._thread_customers.get(discord_thread_id)
        if customer_id:
            chat = await self.plain.send_chat(
                customer_id=customer_id,
                thread_id=plain_thread_id,
                text=formatted,
            )
            return chat is not None

        # Legacy path — pre-fix ticket with no stored customer_id. The email
        # send may bounce (that's how we got here), but reply_to_thread is
        # still the correct call for an email-channel thread.
        log.warning(
            f"forward_to_plain: no customer_id for ticket {discord_thread_id}; "
            f"using legacy reply_to_thread path (ticket pre-dates send_chat fix)"
        )
        return await self.plain.reply_to_thread(
            thread_id=plain_thread_id,
            text=formatted,
        )

    async def load_from_redis(self) -> None:
        """
        Restore active ticket state from Redis on startup.
        Called once from BankrSupportBot.on_ready so open tickets
        survive service redeploys.

        load_active_tickets returns three dicts — the third (customers) may
        be empty or a subset of active_tickets for tickets that pre-date
        customer_id storage. Both shapes are fine; forward_to_plain falls
        back to reply_to_thread when no customer_id is on file.
        """
        active, users, customers = await load_active_tickets()
        self._active_tickets.update(active)
        self._user_tickets.update(users)
        self._thread_customers.update(customers)
        if active:
            log.info(
                f"Restored {len(active)} active ticket(s) from Redis "
                f"({len(customers)} with customer_id, "
                f"{len(active) - len(customers)} on legacy reply path)"
            )

    async def close_ticket(self, discord_thread_id: int, user_id: int) -> None:
        """Remove local and Redis tracking for a resolved ticket."""
        self._active_tickets.pop(discord_thread_id, None)
        self._user_tickets.pop(user_id, None)
        self._thread_customers.pop(discord_thread_id, None)
        await delete_active_ticket(discord_thread_id, user_id)


# ─── Bot ──────────────────────────────────────────────────────────────────────

class BankrSupportBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)

        self.docs          = SemanticDocsManager()
        self.conversations = ConversationManager()
        # self.ollama is the LLM router: tries Bankr gateway first, falls back
        # to Ollama Cloud (Gemma) on failure. Attribute kept named "ollama" so
        # existing call sites (self.ollama.chat(...)) don't change.
        self.ollama        = LLMRouter()
        self.plain         = PlainClient(PLAIN_API_KEY) if PLAIN_API_KEY else None
        self.tickets       = PlainTicketManager(self.plain) if self.plain else None

        self.recently_flagged:    dict[tuple, datetime] = {}
        self._handled_message_ids: set[int]             = set()
        # (channel_id, user_id) -> datetime of the last polite redirect we
        # sent them. A redirect does NOT open a conversation, so without this
        # a user whose phrasing the keyword scorer under-counts gets the same
        # canned line on every follow-up forever. See the intent gate in
        # on_message: coming back after a redirect is itself the intent signal.
        self._recent_redirects: dict[tuple, datetime]   = {}
        # Tracks users who have been asked "want a ticket?" but haven't answered yet.
        # Key: (channel_id, user_id)  Value: issue summary string
        self._pending_escalations: dict[tuple, str]     = {}
        # Parallel to _pending_escalations: the conversations-table row id of the
        # message that triggered the escalation offer. When the user later says
        # "yes" and a ticket is created, we backfill plain_thread_id onto that
        # row via db.update_conversation_ticket — so "tickets created" stats are
        # accurate. Keyed the same way as _pending_escalations.
        self._pending_row_ids: dict[tuple, int]         = {}
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

        # Restore open tickets from Redis so they survive redeploys
        if self.tickets:
            await self.tickets.load_from_redis()

        # Initialize the stats database (no-op if DATABASE_URL is unset)
        await db.init_db()

        await self.docs.ensure_ready()
        asyncio.ensure_future(self._cleanup_loop())
        # Self-heal: if a previous run crashed mid-reindex, its status is stuck
        # at 'running' and the admin button stays disabled. Clear it on boot.
        await heal_stale_reindex_status("bot")
        # Listen for manual docs re-index signals from the admin panel.
        asyncio.ensure_future(listen_for_reindex(self._on_reindex_signal))

    async def _on_reindex_signal(self, triggered_by: str):
        """
        Callback for the admin panel's manual docs re-index button. Reports
        status to Redis so the panel can show this service's progress, then
        rebuilds the docs index.
        """
        await set_reindex_status("bot", "running", detail=f"by {triggered_by}")
        try:
            ok = await self.docs.force_reindex()
            await set_reindex_status(
                "bot",
                "done" if ok else "failed",
                detail="" if ok else "fetch or index failed",
            )
        except Exception as e:
            log.error(f"Bot reindex failed: {e}")
            await set_reindex_status("bot", "failed", detail=str(e)[:120])

    async def _cleanup_loop(self):
        # The loop ticks every 300s. Pruning old stats rows only needs to run
        # ~once a day, so we count ticks: 288 ticks * 300s = 24h.
        prune_tick = 0
        PRUNE_EVERY = 288

        while True:
            await asyncio.sleep(300)
            self.conversations.cleanup_expired()

            if len(self._handled_message_ids) > 1000:
                self._handled_message_ids = set(sorted(self._handled_message_ids)[-500:])

            # Drop redirect memory past its window so the dict can't grow
            # without bound in a busy server.
            if self._recent_redirects:
                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=REDIRECT_MEMORY_MINUTES
                )
                self._recent_redirects = {
                    k: v for k, v in self._recent_redirects.items() if v > cutoff
                }

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
                self._pending_row_ids.pop(k, None)
            if stale_pending:
                log.info(f"Cleared {len(stale_pending)} stale pending escalations")

            # Prune stats rows past the retention window, roughly daily.
            prune_tick += 1
            if prune_tick >= PRUNE_EVERY:
                prune_tick = 0
                await db.prune_old_rows()

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
These tags exist as a LAST RESORT — only after you have genuinely tried to help and hit a wall.

[SUGGEST_ESCALATE: <one-line summary>]
Use this when ALL of the following are true:
  1. You have already given the user a real answer with concrete steps to try
  2. The issue might still require human help if those steps don't work
  3. This is NOT the first message in the conversation
Example: you gave troubleshooting steps for a wallet access issue but suspect it may be a backend problem.
This tag adds a soft follow-up note — no ticket is opened yet.

[NEEDS_TICKET: <one-line summary>]
Use this when ALL of the following are true:
  1. You have already attempted a doc-based answer and it was not sufficient
  2. The issue clearly requires a human to investigate (account-level, backend bug, data issue)
  3. This is NOT the first message in the conversation
Example: user tried your suggestions and confirmed they didn't work, or the issue is completely outside the docs.
This tag asks the user if they want a ticket — no ticket is opened automatically.

Rules:
- NEVER use either tag on the first message of a conversation. Always attempt to help first.
- NEVER use either tag if you can fully answer the question from the documentation.
- NEVER use either tag for general how-to questions, even complex ones — just answer them.
- Never use both tags in the same response.
- If you are unsure whether to escalate, do not escalate — answer as best you can instead.
- For partnership/business inquiries: direct them to #partnership-request. Do NOT use either tag for those.

Topic tagging:
At the very END of your response, after any escalation tag, append exactly one
topic tag classifying what the user asked about. Format: [TOPIC: <value>]
Choose the single best-fit value from this fixed list ONLY:
  dca, swaps, wallet, fees, token-launch, staking, leaderboard, bankr-club,
  api, openclaw, bridging, airdrop, nft, portfolio, account-access,
  partnership, greeting, other
- Use 'greeting' for greetings/thanks/small talk with no real question.
- Use 'account-access' for login, 401, locked-out, or sign-in problems.
- Use 'other' ONLY if nothing else fits — do not invent new values.
- The tag is for internal analytics; the user must never see it. Always
  include exactly one. Example ending: ...let me know! [TOPIC: dca]

--- RELEVANT BANKR DOCUMENTATION ---
{relevant_docs}
--- END DOCUMENTATION ---"""

    # Valid topic values — must match the fixed list in the system prompt.
    # Anything the model emits outside this set is normalized to 'other' so
    # the stats GROUP BY stays clean.
    _VALID_TOPICS = {
        "dca", "swaps", "wallet", "fees", "token-launch", "staking",
        "leaderboard", "bankr-club", "api", "openclaw", "bridging",
        "airdrop", "nft", "portfolio", "account-access", "partnership",
        "greeting", "other",
    }

    def _extract_topic(self, response: str) -> tuple[str, str]:
        """
        Pull the [TOPIC: x] tag the LLM appends for analytics out of the
        response. Returns (cleaned_response, topic).

        The tag must never reach the user, so this strips it regardless of
        whether topic logging is even enabled. If the tag is missing or the
        value isn't in the fixed vocabulary, topic falls back to 'other'.
        """
        topic = "other"
        match = re.search(r"\[TOPIC:\s*([a-zA-Z\-]+)\s*\]", response, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().lower()
            if candidate in self._VALID_TOPICS:
                topic = candidate
        # Strip the tag (and any leftover whitespace) whether or not it matched
        cleaned = re.sub(r"\s*\[TOPIC:\s*[a-zA-Z\-]+\s*\]", "", response, flags=re.IGNORECASE).strip()
        return cleaned, topic

    def _was_recently_flagged(self, channel_id: int, user_id: int) -> bool:
        key = (channel_id, user_id)
        if key not in self.recently_flagged:
            return False
        return datetime.now(timezone.utc) - self.recently_flagged[key] < timedelta(minutes=REFLAG_COOLDOWN_MINUTES)

    def _mark_flagged(self, channel_id: int, user_id: int):
        self.recently_flagged[(channel_id, user_id)] = datetime.now(timezone.utc)

    async def _is_busy_mode_suppressed(self, message: discord.Message) -> bool:
        """
        Busy mode: when an admin has enabled it from the panel, the bot stops
        sending passive proactive offers to members holding configured staff
        roles (Moderator, Support, etc). The point is high-traffic windows —
        when the support team is answering customers live in channels, the bot
        shouldn't keep jumping in on the team's own messages.

        Returns True if this message's author should be skipped on the passive
        path. Only consulted from Case 3 (passive monitoring) — direct mentions
        and active conversations are never suppressed, so staff can always
        @mention the bot deliberately.
        """
        settings = await get_settings()
        if not settings.get("busy_mode_enabled"):
            return False

        # message.author is a Member in a guild channel (has .roles); in the
        # rare case it's a plain User (e.g. DM), there are no roles to match.
        author_roles = getattr(message.author, "roles", None)
        if not author_roles:
            return False

        busy_roles = {r.lower() for r in settings.get("busy_mode_roles", []) if r.strip()}
        if not busy_roles:
            return False

        for role in author_roles:
            if role.name.lower() in busy_roles:
                log.info(
                    f"Busy mode: suppressing passive offer for {message.author} "
                    f"(role '{role.name}')"
                )
                return True
        return False

    def _is_disengaging(self, content: str) -> bool:
        low = content.strip().lower()
        if low in DISENGAGE_COMMANDS:
            return True
        if len(content) < 80:
            return any(p.search(content) for p in DISENGAGE_PATTERNS)
        return False

    def _is_moderator(self, member: discord.Member) -> bool:
        """
        True if the member holds any role named in MOD_ROLE_NAME (the same env
        var used to decide who gets added to ticket threads). Case-insensitive
        match, comma-separated values supported.

        Used to gate moderator-only commands like !openticket.

        Returns False for non-Members (e.g. DM authors) — moderator status
        is only meaningful inside a guild.
        """
        author_roles = getattr(member, "roles", None)
        if not author_roles:
            return False
        mod_roles = {
            r.strip().lower()
            for r in MOD_ROLE_NAME.split(",")
            if r.strip()
        }
        return any(role.name.lower() in mod_roles for role in author_roles)

    async def _handle_openticket_command(self, message: discord.Message):
        """
        Moderator-only manual ticket opener. Bypasses the LLM escalation path
        entirely, useful when a mod has already triaged a request and wants
        to open the ticket without making the user dance through the bot's
        usual "want a ticket? reply yes" flow.

        Syntax:
            !openticket @user [optional reason text]

        The optional trailing text becomes the ticket subject and first
        message body in Plain. If omitted, a default placeholder is used so
        the agent sees *something* contextual rather than a blank ticket.

        Refused if:
          - the invoker isn't a moderator (silent — no reply, to avoid
            broadcasting the command's existence to non-mods)
          - no @mention is supplied (replies with usage help)
          - the @mention isn't resolvable to a guild Member (e.g. mod typed
            a raw ID; replies with a clear error)
          - the target user is a bot (refused — bots don't open tickets)
          - the target user already has an open ticket (forwards the
            "you've already got one" reply, matching the LLM escalation
            flow's behavior)

        On success, posts a brief confirmation in the channel pointing to
        the new private ticket thread.
        """
        # 1. Gate to moderators. Silent refusal — non-mods don't even learn
        #    the command exists.
        if not self._is_moderator(message.author):
            log.info(
                f"!openticket: refused for {message.author} (not a moderator)"
            )
            return

        # 2. Require at least one user mention. (Bot's own mention doesn't count.)
        targets = [m for m in message.mentions if m.id != self.user.id]
        if not targets:
            await message.reply(
                "Usage: `!openticket @user [optional reason]`",
                mention_author=False,
            )
            return
        if len(targets) > 1:
            await message.reply(
                "I can only open one ticket at a time. Tag a single user.",
                mention_author=False,
            )
            return

        target = targets[0]
        if target.bot:
            await message.reply(
                "Can't open a ticket for a bot.",
                mention_author=False,
            )
            return

        # 3. Pull the optional reason: everything after the @mention and the
        #    !openticket word. We strip mentions out of the raw content the
        #    same way _clean_content does for normal messages.
        raw = message.content
        for m in message.mentions:
            raw = raw.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        reason = re.sub(r"^\s*!openticket\b", "", raw, flags=re.IGNORECASE).strip()
        if not reason:
            reason = (
                f"Ticket opened manually by moderator {message.author.display_name}."
            )

        # 4. Plain integration must be live.
        if not self.tickets:
            await message.reply(
                "Plain isn't configured — can't open a ticket.",
                mention_author=False,
            )
            log.warning("!openticket: tickets manager not initialized")
            return

        # 5. Duplicate-ticket check — same behavior the LLM escalation flow
        #    has when a user with an open ticket asks for another one. Use
        #    the validated version so a stale entry (Discord thread gone but
        #    Redis hash not cleaned) doesn't refuse a legitimate new ticket.
        existing_thread_id = await self.tickets.user_has_open_ticket_validated(
            target.id, self
        )
        if existing_thread_id:
            await message.reply(
                f"{target.mention} already has an open ticket in "
                f"<#{existing_thread_id}>.",
                mention_author=False,
            )
            log.info(
                f"!openticket: refused for {target} — already has ticket "
                f"in {existing_thread_id}"
            )
            return

        # 6. Open the ticket. The moderator's message anchors the Discord
        #    thread (it spawns in their channel) and gives us the message_id
        #    for thread-field metadata. for_user redirects the Plain customer
        #    record + welcome ping to the target user instead of the mod.
        issue_summary = reason[:80]
        full_context = (
            f"**Manually opened by moderator:** {message.author.display_name}\n\n"
            f"**For user:** {target} ({target.display_name})\n\n"
            f"**Reason:**\n{reason}"
        )
        discord_thread = await self.tickets.open_ticket(
            message=message,
            issue_summary=issue_summary,
            full_context=full_context,
            bot_user=self.user,
            for_user=target,
        )

        if discord_thread:
            await message.reply(
                f"🎫 Opened a ticket for {target.mention} in {discord_thread.mention}.",
                mention_author=False,
            )
            log.info(
                f"!openticket: opened by {message.author} for {target} "
                f"→ Discord thread {discord_thread.id}"
            )
        else:
            await message.reply(
                "Couldn't open the ticket — check the bot logs.",
                mention_author=False,
            )

    async def _disengage(self, message: discord.Message):
        self.conversations.clear(message.channel.id, message.author.id)
        # Also clear any pending ticket offer so it doesn't linger
        self._pending_escalations.pop((message.channel.id, message.author.id), None)
        self._pending_row_ids.pop((message.channel.id, message.author.id), None)
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
    ) -> tuple[str, str | None]:
        """
        Actually opens a Plain ticket + Discord thread.
        Returns (suffix, plain_thread_id) where:
          suffix          - string to append to the bot's response
          plain_thread_id - the Plain thread id if a ticket was created/forwarded
                            to, else None. Used to backfill the stats row.
        """
        if not self.tickets:
            log.warning("Plain not configured; cannot open ticket")
            return (
                "\n\n⚠️ This looks like it needs human attention, but I couldn't "
                "open a ticket right now. Please ping a moderator here and "
                "they'll get you sorted.",
                None,
            )

        # User already has an open ticket — forward to it instead. Validate
        # first so a stale entry (Discord thread gone, Redis hash not cleaned)
        # falls through to fresh-ticket creation rather than forwarding to
        # <#deleted_thread> which Discord renders as ⁠unknown.
        existing_thread_id = await self.tickets.user_has_open_ticket_validated(
            message.author.id, self
        )
        if existing_thread_id:
            full_context = self._build_ticket_context(message, original_content)
            await self.tickets.forward_to_plain(existing_thread_id, message.author, original_content)
            existing_plain_id = self.tickets.get_plain_thread_id(existing_thread_id)
            return (
                f"\n\n📋 I've added this to your existing support ticket. "
                f"Check <#{existing_thread_id}> for updates from our team.",
                existing_plain_id,
            )

        full_context = self._build_ticket_context(message, original_content)
        discord_thread = await self.tickets.open_ticket(
            message=message,
            issue_summary=issue_summary,
            full_context=full_context,
            bot_user=self.user,
        )

        if discord_thread:
            plain_thread_id = self.tickets.get_plain_thread_id(discord_thread.id)

            # Re-key this conversation's session to the ticket. The pre-escalation
            # messages were logged under a TTL-scoped 'sess_...' id; switch the
            # live conversation onto a ticket-anchored id and re-point the
            # already-logged rows to match, so the whole case is one transcript.
            old_session = self.conversations.get_session_id(
                message.channel.id, message.author.id
            )
            ticket_session = f"ticket_{discord_thread.id}"
            self.conversations.set_session_id(
                message.channel.id, message.author.id, ticket_session
            )
            asyncio.ensure_future(db.rekey_session(old_session, ticket_session))

            return (
                f"\n\n🎫 I've opened a support ticket for you in {discord_thread.mention}. "
                f"Our team will respond there. You can also send additional details in that thread.",
                plain_thread_id,
            )
        else:
            return (
                "\n\n⚠️ I wasn't able to create a ticket automatically. "
                "Please try again in a few minutes, or ping a moderator here "
                "and they can open one for you.",
                None,
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

        First-exchange guard: if this is the opening message of the conversation
        (only 1 message in history = the one we just added), strip any escalation
        tags silently. The bot must try to help at least once before escalating.
        """
        key = (message.channel.id, message.author.id)

        # ── First-exchange guard ─────────────────────────────────────────────
        # History already has the user message we just added, so length == 1
        # means this is the very first exchange. Strip tags and don't escalate.
        history = self.conversations.get_history(message.channel.id, message.author.id)
        user_message_count = sum(1 for m in history if m["role"] == "user")
        if user_message_count <= 1:
            cleaned = re.sub(r"\s*\[(SUGGEST_ESCALATE|NEEDS_TICKET):[^\]]+\]", "", response, flags=re.IGNORECASE).strip()
            if cleaned != response:
                log.info(f"Stripped escalation tag on first exchange for {message.author}")
            return cleaned

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
            # Row id captured at offer-time — may be None if the bot redeployed
            # between the offer and this "yes" (it lives only in memory).
            row_id = self._pending_row_ids.pop(key, None)

            suffix, plain_thread_id = await self._open_ticket_for_user(
                message, issue_summary, content
            )
            await message.reply(
                "On it! Opening a ticket now..." + suffix,
                mention_author=False,
            )

            # Backfill the stats row so "tickets created" counts this.
            # Capture session_id AFTER _open_ticket_for_user: that call
            # re-keys the conversation (and its rows) to the ticket session,
            # so the post-call session id is the one the escalation rows now
            # carry. Primary path is by session_id (survives a redeploy
            # mid-escalation); the row_id path is a fallback.
            if plain_thread_id:
                session_id = self.conversations.get_session_id(
                    message.channel.id, message.author.id
                )
                marked = await db.mark_ticket_for_session(session_id, plain_thread_id)
                if marked == 0 and row_id:
                    await db.update_conversation_ticket(row_id, plain_thread_id)

            log.info(f"User confirmed ticket for pending escalation: {message.author}")
            return True

        if is_no:
            del self._pending_escalations[key]
            self._pending_row_ids.pop(key, None)
            await message.reply(
                "No problem! Feel free to come back if anything changes. 👋",
                mention_author=False,
            )
            log.info(f"User declined ticket: {message.author}")
            return True

        # Ambiguous reply — clear pending (they moved on) and let normal flow handle it
        del self._pending_escalations[key]
        self._pending_row_ids.pop(key, None)
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
                        import inspect
                        if inspect.iscoroutinefunction(on_delete):
                            await on_delete()
                        else:
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
            # Capture loop-local vars for the closure
            _thread_id = thread_id
            _user_id   = message.author.id

            async def _close_and_delete():
                await self.tickets.close_ticket(_thread_id, _user_id)

            await self._schedule_thread_deletion(
                message.channel,
                triggered_by="user",
                on_delete=_close_and_delete,
            )
            return

        # Forward to Plain
        success = await self.tickets.forward_to_plain(
            thread_id, message.author, content
        )
        if success:
            await message.add_reaction("📨")
        else:
            # Failure path: don't post the "Couldn't forward your message to our
            # support team right now. Please try again." reply. After the
            # send_chat migration (2026-06-02), the only way a forward fails
            # is on legacy pre-fix tickets still on the email/reply_to_thread
            # path — and those failures will trigger Plain's suppression-list
            # error 100% of the time for fake-email customers. Showing a "try
            # again" message just misleads the user since retrying can't work.
            # We add a discreet ❗ reaction so the user knows something didn't
            # land, and the error is still logged via plain_client for us.
            try:
                await message.add_reaction("❗")
            except Exception:
                # React permission could be missing; not worth a separate path.
                pass

        # Log the user's ticket-thread message to the transcript. kind marks it
        # as a transcript-only row so the stats dashboard ignores it; the
        # session is the ticket itself, so it groups with the whole case.
        asyncio.ensure_future(db.log_conversation(
            source="discord",
            kind="ticket_user_msg",
            user_id=str(message.author.id),
            username=message.author.display_name,
            channel_id=str(thread_id),
            question=content,
            response_source="ticket_message",
            session_id=f"ticket_{thread_id}",
            plain_thread_id=self.tickets.get_plain_thread_id(thread_id),
        ))

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

        # ── Moderator commands ──────────────────────────────────────────────
        # !openticket runs before mention/intent checks so it short-circuits
        # the bot's normal response paths. The handler self-gates to mods —
        # non-mods who type it get silent treatment (no reply, no fall-through
        # to intent detection). That keeps the command from being discoverable
        # to random users while still being easy for mods to use.
        if message.content.strip().lower().startswith("!openticket"):
            self._handled_message_ids.add(message.id)
            await self._handle_openticket_command(message)
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

            # Intent gate for the FIRST message of a conversation. Two
            # relaxations over the bare INTENT_THRESHOLD, both aimed at the
            # same failure mode: a genuine support question the keyword scorer
            # under-counts (typos, vocabulary gaps) getting a canned redirect
            # instead of an answer.
            #
            #  1. A direct REPLY to the bot is a far stronger engagement signal
            #     than an @mention in passing — the user is answering us, often
            #     continuing a thread whose conversation TTL quietly expired.
            #     One topical hit is enough there.
            #  2. If we already redirected this user recently and they came
            #     back anyway, persisting IS the intent. Re-serving the same
            #     canned line is the dead end we hit in production: a redirect
            #     never opens a conversation, so Case 2 never picks the user
            #     up and every follow-up re-ran the same failing check.
            if not has_active_convo and clean:
                _, intent_score = detect_support_intent(clean)
                threshold = 1 if is_reply_to_bot else INTENT_THRESHOLD
                redirect_key = (message.channel.id, message.author.id)
                last_redirect = self._recent_redirects.get(redirect_key)
                persisted = (
                    last_redirect is not None
                    and datetime.now(timezone.utc) - last_redirect
                    < timedelta(minutes=REDIRECT_MEMORY_MINUTES)
                )
                if intent_score < threshold and not persisted:
                    self._handled_message_ids.add(message.id)
                    self._recent_redirects[redirect_key] = datetime.now(timezone.utc)
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
                    log.info(
                        f"Non-support mention from {message.author} "
                        f"(score={intent_score} < threshold={threshold}, "
                        f"reply_to_bot={bool(is_reply_to_bot)}), sent polite redirect: "
                        f"{clean[:80]}"
                    )
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

        # Busy mode — if an admin has it on, don't passively ping staff roles.
        # This only gates the proactive path; staff can still @mention the bot.
        if await self._is_busy_mode_suppressed(message):
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

    # ── Doc Overrides ──────────────────────────────────────────────────────

    async def _check_overrides(
        self,
        message: discord.Message,
        content: str,
        is_proactive: bool,
        is_first_response: bool,
    ) -> bool:
        """
        Check for an admin-configured doc override. If one matches, send the
        override message and return True so the caller skips the normal LLM
        flow entirely.

        Two override modes (per-override flag):
          - allow_ticket_offer=False (default): send the override message, done.
          - allow_ticket_offer=True:  send the override message AND append the
            standard "want a ticket?" prompt, then set pending escalation state
            so the existing yes/no handler picks it up on the user's next reply.

        Returns True if an override fired (caller stops). False if no match.
        """
        override = await find_matching_override(content)
        if not override:
            return False

        response = override["message"]

        # Mode B: still offer a ticket. Mirror NEEDS_TICKET shape so the
        # existing _check_pending_escalation handler works without changes.
        if override.get("allow_ticket_offer"):
            key = (message.channel.id, message.author.id)
            # Use the override name as the issue summary on the Plain ticket
            self._pending_escalations[key] = (
                f"Override triggered: {override.get('name', 'unknown')}"
            )
            response = (
                response
                + "\n\n💬 If you'd still like our team to take a look directly, "
                "reply **yes** and I'll open a ticket for you."
            )

        # Proactive greeting and first-response footer apply the same way they
        # would for a normal LLM answer — keeps the surface UX consistent.
        if is_proactive:
            greeting = f"Hey {message.author.mention}! 👋 I'm the Bankr support bot — \n\n"
            response = greeting + response

        if is_first_response:
            footer = "\n\n-# _If this solved your issue, reply_ `!close` _or say thanks to end this conversation._"
            response = response + footer

        # Log the user message + override response into conversation history so
        # follow-ups have context (e.g. "are you sure?" -> bot still knows what
        # was said). Skipping this would make the override feel like a dead end.
        self.conversations.add_message(
            message.channel.id, message.author.id, "user", content,
        )
        self.conversations.add_message(
            message.channel.id, message.author.id, "assistant", response,
        )

        # Discord 2000-char limit — chunk if needed (same logic as the main path).
        if len(response) <= 1900:
            await message.reply(response, mention_author=False)
        else:
            chunks = [response[i:i + 1900] for i in range(0, len(response), 1900)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await message.reply(chunk, mention_author=False)
                else:
                    await message.channel.send(chunk)

        # Fire-and-forget hit counter — we don't want a Redis hiccup to block
        # the user-facing path. The function itself is async but errors are
        # caught inside record_override_hit.
        asyncio.ensure_future(record_override_hit(override["id"]))

        # Stats row for the override hit. response_source='override' so the
        # dashboard counts these separately from doc answers; resolved_by_bot
        # is True (the user got a definitive answer) unless the override is
        # one that also offers a ticket, in which case it's an escalation path.
        override_log_kwargs = dict(
            source="discord",
            user_id=str(message.author.id),
            username=message.author.display_name,
            channel_id=str(message.channel.id),
            question=content,
            topic="override",
            response_source="override",
            response_text=response,   # the override message, as the user saw it
            session_id=self.conversations.get_session_id(
                message.channel.id, message.author.id
            ),
            resolved_by_bot=not override.get("allow_ticket_offer"),
            doc_gap=False,
            override_id=override["id"],
            llm_provider=None,   # overrides bypass the LLM entirely
        )
        if override.get("allow_ticket_offer"):
            # Mode B set a pending escalation — capture this row's id so a
            # later "yes" can backfill plain_thread_id onto it.
            key = (message.channel.id, message.author.id)
            row_id = await db.log_conversation(**override_log_kwargs)
            if row_id:
                self._pending_row_ids[key] = row_id
        else:
            asyncio.ensure_future(db.log_conversation(**override_log_kwargs))

        log.info(
            f"Override fired: id={override['id']} name={override.get('name')!r} "
            f"user={message.author} allow_ticket_offer={override.get('allow_ticket_offer')}"
        )
        return True

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

        # Check for admin-configured doc overrides (outage messages, etc).
        # If one fires, it handles the full reply itself and we stop here —
        # no docs query, no LLM call, no escalation tag parsing. The override
        # may have set pending escalation state if its allow_ticket_offer
        # flag is on; that gets picked up by _check_pending_escalation on the
        # user's next message.
        if await self._check_overrides(message, content, is_proactive, is_first_response):
            return

        self.conversations.add_message(message.channel.id, message.author.id, "user", content)

        key = (message.channel.id, message.author.id)
        t0 = time.monotonic()

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

        latency_ms = int((time.monotonic() - t0) * 1000)

        # Capture LLM-call metadata from the RAW response now, before any
        # stripping or concatenation turns it into a plain str and loses the
        # LLMResponse attributes.
        llm_provider = getattr(response, "provider", None)
        tokens_in    = getattr(response, "tokens_in", 0)
        tokens_out   = getattr(response, "tokens_out", 0)
        llm_ok       = getattr(response, "ok", True)

        # Doc-gap signal: the grounded system prompt instructs the model to
        # emit a specific sentence when the docs don't cover the question.
        # Detecting that exact phrase is how we flag a documentation hole.
        doc_gap = "couldn't find information about that in the Bankr documentation" in response

        # Pull the analytics topic tag out before the user ever sees the text.
        response, topic = self._extract_topic(response)

        # Track whether a ticket offer was pending BEFORE _maybe_escalate runs,
        # so we can tell if this message newly triggered an escalation.
        had_pending_before = key in self._pending_escalations

        # Check for escalation signal from the LLM
        response = await self._maybe_escalate(message, response, content)

        # Classify the outcome for the stats row.
        #   error      — the LLM call itself failed
        #   escalated  — this message newly raised an escalation offer
        #   docs       — a normal grounded answer
        pending_after = key in self._pending_escalations
        if not llm_ok:
            response_source = "error"
        elif pending_after and not had_pending_before:
            response_source = "escalated"
        else:
            response_source = "docs"
        # resolved_by_bot: the bot answered and did NOT need to escalate.
        resolved_by_bot = response_source == "docs"

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

        # ── Stats logging ────────────────────────────────────────────────────
        # db.log_conversation never raises and is a single fast insert.
        #
        # Normally fire-and-forget — we don't want to await a stats write on the
        # user-facing path. BUT if this message just triggered an escalation
        # offer (_maybe_escalate set _pending_escalations for this key), we need
        # this row's id so that — if the user later says "yes" and a ticket is
        # created — we can backfill plain_thread_id onto THIS row. In that one
        # case we await the insert to capture the id. Escalations are a small
        # fraction of messages, so the cost is negligible.
        log_kwargs = dict(
            source="discord",
            user_id=str(message.author.id),
            username=message.author.display_name,
            channel_id=str(message.channel.id),
            question=content,
            topic=topic,
            response_source=response_source,
            response_text=response,   # the decorated reply, as the user saw it
            session_id=self.conversations.get_session_id(
                message.channel.id, message.author.id
            ),
            resolved_by_bot=resolved_by_bot,
            doc_gap=doc_gap,
            llm_provider=llm_provider,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            error=(None if llm_ok else "LLM call failed"),
        )
        if key in self._pending_escalations:
            # Escalation offer was just made on this message — capture the row id.
            row_id = await db.log_conversation(**log_kwargs)
            if row_id:
                self._pending_row_ids[key] = row_id
        else:
            asyncio.ensure_future(db.log_conversation(**log_kwargs))

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
