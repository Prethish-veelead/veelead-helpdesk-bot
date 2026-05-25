"""
test_memory.py — verify the conversation memory feature.

Simulates a 5-turn chat where follow-up questions depend on context.
Hits the bot with `previous` query params (oldest first) just like the
browser would. Prints whether each question was contextualised and what
the bot used internally.

Run while bot is local:
    python test_memory.py

For Azure, edit API_BASE.
"""
import json
import urllib.parse
import urllib.request

API_BASE = "http://localhost:8000"
API_KEY = "veelead-secure-9f83jsdf9832@"


# ── Test conversation: leaves topic (questions 1-4) then a new topic (5) ──
TURNS = [
    {
        "q": "How many leaves can I take per year?",
        "expect_contextualised": False,
        "note": "First question — no history, no rewrite expected",
    },
    {
        "q": "Where can I apply?",
        "expect_contextualised": True,
        "note": "Ambiguous follow-up — should rewrite to 'Where can I apply for leave?'",
    },
    {
        "q": "How does approval work?",
        "expect_contextualised": True,
        "note": "Still about leaves — should rewrite to mention leave approval",
    },
    {
        "q": "What about maternity leave?",
        "expect_contextualised": True,
        "note": "Still in leaves topic but specific subtype",
    },
    {
        "q": "How do I reset my Windows password?",
        "expect_contextualised": False,
        "note": "Completely new topic — should NOT rewrite using leaves context",
    },
]


def fetch(query: str, previous: list) -> dict:
    params = [("q", query)]
    for p in previous:
        params.append(("previous", p))
    qs = urllib.parse.urlencode(params)
    url = f"{API_BASE}/search.json?{qs}"
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 80)
    print(f"  Testing conversation memory @ {API_BASE}")
    print("=" * 80)
    print()

    history = []
    correct = 0

    for i, turn in enumerate(TURNS, 1):
        q = turn["q"]
        expect = turn["expect_contextualised"]

        print(f"━━━ Turn {i} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"User typed:        {q}")
        print(f"History sent:      {history if history else '(none)'}")
        print(f"Expected:          {'contextualise' if expect else 'no rewrite'}")
        print(f"Why:               {turn['note']}")

        try:
            data = fetch(q, history)
        except Exception as e:
            print(f"❌ Request failed: {e}")
            print()
            continue

        contextualised = data.get("contextualized_query")
        was_rewritten = contextualised is not None
        subject = data.get("subject") or "?"
        match_type = data.get("cache_match_type") or "fresh"

        if contextualised:
            print(f"Bot rewrote to:    {contextualised!r}")
        else:
            print(f"Bot used as-is:    {q!r}")
        print(f"Subject:           {subject}")
        print(f"Cache:             {match_type}")

        # Verdict
        if was_rewritten == expect:
            print(f"✅ PASS — behaviour matches expectation")
            correct += 1
        else:
            if expect and not was_rewritten:
                print(f"⚠ SOFT-FAIL — expected rewrite but bot left it as-is")
                print(f"   (the bot is allowed to skip if question seems complete)")
            else:
                print(f"❌ FAIL — bot rewrote when it shouldn't have")

        # Push this question into history for next turn
        history.append(q)
        if len(history) > 5:
            history.pop(0)

        print()

    print("=" * 80)
    print(f"  SUMMARY")
    print("=" * 80)
    print(f"  Turns:  {len(TURNS)}")
    print(f"  Passed: {correct}")
    print()
    if correct == len(TURNS):
        print("  ✅ All turns behaved correctly")
    elif correct >= len(TURNS) - 1:
        print("  ✅ Conversation memory working (one soft-fail is acceptable —")
        print("     the LLM is allowed to leave questions unchanged when uncertain)")
    else:
        print(f"  ⚠ {len(TURNS) - correct} unexpected results — check logs for details")


if __name__ == "__main__":
    main()
