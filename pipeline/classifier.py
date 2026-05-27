# """
# pipeline/classifier.py — Predict which category a user question belongs to.

# Used at query time when the user did NOT explicitly select a category.
# The bot routes the question to a category-filtered search, dramatically
# improving precision over a "search-everything" approach.

# Two-stage strategy:
#   1. Fast keyword heuristic — runs in <1ms. Handles obvious cases free.
#   2. LLM classifier (gpt-4o-mini) — for ambiguous queries. ~$0.0001/call.

# Both stages return the SAME shape:
#     {
#         "category": "IT",            # one of the categories we know about
#         "confidence": "high",        # 'high' / 'medium' / 'low'
#         "method": "heuristic"        # or 'llm' or 'fallback'
#     }

# Public API:
#     classify(query, available_categories) -> dict
#     get_classifier_info() -> dict       # for /health endpoint
# """

# import json
# import logging
# import re
# from typing import List, Optional, Dict, Any

# from openai import AzureOpenAI
# from openai import APIConnectionError, APIError, RateLimitError, APITimeoutError

# from config import settings

# log = logging.getLogger(__name__)


# # ═══════════════════════════════════════════════════════════
# #  KEYWORD HEURISTICS (stage 1 — free, instant)
# # ═══════════════════════════════════════════════════════════
# # Maps category → list of regex patterns that strongly indicate that category.
# # Order matters: the FIRST matching category wins. Tune as needed.

# CATEGORY_KEYWORDS: Dict[str, List[re.Pattern]] = {
#     "IT": [
#         re.compile(r"\b(password|reset|login|signin|sign-?in|m365|microsoft 365|"
#                    r"vpn|wifi|wi-?fi|network|internet|laptop|computer|pc|desktop|"
#                    r"software|windows|outlook|teams|sharepoint|onedrive|"
#                    r"email|mailbox|spam|browser|chrome|edge|firefox|"
#                    r"server|application|app crash|install|update|patch|"
#                    r"blue ?screen|bsod|driver|printer|monitor|keyboard|mouse|"
#                    r"cable|hdmi|usb|bluetooth|webcam|mic|microphone|audio|sound|"
#                    r"two[- ]factor|2fa|mfa|authenticator)\b", re.I),
#     ],
#     "HR": [
#         re.compile(r"\b(leaves?|vacation|holidays?|sick day|sick leaves?|pto|"
#                    r"casual leaves?|privilege leaves?|maternity|paternity|"
#                    r"comp(?:ensatory)?[- ]?off|comp[- ]?off|"
#                    r"salary|payroll|payslip|pay slip|wage|compensation|bonus|"
#                    r"appraisal|review|performance|promotion|increment|"
#                    r"resignation|resign|notice period|offboard|"
#                    r"onboard|joining|join date|new hire|new joiner|"
#                    r"probation|confirmation|"
#                    r"hr policy|code of conduct|harassment|grievance|"
#                    r"insurance|medical|reimburs|expense claim|"
#                    r"employee benefit|provident fund|pf|gratuity|esi)\b", re.I),
#     ],
#     "Facilities": [
#         re.compile(r"\b(ac|air[- ]?condition|cooling|heating|hvac|"
#                    r"workspace|cubicle|seat|desk|chair|cabin|"
#                    r"office|premises|building|floor|parking|"
#                    r"cafeteria|pantry|water|drinking water|"
#                    r"cleaning|housekeeping|restroom|washroom|toilet|"
#                    r"lift|elevator|stairs|security guard|access card|"
#                    r"id card|visitor|reception|lobby)\b", re.I),
#     ],
#     "General": [
#         re.compile(r"\b(ticket|helpdesk|raise (?:a )?(?:ticket|request|issue)|"
#                    r"how do i (?:raise|create|submit|open) (?:a )?(?:ticket|request)|"
#                    r"submit (?:a )?(?:ticket|request|issue)|"
#                    r"contact (?:hr|it|admin|support)|escalate|"
#                    r"status of (?:my )?ticket|track (?:my )?ticket|"
#                    r"company policy|workplace policy|guideline)\b", re.I),
#     ],
# }


# def heuristic_classify(query: str,
#                        available_categories: List[str]) -> Optional[Dict[str, Any]]:
#     """
#     Try keyword-based classification first. Returns a result dict if confident,
#     None if ambiguous (so we fall through to the LLM).

