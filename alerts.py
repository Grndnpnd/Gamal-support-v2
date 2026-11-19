"""
alerts.py
---------
Tells a person when something that matters happens: a refused sign-in, a first sign-in by an account, an override
made or changed (this is what the bot says to users), a setting changed, an article published, a process started.

One setting:
  ALERT_WEBHOOK_URL   a Discord channel webhook (or a Slack incoming webhook) in a channel only staff can read.
                      Unset = no alerts are sent, and nothing else changes.

An alert never gets in the way of what caused it: it is sent in the background, it gives up after five seconds, and
any failure is logged and swallowed. The same kind of alert can be limited to one per period (`every`), so a burst
of refused sign-ins is one message with a count, not a flood. Text from a person (an override's message) is sent
with mentions switched off, so it cannot ping a channel.
"""
import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)

ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
SERVICE = os.getenv("ALERT_SERVICE_NAME", "support bot")

_last: dict[str, float] = {}
_held: dict[str, int] = {}


def enabled() -> bool:
    return bool(ALERT_WEBHOOK_URL)


def _due(key: str, every: float, now: float) -> tuple[bool, int]:
    """Whether an alert of this kind may go now, and how many were held back since the last one that went."""
    if every <= 0:
        return True, 0
    last = _last.get(key, 0.0)
    if now - last < every:
        _held[key] = _held.get(key, 0) + 1
        return False, 0
    _last[key] = now
    return True, _held.pop(key, 0)


def _payload(text: str) -> dict:
    text = f"[{SERVICE}] {text}"
    if "hooks.slack.com" in ALERT_WEBHOOK_URL:
        return {"text": text[:3000]}
    return {"content": text[:1900], "allowed_mentions": {"parse": []}}


async def _send(text: str) -> None:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(ALERT_WEBHOOK_URL, json=_payload(text), timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status >= 300:
                    log.warning(f"Alert not delivered: HTTP {resp.status}")
    except Exception as e:
        log.warning(f"Alert not delivered: {type(e).__name__}")


def alert(text: str, key: str = "", every: float = 0) -> bool:
    """Send an alert in the background. Returns False when it was not sent (alerts are off, or this kind was sent
    within the last `every` seconds). Safe to call from anywhere a loop is running; never raises."""
    if not ALERT_WEBHOOK_URL:
        return False
    try:
        go, held = _due(key or text, every, time.monotonic())
        if not go:
            return False
        if held:
            text = f"{text} (and {held} more like it since the last alert)"
        asyncio.get_running_loop().create_task(_send(text))
        return True
    except Exception as e:
        log.warning(f"Alert not sent: {type(e).__name__}")
        return False
