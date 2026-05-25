"""
test_spelling.py — verify the new query rewrite feature.

Fires 10 deliberately misspelled queries at the bot and prints:
  - Original query
  - Corrected query (if any)
  - First source found
  - Confidence

Run while the bot is running locally:
    python test_spelling.py

Or against Azure (just change API_BASE):
    API_BASE = "https://veelead-helpdesk-bot.azurewebsites.net"
    python test_spelling.py
"""
import sys
import urllib.parse
import urllib.request
import json

API_BASE = "http://localhost:8000"
API_KEY = "veelead-secure-9f83jsdf9832@"

# Queries with deliberate typos, bad grammar, or mangled domain terms
TEST_QUERIES = [
    # Typos
    "how do i aply for leve",                          # → apply for leave
    "how to claim reimbersement",                       # → claim reimbursement
    "what is the playslip strucutre",                   # → payslip structure
    "what is mater nity leave duraton",                 # → maternity leave duration
    "how to setup my offical emial on phone",           # → official email on phone

    # Bad grammar
    "leve how take i can",                              # → how can I take leave
    "my latop blue screan what i do",                   # → blue screen on my laptop, what should I do

    # Domain-term variants
    "what r the rules for VPN connectn",                # → rules for VPN connection
    "how mch is per dim for mumbai",                    # → per diem for Mumbai

    # Correct queries (should pass through unchanged)
    "What are the password requirements?",
    "How do I claim a reimbursement?",
]


def fetch(query: str) -> dict:
    """Hit the bot and return the parsed JSON."""
    encoded = urllib.parse.quote(query)
    url = f"{API_BASE}/search.json?q={encoded}"
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 80)
    print(f"  Testing query rewrite at {API_BASE}")
    print("=" * 80)
    print()

    corrected_count = 0
    unchanged_count = 0

    for i, q in enumerate(TEST_QUERIES, 1):
        print(f"[{i:>2}] User typed: {q!r}")
        try:
            data = fetch(q)
        except Exception as e:
            print(f"     ❌ Request failed: {e}")
            print()
            continue

        corrected = data.get("corrected_query")
        if corrected:
            print(f"     ✏️  Corrected to:  {corrected!r}")
            corrected_count += 1
        else:
            print(f"     ✓  No change needed")
            unchanged_count += 1

        # Show top source and confidence
        chunks = data.get("chunks") or []
        if chunks:
            top = chunks[0]
            print(f"     📄 Top source: {top.get('filename')} "
                  f"(score={top.get('score'):.3f}, cat={top.get('category')})")
        else:
            print(f"     📄 No chunks returned")

        conf = data.get("confidence", 0)
        cached = data.get("cached", False)
        model = data.get("model_used", "?")
        print(f"     📊 Confidence: {conf:.2f} | Model: {model} | Cached: {cached}")
        print()

    print("=" * 80)
    print(f"  Summary")
    print("=" * 80)
    print(f"  Total queries:  {len(TEST_QUERIES)}")
    print(f"  Corrected:      {corrected_count}")
    print(f"  Unchanged:      {unchanged_count}")
    print()
    print("  ✅ Expected: ~9 corrections out of 11 queries")
    print("  ✅ The 2 'correct' queries should pass through unchanged")


if __name__ == "__main__":
    main()