#     Confidence rules:
#       - >=2 unique keyword hits from same category → high
#       - Exactly 1 hit → medium
#       - Multi-category tie → ambiguous (return None, escalate to LLM)
#     """
#     matches: Dict[str, int] = {}
#     for cat, patterns in CATEGORY_KEYWORDS.items():
#         if cat not in available_categories and cat != "Uncategorized":
#             continue
#         # Count UNIQUE keyword matches across all patterns for this category
#         unique_hits = set()
#         for pat in patterns:
#             for m in pat.finditer(query):
#                 unique_hits.add(m.group(0).lower())
#         if unique_hits:
#             matches[cat] = len(unique_hits)

#     if not matches:
#         return None  # No keyword hits — escalate to LLM

#     # Sort by match count
#     sorted_cats = sorted(matches.items(), key=lambda x: x[1], reverse=True)
#     top_cat, top_count = sorted_cats[0]

#     # If two categories tied or close → ambiguous, escalate to LLM
#     if len(sorted_cats) > 1 and sorted_cats[1][1] >= top_count:
#         return None

#     confidence = "high" if top_count >= 2 else "medium"
#     return {
#         "category": top_cat,
#         "confidence": confidence,
#         "method": "heuristic",
#         "matched_keywords": top_count,
#     }


# # ═══════════════════════════════════════════════════════════
# #  LLM CLASSIFIER (stage 2 — gpt-4o-mini)
# # ═══════════════════════════════════════════════════════════

# _llm_client: Optional[AzureOpenAI] = None


# def _get_llm_client() -> AzureOpenAI:
#     global _llm_client
#     if _llm_client is None:
#         _llm_client = AzureOpenAI(
#             api_key=settings.gpt_api_key,
#             api_version=settings.gpt_api_ver,
#             azure_endpoint=settings.gpt_endpoint,
#         )
#     return _llm_client


# CLASSIFY_SYSTEM = """You are a category classifier for a company helpdesk bot.

# Given a user question, decide which category it belongs to.

# Available categories (and what each covers):
# {categories_description}

# Rules:
# 1. Pick exactly ONE category — the most likely fit.
# 2. If the question is genuinely ambiguous or off-topic, pick "Uncategorized".
# 3. Set confidence based on how clear the question is:
#    - "high": the question clearly maps to one category
#    - "medium": likely, but could plausibly be another category
#    - "low": unclear, off-topic, or could match multiple
# 4. Respond with ONLY a JSON object. No prose, no markdown, no code fences.

# Required JSON shape:
# {{"category": "<one of the listed categories>", "confidence": "high|medium|low"}}"""


# CATEGORY_DESCRIPTIONS = {
#     "IT": "passwords, VPN, laptops, software, hardware, network, M365, email, "
#           "system issues, accounts, devices",
#     "HR": "leave, salary, payroll, benefits, policies, onboarding, offboarding, "
#           "appraisal, employee questions",
#     "Facilities": "office workspace, AC, seating, cafeteria, parking, "
#                   "building/floor issues, ID cards",
#     "General": "raising helpdesk tickets, escalation, general support, "
#                "company-wide policies",
#     "Uncategorized": "anything that doesn't clearly fit the above, "
#                      "or is off-topic / unrelated to work",
# }


# def _build_system_prompt(available_categories: List[str]) -> str:
#     """Build the system prompt listing only the categories that exist in the index."""
#     descriptions = []
#     # Always include Uncategorized so the LLM can fall back to it
#     cats_to_describe = list(available_categories)
#     if "Uncategorized" not in cats_to_describe:
#         cats_to_describe.append("Uncategorized")

#     for cat in cats_to_describe:
#         desc = CATEGORY_DESCRIPTIONS.get(cat, f"items categorized as {cat}")
#         descriptions.append(f"- {cat}: {desc}")

#     return CLASSIFY_SYSTEM.format(
#         categories_description="\n".join(descriptions),
#     )


# def llm_classify(query: str, available_categories: List[str]) -> Dict[str, Any]:
#     """
#     Ask gpt-4o-mini to classify. Returns dict with category, confidence, method.
#     Falls back to 'Uncategorized' / low / 'fallback' on any error.
#     """
#     system_prompt = _build_system_prompt(available_categories)

