"""
test_subject_and_semantic_cache.py

Two-phase test for the new features:

Phase 1: Hit each of these queries ONCE — bot will generate fresh answers
         with subject+description. Cache will store them with embeddings.

Phase 2: Hit RELATED queries that are semantically similar but NOT exact.
         Expect cache_match_type="semantic" with similarity ≥ 0.92.

Phase 3: Hit DIFFERENT queries that should NOT match cache.
         Expect cache_match_type=None (fresh generation).

Run while the bot is running locally:
    python test_subject_and_semantic_cache.py

Against Azure: edit API_BASE.
"""
import urllib.parse
import urllib.request
import json
import time

API_BASE = "http://localhost:8000"
API_KEY = "veelead-secure-9f83jsdf9832@"


# ── Phase 1: prime the cache ─────────────────────────────────────
SEED_QUERIES = [
    "What is the salary structure at Veelead?",
    "How do I apply for casual leave?",
    "What should I do if I see a blue screen error?",
    "What are the per diem rates for domestic travel?",
]

# ── Phase 2: semantically similar variants (should HIT semantic cache) ──
RELATED_QUERIES = [
    ("Tell me about salary structure",                 "What is the salary structure at Veelead?"),
    ("How can I apply for casual leave",               "How do I apply for casual leave?"),
    ("Steps to fix a blue screen error",               "What should I do if I see a blue screen error?"),
    ("Domestic per diem amounts",                       "What are the per diem rates for domestic travel?"),
]

# ── Phase 3: different queries that should NOT match ──
DIFFERENT_QUERIES = [
    "How do I reset my password?",
    "What is the WFH policy?",
    "What are the maternity leave rules?",
]


def fetch(query: str) -> dict:
    encoded = urllib.parse.quote(query)
    url = f"{API_BASE}/search.json?q={encoded}"
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def print_response(q: str, data: dict, phase: str):
    cached = data.get("cached", False)
    match_type = data.get("cache_match_type")
    sim = data.get("cache_similarity")
    subject = data.get("subject", "")
    description = data.get("description", "")[:80]

    if cached:
        emoji = "💰"
        if match_type == "semantic":
            tag = f"CACHE-SEMANTIC sim={sim:.3f}"
        else:
            tag = "CACHE-EXACT"
    else:
        emoji = "🆕"
        tag = "FRESH"

    print(f"  {emoji} [{tag}]")
    print(f"     Q:     {q}")
    print(f"     Subj:  {subject}")
    print(f"     Desc:  {description}{'...' if len(data.get('description', '')) > 80 else ''}")
    print()


def main():
    print("=" * 80)
    print(f"  Testing subject/description + semantic cache @ {API_BASE}")
    print("=" * 80)
    print()

    # ─── Phase 1: prime the cache ───
    print("━" * 80)
    print("PHASE 1 — Seed the cache (fresh generation for each)")
    print("━" * 80)
    for q in SEED_QUERIES:
        try:
            data = fetch(q)
            print_response(q, data, "seed")
        except Exception as e:
            print(f"  ❌ Failed: {e}\n")

    print()
    print("Waiting 2 seconds for cache to settle...")
    time.sleep(2)
    print()

    # ─── Phase 2: semantic matches ───
    print("━" * 80)
    print("PHASE 2 — Semantically similar (expect CACHE-SEMANTIC hits)")
    print("━" * 80)
    semantic_hits = 0
    for variant, original in RELATED_QUERIES:
        try:
            data = fetch(variant)
            print(f"  Variant of:  {original!r}")
            print_response(variant, data, "semantic")
            if data.get("cache_match_type") == "semantic":
                semantic_hits += 1
        except Exception as e:
            print(f"  ❌ Failed: {e}\n")

    # ─── Phase 3: unrelated queries ───
    print("━" * 80)
    print("PHASE 3 — Different topics (expect FRESH generation)")
    print("━" * 80)
    fresh_count = 0
    for q in DIFFERENT_QUERIES:
        try:
            data = fetch(q)
            print_response(q, data, "different")
            if not data.get("cached"):
                fresh_count += 1
        except Exception as e:
            print(f"  ❌ Failed: {e}\n")

    # ─── Summary ───
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Phase 1 (seed):           {len(SEED_QUERIES)} queries")
    print(f"  Phase 2 (semantic hits):  {semantic_hits} / {len(RELATED_QUERIES)} matched cache")
    print(f"  Phase 3 (fresh expected): {fresh_count} / {len(DIFFERENT_QUERIES)} were fresh")
    print()
    if semantic_hits == len(RELATED_QUERIES) and fresh_count == len(DIFFERENT_QUERIES):
        print("  ✅ Semantic cache working correctly")
    else:
        print(f"  ⚠ Some unexpected results — tune CACHE_SEMANTIC_THRESHOLD if needed")


if __name__ == "__main__":
    main()
