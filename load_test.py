"""
load_test.py — measure your hosted bot's capacity

Sends concurrent requests to your hosted Veelead Helpdesk Bot and reports:
  - Requests per second (throughput)
  - Latency percentiles (median, p95, p99)
  - Error rate
  - Estimated cost in INR

This is a STANDALONE test script. It does NOT modify your bot code.
Run it from your laptop against the deployed Azure URL.

═══════════════════════════════════════════════════════════
QUICK START
═══════════════════════════════════════════════════════════

  # Smoke test (20 requests, 2 concurrent — costs ~₹1)
  python load_test.py --max-requests 20 --workers 2

  # Medium test (200 requests, 10 concurrent — costs ~₹10)
  python load_test.py --max-requests 200 --workers 10

  # Stress test (1000 requests, ramp from 5 to 50 workers — costs ~₹50)
  python load_test.py --max-requests 1000 --workers 50 --ramp

  # Dry run (no requests sent, just shows config)
  python load_test.py --dry-run

═══════════════════════════════════════════════════════════
WHAT IT MEASURES
═══════════════════════════════════════════════════════════

  Requests per second  — how many queries your bot serves in 1 second
  Median (p50)         — half of users see this latency or faster
  p95                  — 95% of users see this latency or faster
  p99                  — worst 1% of users see this
  Error rate           — % of requests that failed (timeout, 500, etc.)
  Cache hit ratio      — what % were served from cache
  Estimated cost       — how much you spent on OpenAI for this test

═══════════════════════════════════════════════════════════
DEPENDENCIES
═══════════════════════════════════════════════════════════

  pip install requests
"""
import argparse
import json
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Install requests first: pip install requests")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════
# CONFIG — edit these for your bot
# ═══════════════════════════════════════════════════════════

DEFAULT_BOT_URL = "https://veelead-helpdesk-bot-bagsakfcf2aeh7ag.southeastasia-01.azurewebsites.net"
DEFAULT_API_KEY = "veelead-secure-9f83jsdf9832@"

# A good mix of questions covering different categories. Some will get
# cached after the first hit, so subsequent requests are fast — this
# matches realistic production usage.
TEST_QUESTIONS = [
    # Common queries (likely cached after warm-up)
    "How many leaves can I take per year?",
    "How do I apply for casual leave?",
    "What is my salary structure?",
    "How do I reset my password?",
    "What should I do if my laptop crashes?",
    # Slightly different phrasing (tests semantic cache)
    "What is the leave policy?",
    "Tell me about the salary breakdown",
    "How can I claim reimbursement?",
    # Variety (less likely to be cached)
    "What is the VPN setup process?",
    "How do I get a salary certificate?",
]

# Rough cost per request (INR). Tuned for gpt-4o-mini + embeddings.
# Used only for the estimated-cost report — not a real meter.
ESTIMATED_COST_PER_FRESH_REQUEST = 0.05   # ~5 paise
ESTIMATED_COST_PER_CACHED_REQUEST = 0.005  # tiny — just an embedding call


# ═══════════════════════════════════════════════════════════
# REQUEST EXECUTOR
# ═══════════════════════════════════════════════════════════

class RequestResult:
    __slots__ = ("question", "status_code", "duration_ms", "cached", "error", "request_id")

    def __init__(self, question, status_code, duration_ms, cached=False, error=None, request_id=None):
        self.question = question
        self.status_code = status_code
        self.duration_ms = duration_ms
        self.cached = cached
        self.error = error
        self.request_id = request_id


def send_one_request(bot_url, api_key, question, timeout=30):
    """Send a single search request and time it. Returns a RequestResult."""
    url = f"{bot_url}/search.json?q={quote(question)}"
    headers = {"x-api-key": api_key, "accept": "application/json"}
    started = time.time()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        duration_ms = int((time.time() - started) * 1000)

        cached = False
        request_id = resp.headers.get("X-Request-ID")
        if resp.status_code == 200:
            try:
                data = resp.json()
                cached = bool(data.get("cached"))
            except Exception:
                pass

        return RequestResult(
            question=question,
            status_code=resp.status_code,
            duration_ms=duration_ms,
            cached=cached,
            request_id=request_id,
        )
    except requests.Timeout:
        duration_ms = int((time.time() - started) * 1000)
        return RequestResult(question, 0, duration_ms, error="timeout")
    except Exception as e:
        duration_ms = int((time.time() - started) * 1000)
        return RequestResult(question, 0, duration_ms, error=type(e).__name__)


# ═══════════════════════════════════════════════════════════
# TEST RUNNER
# ═══════════════════════════════════════════════════════════