#     try:
#         client = _get_llm_client()
#         resp = client.chat.completions.create(
#             model=settings.gpt_mini_deploy,
#             messages=[
#                 {"role": "system", "content": system_prompt},
#                 {"role": "user", "content": query},
#             ],
#             temperature=0,
#             max_tokens=60,
#             response_format={"type": "json_object"},
#             timeout=10,
#         )
#         content = resp.choices[0].message.content or "{}"
#         result = json.loads(content)

#         category = result.get("category", "Uncategorized")
#         confidence = result.get("confidence", "low")

#         # Validate the LLM returned something sensible
#         valid_cats = set(available_categories) | {"Uncategorized"}
#         if category not in valid_cats:
#             log.warning(f"  LLM returned unknown category '{category}'. "
#                         f"Falling back to Uncategorized.")
#             category = "Uncategorized"
#             confidence = "low"
#         if confidence not in ("high", "medium", "low"):
#             confidence = "low"

#         return {
#             "category": category,
#             "confidence": confidence,
#             "method": "llm",
#         }
#     except (RateLimitError, APIConnectionError, APIError, APITimeoutError,
#             json.JSONDecodeError, KeyError, IndexError) as e:
#         log.warning(f"LLM classifier failed ({type(e).__name__}: {e}). "
#                     f"Using fallback.")
#         return {
#             "category": "Uncategorized",
#             "confidence": "low",
#             "method": "fallback",
#         }


# # ═══════════════════════════════════════════════════════════
# #  PUBLIC API
# # ═══════════════════════════════════════════════════════════

# def classify(query: str,
#              available_categories: List[str],
#              use_llm: bool = True) -> Dict[str, Any]:
#     """
#     Classify a user query into one of the available categories.

#     Args:
#         query: User's question.
#         available_categories: Categories that actually exist in the index
#                               (from list_categories()). The classifier will
#                               only ever return one of these (or Uncategorized).
#         use_llm: If False, skip the LLM and only use heuristics (returns
#                  'Uncategorized' if heuristic doesn't match).

#     Returns:
#         {
#             "category": "IT",
#             "confidence": "high",
#             "method": "heuristic" | "llm" | "fallback"
#         }
#     """
#     if not query or not query.strip():
#         return {"category": "Uncategorized", "confidence": "low", "method": "fallback"}

#     # Stage 1: keyword heuristic
#     heuristic_result = heuristic_classify(query, available_categories)
#     if heuristic_result and heuristic_result["confidence"] == "high":
#         return heuristic_result

#     # Stage 2: LLM (skipped if disabled or no available categories)
#     if use_llm and available_categories:
#         llm_result = llm_classify(query, available_categories)
#         # If LLM is high-confidence, use it; otherwise prefer heuristic if any
#         if llm_result["confidence"] == "high":
#             return llm_result
#         if heuristic_result:
#             return heuristic_result
#         return llm_result

#     # No LLM available, return heuristic if any, else Uncategorized
#     if heuristic_result:
#         return heuristic_result
#     return {"category": "Uncategorized", "confidence": "low", "method": "fallback"}


# def get_classifier_info() -> Dict[str, Any]:
#     """Diagnostic info for /health endpoint."""
#     return {
#         "llm_deployment": settings.gpt_mini_deploy,
#         "heuristic_categories": list(CATEGORY_KEYWORDS.keys()),
#     }


# # ═══════════════════════════════════════════════════════════
# #  CLI / quick test
# # ═══════════════════════════════════════════════════════════

# if __name__ == "__main__":
#     """
#     Test the classifier on a list of example queries.
#     Uses heuristic only by default (no API needed); pass --llm to test LLM too.

#     Usage:
#         python -m pipeline.classifier                    # heuristic only
#         python -m pipeline.classifier "my custom query"  # one query
#     """
#     import sys

#     available = ["IT", "HR", "Facilities", "General"]

#     if len(sys.argv) > 1:
#         queries = [" ".join(sys.argv[1:])]
#     else:
#         queries = [
#             "How do I reset my password?",
#             "My VPN keeps disconnecting",
#             "How many sick leaves do I have?",
#             "When will I get my salary?",
#             "AC is not working in our area",
#             "How do I raise a helpdesk ticket?",
#             "Laptop is making a weird noise",
#             "Need maternity leave information",
#             "What's the weather today?",                   # ambiguous / off-topic
#             "I need help with something",                   # too vague
#             "How do I submit my expense claim?",            # could be HR or General
#         ]

