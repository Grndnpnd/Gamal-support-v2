"""
The pieces that need no database: the signature on calls between the bot and the webhook process, and alerts.
    python tests/test_units.py
"""
import asyncio
import os
import sys

os.environ["DISCORD_TOKEN"] = "unit-test-token"
os.environ.pop("INTERNAL_SECRET", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import internal_auth as ia     # noqa: E402

failed = 0


def check(name, ok, detail=""):
    global failed
    print(("ok    " if ok else "FAIL  ") + name + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        failed += 1


NOW = 1_800_000_000
sig = ia.sign("cancel-deletion", 123456789, now=NOW)
check("a signed call verifies", ia.verify(sig, "cancel-deletion", 123456789, now=NOW + 5))
check("a call with no signature does not", not ia.verify(None, "cancel-deletion", 123456789, now=NOW) and not ia.verify("", "cancel-deletion", 123456789, now=NOW))
check("a signature for one thread is no good for another", not ia.verify(sig, "cancel-deletion", 987654321, now=NOW))
check("nor for another action", not ia.verify(sig, "something-else", 123456789, now=NOW))
check("it stops working after two minutes", ia.verify(sig, "cancel-deletion", 123456789, now=NOW + 119) and not ia.verify(sig, "cancel-deletion", 123456789, now=NOW + 121))
check("a made-up signature does not verify", not ia.verify(f"{NOW}.{'0' * 64}", "cancel-deletion", 123456789, now=NOW) and not ia.verify("garbage", "cancel-deletion", 123456789, now=NOW))
os.environ["DISCORD_TOKEN"] = "a-different-token"
check("a signature made with another secret does not verify", not ia.verify(sig, "cancel-deletion", 123456789, now=NOW))
os.environ["INTERNAL_SECRET"] = "explicit"
s2 = ia.sign("cancel-deletion", 1, now=NOW)
os.environ["DISCORD_TOKEN"] = "rotated-token"
check("INTERNAL_SECRET, when set, is the key (a Discord token change does not break it)", ia.verify(s2, "cancel-deletion", 1, now=NOW))
os.environ.pop("INTERNAL_SECRET"); os.environ.pop("DISCORD_TOKEN")
check("with no secret at all nothing signs and nothing verifies", ia.sign("cancel-deletion", 1) == "" and not ia.verify(s2, "cancel-deletion", 1, now=NOW))

# ── alerts ──
os.environ["ALERT_WEBHOOK_URL"] = ""
import importlib               # noqa: E402
import alerts                  # noqa: E402
importlib.reload(alerts)
check("with no webhook configured an alert is a no-op", alerts.alert("x") is False)
alerts.ALERT_WEBHOOK_URL = "https://discord.example/api/webhooks/1/abc"
sent = []


async def fake_send(text):
    sent.append(text)

alerts._send = fake_send


async def run():
    check("an alert is sent", alerts.alert("one") is True)
    check("the same kind inside its period is held back", alerts.alert("refused A", key="k", every=600) is True and alerts.alert("refused B", key="k", every=600) is False and alerts.alert("refused C", key="k", every=600) is False)
    alerts._last["k"] -= 601
    check("the next one after the period says how many were held", alerts.alert("refused D", key="k", every=600) is True)
    await asyncio.sleep(0.05)
    check("what went out", sent == ["one", "refused A", "refused D (and 2 more like it since the last alert)"], str(sent))
    p = alerts._payload("hello @everyone")
    check("a Discord alert cannot ping anyone", p.get("allowed_mentions") == {"parse": []} and p["content"].endswith("hello @everyone"))
    alerts.ALERT_WEBHOOK_URL = "https://hooks.slack.com/services/x"
    check("a Slack webhook gets Slack's shape", "text" in alerts._payload("hi") and "content" not in alerts._payload("hi"))

asyncio.run(run())
print("\n" + (f"{failed} FAILED" if failed else "all passed"))
sys.exit(1 if failed else 0)