def run_load_test(bot_url, api_key, max_requests, workers, ramp=False, timeout=30):
    """Run the load test and collect results."""
    print()
    print("=" * 70)
    print("  LOAD TEST IN PROGRESS")
    print("=" * 70)
    print(f"  Target:     {bot_url}")
    print(f"  Requests:   {max_requests}")
    print(f"  Workers:    {workers}{' (ramping)' if ramp else ' (constant)'}")
    print(f"  Timeout:    {timeout}s per request")
    print("=" * 70)
    print()

    results = []
    started_at = time.time()
    progress_lock = threading.Lock()
    completed = [0]  # using a list so it's mutable in closures

    def show_progress():
        with progress_lock:
            completed[0] += 1
            n = completed[0]
            if n % 10 == 0 or n == max_requests:
                pct = n * 100 // max_requests
                elapsed = time.time() - started_at
                rps = n / elapsed if elapsed > 0 else 0
                print(f"  Progress: {n}/{max_requests} ({pct}%) | {rps:.1f} req/s", flush=True)

    def submit_one(idx):
        # Pick question — round-robin through TEST_QUESTIONS
        question = TEST_QUESTIONS[idx % len(TEST_QUESTIONS)]
        result = send_one_request(bot_url, api_key, question, timeout=timeout)
        show_progress()
        return result

    if ramp:
        # Ramp pattern: 1 → 5 → workers, 1/3 of requests at each level
        stages = [
            max(1, workers // 10),    # warm-up
            max(2, workers // 4),     # light
            max(4, workers // 2),     # medium
            workers,                   # full
        ]
        per_stage = max_requests // len(stages)
        print(f"  Ramp stages: {stages} workers, {per_stage} requests each\n")

        idx = 0
        for stage_workers in stages:
            stage_start = time.time()
            with ThreadPoolExecutor(max_workers=stage_workers) as pool:
                futures = [pool.submit(submit_one, idx + i) for i in range(per_stage)]
                for f in as_completed(futures):
                    results.append(f.result())
            idx += per_stage
            stage_duration = time.time() - stage_start
            print(f"  ── Stage with {stage_workers} workers done in {stage_duration:.1f}s ──\n", flush=True)
    else:
        # Constant workers
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(submit_one, i) for i in range(max_requests)]
            for f in as_completed(futures):
                results.append(f.result())

    duration = time.time() - started_at
    return results, duration


# ═══════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════

def report(results, total_duration):
    """Print a human-readable summary of the test results."""
    n_total = len(results)
    successes = [r for r in results if r.status_code == 200]
    n_ok = len(successes)
    failures = [r for r in results if r.status_code != 200]

    durations = [r.duration_ms for r in successes]
    cached_hits = [r for r in successes if r.cached]
    fresh_hits = [r for r in successes if not r.cached]

    print()
    print("=" * 70)
    print("  LOAD TEST RESULTS")
    print("=" * 70)

    # Throughput
    rps = n_total / total_duration if total_duration > 0 else 0
    print(f"\n  Total time:        {total_duration:.1f}s")
    print(f"  Total requests:    {n_total}")
    print(f"  Successful:        {n_ok} ({n_ok * 100 // max(1, n_total)}%)")
    print(f"  Failed:            {len(failures)} ({len(failures) * 100 // max(1, n_total)}%)")
    print(f"  Requests/second:   {rps:.2f}")

    # Latency
    if durations:
        durations_sorted = sorted(durations)
        p50 = durations_sorted[len(durations_sorted) // 2]
        p95 = durations_sorted[int(len(durations_sorted) * 0.95)]
        p99 = durations_sorted[int(len(durations_sorted) * 0.99)]
        avg = statistics.mean(durations_sorted)

        print(f"\n  LATENCY (successful requests only)")
        print(f"  ────────────────────────────────────")
        print(f"  Average:           {avg:.0f}ms")
        print(f"  Median (p50):      {p50}ms")
        print(f"  p95:               {p95}ms")
        print(f"  p99:               {p99}ms")
        print(f"  Min:               {durations_sorted[0]}ms")
        print(f"  Max:               {durations_sorted[-1]}ms")

    # Cache
    if successes:
        cache_pct = len(cached_hits) * 100 // len(successes)
        print(f"\n  CACHE EFFICIENCY")
        print(f"  ────────────────────────────────────")
        print(f"  Cache hits:        {len(cached_hits)} ({cache_pct}%)")
        print(f"  Fresh generations: {len(fresh_hits)} ({100 - cache_pct}%)")

        if cached_hits and fresh_hits:
            cached_avg = statistics.mean(r.duration_ms for r in cached_hits)
            fresh_avg = statistics.mean(r.duration_ms for r in fresh_hits)
            print(f"  Cached avg:        {cached_avg:.0f}ms")
            print(f"  Fresh avg:         {fresh_avg:.0f}ms")
            speedup = fresh_avg / max(1, cached_avg)
            print(f"  Cache speedup:    {speedup:.1f}x")

    # Failures breakdown
    if failures:
        print(f"\n  FAILURES")
        print(f"  ────────────────────────────────────")
        from collections import Counter
        by_code = Counter(r.status_code for r in failures)
        for code, count in by_code.most_common():
            label = "Network error" if code == 0 else f"HTTP {code}"
            print(f"  {label}: {count}")
        by_error = Counter(r.error for r in failures if r.error)
        for error, count in by_error.most_common(3):
            print(f"  {error}: {count}")

    # Estimated cost
    estimated_cost = (
        len(fresh_hits) * ESTIMATED_COST_PER_FRESH_REQUEST +
        len(cached_hits) * ESTIMATED_COST_PER_CACHED_REQUEST
    )
    print(f"\n  ESTIMATED COST")
    print(f"  ────────────────────────────────────")
    print(f"  Total:             ~₹{estimated_cost:.2f}")
    print(f"  (Fresh: {len(fresh_hits)} × ₹{ESTIMATED_COST_PER_FRESH_REQUEST}, "
          f"Cached: {len(cached_hits)} × ₹{ESTIMATED_COST_PER_CACHED_REQUEST})")

    # Verdict
    print(f"\n  VERDICT")
    print(f"  ────────────────────────────────────")
    error_rate = len(failures) * 100 // max(1, n_total)
    if error_rate == 0 and durations and statistics.median(durations) < 3000:
        print(f"  ✅ Bot is healthy and responsive.")
    elif error_rate < 5:
        print(f"  ⚠️  Bot is mostly OK ({error_rate}% errors) — may struggle "
              f"at higher load.")
    else:
        print(f"  ❌ High error rate ({error_rate}%) — bot is overloaded. "
              f"Consider scaling up.")
    if durations:
        p95_val = sorted(durations)[int(len(durations) * 0.95)]
        if p95_val > 6000:
            print(f"  ⚠️  p95 latency {p95_val}ms is high. Users will feel this.")

    print()
    print("=" * 70)


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Load-test the Veelead Helpdesk Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bot-url", default=DEFAULT_BOT_URL,
                        help="Bot base URL (default: %(default)s)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY,
                        help="API key (default: from script)")
    parser.add_argument("--max-requests", type=int, default=100,
                        help="Total requests to send (default: 100)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Concurrent workers (default: 5)")
    parser.add_argument("--ramp", action="store_true",
                        help="Ramp workers gradually instead of constant")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Per-request timeout in seconds (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print config without running")
    args = parser.parse_args()

    estimated_cost = args.max_requests * ESTIMATED_COST_PER_FRESH_REQUEST
    estimated_cached_cost = args.max_requests * ESTIMATED_COST_PER_CACHED_REQUEST
    cost_range = f"₹{estimated_cached_cost:.0f}-{estimated_cost:.0f}"

    print()
    print("Configuration:")
    print(f"  Bot URL:         {args.bot_url}")
    print(f"  Max requests:    {args.max_requests}")
    print(f"  Workers:         {args.workers}{' (ramping)' if args.ramp else ''}")
    print(f"  Timeout:         {args.timeout}s")
    print(f"  Questions pool:  {len(TEST_QUESTIONS)} unique")
    print(f"  Estimated cost:  {cost_range} INR (depending on cache hit rate)")

    if args.dry_run:
        print("\n  --dry-run: no requests sent.")
        return

    # Final confirmation if budget could exceed ₹50
    if estimated_cost > 50:
        print(f"\n⚠️  This test could cost up to ₹{estimated_cost:.0f} in OpenAI tokens.")
        ans = input("Continue? Type 'yes' to proceed: ").strip().lower()
        if ans != "yes":
            print("Aborted.")
            return

    # Pre-flight: can we reach the bot?
    print("\nPre-flight: checking bot is reachable...")
    try:
        r = requests.get(f"{args.bot_url}/health", timeout=10)
        if r.status_code == 200:
            print("  ✅ Bot is reachable")
        else:
            print(f"  ⚠️  Bot returned {r.status_code} on /health — proceeding anyway")
    except Exception as e:
        print(f"  ❌ Cannot reach bot: {e}")
        print("  Aborting.")
        return

    # Run the test
    results, duration = run_load_test(
        bot_url=args.bot_url,
        api_key=args.api_key,
        max_requests=args.max_requests,
        workers=args.workers,
        ramp=args.ramp,
        timeout=args.timeout,
    )

    # Report
    report(results, duration)


if __name__ == "__main__":
    main()