#     print("Query Classification Test (heuristic only — no API call)")
#     print("=" * 80)
#     for q in queries:
#         result = classify(q, available, use_llm=False)
#         cat = result["category"]
#         conf = result["confidence"]
#         method = result["method"]
#         print(f"  [{cat:14s}] {conf:6s} ({method:9s}) → {q}")


"""
pipeline/classifier.py — Predict which category a user question belongs to.

Used at query time when the user did NOT explicitly select a category.
The bot routes the question to a category-filtered search, dramatically
improving precision over a "search-everything" approach.

Two-stage strategy:
  1. Fast keyword heuristic — runs in <1ms. Handles obvious cases free.
  2. LLM classifier (gpt-4o-mini) — for ambiguous queries. ~$0.0001/call.

Both stages return the SAME shape:
    {
        "category": "IT",            # one of the categories we know about
        "confidence": "high",        # 'high' / 'medium' / 'low'
        "method": "heuristic"        # or 'llm' or 'fallback'
    }

Public API:
    classify(query, available_categories) -> dict
    get_classifier_info() -> dict       # for /health endpoint
"""

import json
import logging
import re
from typing import List, Optional, Dict, Any

from openai import AzureOpenAI
from openai import APIConnectionError, APIError, RateLimitError, APITimeoutError

from config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  KEYWORD HEURISTICS (stage 1 — free, instant)
# ═══════════════════════════════════════════════════════════
# Maps category → list of regex patterns that strongly indicate that category.
# Order matters: the FIRST matching category wins. Tune as needed.

CATEGORY_KEYWORDS: Dict[str, List[re.Pattern]] = {
    "IT": [
        re.compile(r"\b(password|reset|login|signin|sign-?in|m365|microsoft 365|"
                   r"vpn|wifi|wi-?fi|network|internet|laptop|computer|pc|desktop|"
                   r"software|windows|outlook|teams|sharepoint|onedrive|"
                   r"email|mailbox|spam|browser|chrome|edge|firefox|"
                   r"server|application|app crash|install|update|patch|"
                   r"blue ?screen|bsod|driver|printer|monitor|keyboard|mouse|"
                   r"cable|hdmi|usb|bluetooth|webcam|mic|microphone|audio|sound|"
                   r"two[- ]factor|2fa|mfa|authenticator)\b", re.I),
    ],
    "HR": [
        re.compile(r"\b(leaves?|vacation|holidays?|sick day|sick leaves?|pto|"
                   r"casual leaves?|privilege leaves?|maternity|paternity|"
                   r"comp(?:ensatory)?[- ]?off|comp[- ]?off|"
                   r"salary|payroll|payslip|pay slip|wage|compensation|bonus|"
                   r"appraisal|review|performance|promotion|increment|"
                   r"resignation|resign|notice period|offboard|"
                   r"onboard|joining|join date|new hire|new joiner|"
                   r"probation|confirmation|"
                   r"hr policy|code of conduct|harassment|grievance|"
                   r"insurance|medical|reimburs|expense claim|"
                   r"employee benefit|provident fund|pf|gratuity|esi)\b", re.I),
    ],
    "Facilities": [
        re.compile(r"\b(ac|air[- ]?condition|cooling|heating|hvac|"
                   r"workspace|cubicle|seat|desk|chair|cabin|"
                   r"office|premises|building|floor|parking|"
                   r"cafeteria|pantry|water|drinking water|"
                   r"cleaning|housekeeping|restroom|washroom|toilet|"
                   r"lift|elevator|stairs|security guard|access card|"
                   r"id card|visitor|reception|lobby)\b", re.I),
    ],
    "General": [
        re.compile(r"\b(ticket|helpdesk|raise (?:a )?(?:ticket|request|issue)|"
                   r"how do i (?:raise|create|submit|open) (?:a )?(?:ticket|request)|"
                   r"submit (?:a )?(?:ticket|request|issue)|"
                   r"contact (?:hr|it|admin|support)|escalate|"
                   r"status of (?:my )?ticket|track (?:my )?ticket|"
                   r"company policy|workplace policy|guideline)\b", re.I),
    ],
}


