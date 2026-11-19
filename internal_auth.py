"""
internal_auth.py
----------------
Proof that a call between this system's own processes came from one of them.

The bot asks the webhook process to cancel a thread's deletion (POST /cancel-deletion). That route used to take no
credential, and the webhook process has a public address, so anyone who knew a thread id could call it. Now the
caller signs each request and the receiver checks the signature:

    X-Internal-Signature: <unix time>.<hex HMAC-SHA256 of "<unix time>.<action>.<subject>">

The key is derived from a secret both processes already hold (INTERNAL_SECRET if it is set, otherwise the Discord
bot token), so nothing new has to be configured. A signature is good for one action on one subject, for two
minutes either side of its time. With no secret at all, nothing verifies.
"""
import hashlib
import hmac
import os
import time

HEADER = "X-Internal-Signature"
WINDOW_SECONDS = 120


def _key() -> bytes | None:
    secret = os.getenv("INTERNAL_SECRET", "") or os.getenv("DISCORD_TOKEN", "")
    if not secret:
        return None
    return hashlib.sha256(f"internal-call:{secret}".encode()).digest()


def sign(action: str, subject: str | int, now: float | None = None) -> str:
    """The header value for one call. Empty when there is no secret to sign with."""
    key = _key()
    if key is None:
        return ""
    ts = int(now if now is not None else time.time())
    mac = hmac.new(key, f"{ts}.{action}.{subject}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{mac}"


def verify(header: str | None, action: str, subject: str | int, now: float | None = None) -> bool:
    key = _key()
    if key is None or not header or "." not in header:
        return False
    ts_text, _, mac = header.partition(".")
    if not ts_text.isdigit():
        return False
    ts = int(ts_text)
    if abs((now if now is not None else time.time()) - ts) > WINDOW_SECONDS:
        return False
    want = hmac.new(key, f"{ts}.{action}.{subject}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, mac)
