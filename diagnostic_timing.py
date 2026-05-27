"""
diagnostic_timing.py — measure per-stage timing of a single fresh request

Sends 3 different fresh queries SEQUENTIALLY (no concurrency).
Shows where the time is going for each.

Run from your project folder:
    python diagnostic_timing.py
"""
import time
import requests
import sys

BOT_URL = "https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net"
API_KEY = "veelead-secure-9f83jsdf9832@"

# 3 fresh queries that should NOT hit cache
QUERIES = [
    "What is the exact onboarding process for new joiners at the company?",
    "Explain in detail the maternity leave policy and eligibility criteria",
    "What are the specific rules for claiming international travel expenses?",
]


def main():
    print("=" * 75)
    print("  SEQUENTIAL DIAGNOSTIC — 3 fresh queries, one at a time")
    print("=" * 75)
    print(f"  Target: {BOT_URL}")
    print()

    # First: warm-up to eliminate cold-start
    print("  Warming up bot...")
    try:
        r = requests.get(f"{BOT_URL}/health", timeout=15)
        print(f"  Warm-up: HTTP {r.status_code}\n")
    except Exception as e:
        print(f"  Warm-up failed: {e}\n")

    results = []
    for i, q in enumerate(QUERIES, 1):
        print(f"  [{i}/3] {q[:60]}...")
        started = time.time()
        try:
            r = requests.get(
                f"{BOT_URL}/search.json",
                params={"q": q},
                headers={"x-api-key": API_KEY},
                timeout=90,  # generous so we don't timeout mid-test
            )
            dur = time.time() - started
            data = r.json() if r.status_code == 200 else {}
            cached = data.get("cached")
            model = data.get("model_used", "?")
            chunks = len(data.get("chunks", []))
            req_id = r.headers.get("X-Request-ID", "no-id")

            print(f"        ✅ {dur:.1f}s | status={r.status_code} | cached={cached} | "
                  f"chunks={chunks} | model={model} | req-id={req_id}")
            results.append(dur)
        except requests.Timeout:
            dur = time.time() - started
            print(f"        ❌ TIMEOUT after {dur:.1f}s")
        except Exception as e:
            print(f"        ❌ ERROR: {type(e).__name__}: {e}")
        print()

    # Summary
    print("=" * 75)
    print("  SUMMARY")
    print("=" * 75)
    if results:
        avg = sum(results) / len(results)
        print(f"  Avg fresh response: {avg:.1f}s")
        if avg < 5:
            print(f"  ✅ Bot is FAST. The load test result was a fluke (likely concurrent burst).")
        elif avg < 10:
            print(f"  ⚠ Acceptable but slow. Probably some optimisation possible.")
        else:
            print(f"  ❌ TOO SLOW. There's a real bottleneck. Likely culprits:")
            print(f"     1. Cross-region: App Service & Azure OpenAI in different regions")
            print(f"     2. Cold start: bot was idle, first request paid the wake-up cost")
            print(f"     3. Big prompt: 7000+ char prompt makes every LLM call slow")
            print()
            print(f"     Check first query — if it was way slower than 2 and 3, it's cold start.")
            print(f"     Otherwise probably cross-region or prompt size.")


if __name__ == "__main__":
    main()