def heuristic_classify(query: str,
                       available_categories: List[str]) -> Optional[Dict[str, Any]]:
    """
    Try keyword-based classification first. Returns a result dict if confident,
    None if ambiguous (so we fall through to the LLM).

    Confidence rules:
      - >=2 unique keyword hits from same category → high
      - Exactly 1 hit → medium
      - Multi-category tie → ambiguous (return None, escalate to LLM)
    """
    matches: Dict[str, int] = {}
    for cat, patterns in CATEGORY_KEYWORDS.items():
        if cat not in available_categories and cat != "Uncategorized":
            continue
        # Count UNIQUE keyword matches across all patterns for this category
        unique_hits = set()
        for pat in patterns:
            for m in pat.finditer(query):
                unique_hits.add(m.group(0).lower())
        if unique_hits:
            matches[cat] = len(unique_hits)

    if not matches:
        return None  # No keyword hits — escalate to LLM

    # Sort by match count
    sorted_cats = sorted(matches.items(), key=lambda x: x[1], reverse=True)
    top_cat, top_count = sorted_cats[0]

    # If two categories tied or close → ambiguous, escalate to LLM
    if len(sorted_cats) > 1 and sorted_cats[1][1] >= top_count:
        return None

    confidence = "high" if top_count >= 2 else "medium"
    return {
        "category": top_cat,
        "confidence": confidence,
        "method": "heuristic",
        "matched_keywords": top_count,
    }


# ═══════════════════════════════════════════════════════════
#  LLM CLASSIFIER (stage 2 — gpt-4o-mini)
# ═══════════════════════════════════════════════════════════

_llm_client: Optional[AzureOpenAI] = None


def _get_llm_client() -> AzureOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AzureOpenAI(
            api_key=settings.gpt_api_key,
            api_version=settings.gpt_api_ver,
            azure_endpoint=settings.gpt_endpoint,
        )
    return _llm_client


CLASSIFY_SYSTEM = """You are a category classifier for a company helpdesk bot.

Given a user question, decide which category it belongs to.

Available categories (and what each covers):
{categories_description}

Rules:
1. Pick exactly ONE category — the most likely fit.
2. If the question is genuinely ambiguous or off-topic, pick "Uncategorized".
3. Set confidence based on how clear the question is:
   - "high": the question clearly maps to one category
   - "medium": likely, but could plausibly be another category
   - "low": unclear, off-topic, or could match multiple
4. Respond with ONLY a JSON object. No prose, no markdown, no code fences.

Required JSON shape:
{{"category": "<one of the listed categories>", "confidence": "high|medium|low"}}"""


CATEGORY_DESCRIPTIONS = {
    "IT": "passwords, VPN, laptops, software, hardware, network, M365, email, "
          "system issues, accounts, devices",
    "HR": "leave, salary, payroll, benefits, policies, onboarding, offboarding, "
          "appraisal, employee questions",
    "Facilities": "office workspace, AC, seating, cafeteria, parking, "
                  "building/floor issues, ID cards",
    "General": "raising helpdesk tickets, escalation, general support, "
               "company-wide policies",
    "Uncategorized": "anything that doesn't clearly fit the above, "
                     "or is off-topic / unrelated to work",
}


def _build_system_prompt(available_categories: List[str]) -> str:
    """Build the system prompt listing only the categories that exist in the index."""
    descriptions = []
    # Always include Uncategorized so the LLM can fall back to it
    cats_to_describe = list(available_categories)
    if "Uncategorized" not in cats_to_describe:
        cats_to_describe.append("Uncategorized")

    for cat in cats_to_describe:
        desc = CATEGORY_DESCRIPTIONS.get(cat, f"items categorized as {cat}")
        descriptions.append(f"- {cat}: {desc}")

    return CLASSIFY_SYSTEM.format(
        categories_description="\n".join(descriptions),
    )


