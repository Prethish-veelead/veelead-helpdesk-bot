"""
prewarm.py — Pre-cache demo questions so they respond instantly on stage.

Run this 5 minutes BEFORE your demo. It sends every demo question to the bot
once, so all answers end up in the cache. During the actual demo, each
question hits cache and responds in ~400ms instead of 12-16 seconds.

Usage:
    python prewarm.py

To use a different bot URL:
    BOT_URL=https://your-bot.azurewebsites.net python prewarm.py
"""

import os
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

# ── CONFIGURE ──────────────────────────────────────────────
BOT_URL = os.getenv(
    "BOT_URL",
    "https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net"
)
API_KEY = os.getenv("API_KEY", "veelead-secure-9f83jsdf9832@")

# Edit this list to match your EXACT demo questions
DEMO_QUESTIONS = [
    "Hi",
    "How many leaves can I take?",
    "How do I apply for casual leave?",
    "What is the salary structure?",
    "How do I claim reimbursement?",
    "What should I do if my laptop crashes?",
    "What is the WFH policy?",
    "What are the password requirements?",
]


def warm_one(question: str, num: int, total: int) -> tuple[bool, float]:
    """Send one warm-up request. Returns (success, duration_seconds)."""
    url = f"{BOT_URL}/search.json?q={quote(question)}"
    req = Request(url, headers={"x-api-key": API_KEY})
    started = time.time()
    try:
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
            duration = time.time() - started
            status = resp.status
            if status == 200:
                print(f"  [{num}/{total}] ✅ {duration:5.1f}s  {question[:60]}")
                return (True, duration)
            else:
                print(f"  [{num}/{total}] ⚠️  {status}    {question[:60]}")
                return (False, duration)
    except Exception as e:
        duration = time.time() - started
        print(f"  [{num}/{total}] ❌ {duration:5.1f}s  {question[:60]} — {type(e).__name__}")
        return (False, duration)


def verify_cache_hit(question: str) -> bool:
    """Second request — should be cached and very fast (<2s)."""
    import json
    url = f"{BOT_URL}/search.json?q={quote(question)}"
    req = Request(url, headers={"x-api-key": API_KEY})
    started = time.time()
    try:
        with urlopen(req, timeout=10) as resp:
            duration = time.time() - started
            data = json.loads(resp.read().decode("utf-8"))
            cached = data.get("cached", False)
            if cached:
                print(f"     → verified cached ({duration:.2f}s)")
                return True
            else:
                print(f"     → ⚠️  NOT cached after warming ({duration:.2f}s)")
                return False
    except Exception as e:
        print(f"     → verify failed: {e}")
        return False


def main():
    print("=" * 70)
    print("  PRE-WARMING BOT FOR DEMO")
    print("=" * 70)
    print(f"  Target: {BOT_URL}")
    print(f"  Questions: {len(DEMO_QUESTIONS)}")
    print()

    # Health check first
    print("Step 1: Health check...")
    try:
        req = Request(f"{BOT_URL}/health", headers={"x-api-key": API_KEY})
        with urlopen(req, timeout=20) as resp:
            if resp.status == 200:
                print("  ✅ Bot is reachable")
            else:
                print(f"  ⚠️  Health check returned {resp.status}")
    except Exception as e:
        print(f"  ❌ Cannot reach bot: {e}")
        print("  ABORTING. Fix the bot before pre-warming.")
        sys.exit(1)

    # First pass — populate cache
    print()
    print("Step 2: First pass — populate cache (this is slow, ~1-3 min)")
    print("-" * 70)
    started = time.time()
    successes = 0
    total_duration = 0.0
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        ok, dur = warm_one(q, i, len(DEMO_QUESTIONS))
        if ok:
            successes += 1
        total_duration += dur

    elapsed = time.time() - started
    print()
    print(f"  First pass done: {successes}/{len(DEMO_QUESTIONS)} successful, "
          f"{elapsed:.1f}s total")

    # Second pass — verify cache is warm
    print()
    print("Step 3: Verify cache (should be MUCH faster now)")
    print("-" * 70)
    cache_hits = 0
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        if verify_cache_hit(q):
            cache_hits += 1

    # Summary
    print()
    print("=" * 70)
    print("  PRE-WARM RESULTS")
    print("=" * 70)
    print(f"  Questions warmed:     {successes}/{len(DEMO_QUESTIONS)}")
    print(f"  Cache verified:       {cache_hits}/{len(DEMO_QUESTIONS)}")
    print(f"  Total time taken:     {elapsed:.0f}s")
    print()
    if cache_hits == len(DEMO_QUESTIONS):
        print("  ✅ ALL DEMO QUESTIONS ARE CACHED. Bot is ready for demo!")
        print()
        print("  Each demo question will now respond in ~400ms-2s.")
        print("  ⏰ This warm state lasts ~7 days (cache TTL).")
    elif cache_hits >= len(DEMO_QUESTIONS) * 0.75:
        print("  ⚠️  Most questions cached. Demo should go well.")
    else:
        print(f"  ❌ Only {cache_hits} questions cached. Check bot logs for errors.")


if __name__ == "__main__":
    main()