def llm_classify(query: str, available_categories: List[str]) -> Dict[str, Any]:
    """
    Ask gpt-4o-mini to classify. Returns dict with category, confidence, method.
    Falls back to 'Uncategorized' / low / 'fallback' on any error.
    """
    system_prompt = _build_system_prompt(available_categories)

    try:
        client = _get_llm_client()
        resp = client.chat.completions.create(
            model=settings.gpt_mini_deploy,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            temperature=0,
            max_tokens=60,
            response_format={"type": "json_object"},
            timeout=10,
        )

        # Record token usage (non-fatal)
        try:
            from storage import cache as _cache
            if resp.usage:
                _cache.record_llm_usage(
                    call_type="classify",
                    model=settings.gpt_mini_deploy,
                    input_tokens=resp.usage.prompt_tokens,
                    output_tokens=resp.usage.completion_tokens,
                    request_id=None,
                    question=query,
                )
        except Exception:
            pass

        content = resp.choices[0].message.content or "{}"
        result = json.loads(content)

        category = result.get("category", "Uncategorized")
        confidence = result.get("confidence", "low")

        # Validate the LLM returned something sensible
        valid_cats = set(available_categories) | {"Uncategorized"}
        if category not in valid_cats:
            log.warning(f"  LLM returned unknown category '{category}'. "
                        f"Falling back to Uncategorized.")
            category = "Uncategorized"
            confidence = "low"
        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        return {
            "category": category,
            "confidence": confidence,
            "method": "llm",
        }
    except (RateLimitError, APIConnectionError, APIError, APITimeoutError,
            json.JSONDecodeError, KeyError, IndexError) as e:
        log.warning(f"LLM classifier failed ({type(e).__name__}: {e}). "
                    f"Using fallback.")
        return {
            "category": "Uncategorized",
            "confidence": "low",
            "method": "fallback",
        }


# ═══════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════

def classify(query: str,
             available_categories: List[str],
             use_llm: bool = True) -> Dict[str, Any]:
    """
    Classify a user query into one of the available categories.

    Args:
        query: User's question.
        available_categories: Categories that actually exist in the index
                              (from list_categories()). The classifier will
                              only ever return one of these (or Uncategorized).
        use_llm: If False, skip the LLM and only use heuristics (returns
                 'Uncategorized' if heuristic doesn't match).

    Returns:
        {
            "category": "IT",
            "confidence": "high",
            "method": "heuristic" | "llm" | "fallback"
        }
    """
    if not query or not query.strip():
        return {"category": "Uncategorized", "confidence": "low", "method": "fallback"}

    # Stage 1: keyword heuristic
    heuristic_result = heuristic_classify(query, available_categories)
    if heuristic_result and heuristic_result["confidence"] == "high":
        return heuristic_result

    # Stage 2: LLM (skipped if disabled or no available categories)
    if use_llm and available_categories:
        llm_result = llm_classify(query, available_categories)
        # If LLM is high-confidence, use it; otherwise prefer heuristic if any
        if llm_result["confidence"] == "high":
            return llm_result
        if heuristic_result:
            return heuristic_result
        return llm_result

    # No LLM available, return heuristic if any, else Uncategorized
    if heuristic_result:
        return heuristic_result
    return {"category": "Uncategorized", "confidence": "low", "method": "fallback"}


def get_classifier_info() -> Dict[str, Any]:
    """Diagnostic info for /health endpoint."""
    return {
        "llm_deployment": settings.gpt_mini_deploy,
        "heuristic_categories": list(CATEGORY_KEYWORDS.keys()),
    }


# ═══════════════════════════════════════════════════════════
#  CLI / quick test
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Test the classifier on a list of example queries.
    Uses heuristic only by default (no API needed); pass --llm to test LLM too.

    Usage:
        python -m pipeline.classifier                    # heuristic only
        python -m pipeline.classifier "my custom query"  # one query
    """
    import sys

    available = ["IT", "HR", "Facilities", "General"]

    if len(sys.argv) > 1:
        queries = [" ".join(sys.argv[1:])]
    else:
        queries = [
            "How do I reset my password?",
            "My VPN keeps disconnecting",
            "How many sick leaves do I have?",
            "When will I get my salary?",
            "AC is not working in our area",
            "How do I raise a helpdesk ticket?",
            "Laptop is making a weird noise",
            "Need maternity leave information",
            "What's the weather today?",                   # ambiguous / off-topic
            "I need help with something",                   # too vague
            "How do I submit my expense claim?",            # could be HR or General
        ]

    print("Query Classification Test (heuristic only — no API call)")
    print("=" * 80)
    for q in queries:
        result = classify(q, available, use_llm=False)
        cat = result["category"]
        conf = result["confidence"]
        method = result["method"]
        print(f"  [{cat:14s}] {conf:6s} ({method:9s}) → {q}")