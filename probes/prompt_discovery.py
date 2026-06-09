#!/usr/bin/env python3
"""Long-tail keyword + buyer-prompt discovery — FREE via Serper + Google autocomplete.

Replicates the "buyer-prompt / long-tail keyword discovery" that paid GEO/SEO
tools charge for, using only free signal sources:

  * Serper.dev (env SERPER_API_KEY) — `relatedSearches`, `peopleAlsoAsk`,
    and `organic` results are free long-tail / question sources.
  * Google autocomplete (`suggestqueries.google.com`) — fully free, no key.

Output feeds two downstream consumers:
  (a) the live_ai_search probe, with REAL buyer prompts to test against AI search;
  (b) a keyword / long-tail module in the report.

Budget: <= ~6 Serper calls (5 seeds + headroom) + autocomplete (free).
Stdlib only. No print(); uses logging. Graceful without SERPER_API_KEY
(autocomplete still produces keywords + prompts).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"
AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
TIMEOUT_SEC = 10.0
MAX_SEEDS = 5
SERPER_DELAY_SEC = 1.0
USER_AGENT = "Mozilla/5.0 (compatible; canmarket-audit/1.1; +prompt_discovery)"

# Question-leading tokens used to classify keywords / prompts as "questions".
_QUESTION_LEADS = (
    "who", "what", "which", "when", "where", "why", "how",
    "is", "are", "can", "do", "does", "should", "best",
)

# Intent classification keyword sets. Order matters: checked top-down.
_COMPARISON_TOKENS = ("vs", "versus", "compare", "comparison", "or", "alternative",
                      "difference between", "better than")
_RECOMMENDATION_TOKENS = ("best", "top", "recommended", "recommend", "good",
                          "review", "reviews", "rated", "where to buy", "buy")
_FACTUAL_TOKENS = ("what is", "how to", "how much", "how do", "how does", "cost",
                   "price", "size", "dimensions", "install", "material", "warranty",
                   "are", "is", "does", "do")
# everything else -> discovery


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #
def _http_get_json(url: str) -> Any | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        logger.warning("autocomplete GET failed for %s: %s", url, e)
        return None
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("autocomplete JSON parse failed for %s: %s", url, e)
        return None


def _serper_post(query: str, api_key: str) -> dict | None:
    body = json.dumps({"q": query, "hl": "en", "num": 20}).encode("utf-8")
    req = urllib.request.Request(
        SERPER_URL, data=body, method="POST",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        logger.warning("serper POST failed for %r: %s", query, e)
        return None
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("serper JSON parse failed for %r: %s", query, e)
        return None


def _autocomplete(seed: str) -> list[str]:
    """Free Google autocomplete suggestions for a seed term."""
    q = urllib.parse.urlencode({"client": "firefox", "q": seed})
    data = _http_get_json(f"{AUTOCOMPLETE_URL}?{q}")
    # Firefox client returns [query, [suggestions...]].
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
        return [s for s in data[1] if isinstance(s, str) and s.strip()]
    return []


# --------------------------------------------------------------------------- #
# Seed derivation
# --------------------------------------------------------------------------- #
def _derive_seeds_from_domain(url: str) -> list[str]:
    """Best-effort seed terms from the domain when no industry hint is given."""
    try:
        host = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        host = url.lower()
    host = host[4:] if host.startswith("www.") else host
    label = host.split(".")[0] if host else ""
    # Split camelCase / digits and known glue words.
    label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label)
    label = re.sub(r"[^a-zA-Z]+", " ", label).strip().lower()
    parts = [p for p in label.split() if len(p) > 2]
    return parts[:2]


def _build_seeds(url: str, brand_name: str | None,
                 industry_keywords: list[str] | None,
                 categories: list[str] | None = None) -> list[str]:
    seeds: "OrderedDict[str, None]" = OrderedDict()
    # Prefer the LLM-detected canonical categories — these are real product-NOUN
    # phrases ("rugged mobile computers", "industrial tablets") that make both
    # good Serper seeds AND grammatical buyer prompts. The legacy
    # industry_keywords are single tokenized words (often adjectives like
    # "rugged"/"industrial") that produce nonsense templates ("a good rugged
    # for my home"), so they are only a fallback.
    for cat in (categories or []):
        cat = (cat or "").strip().lower()
        if cat:
            seeds.setdefault(cat, None)
    if not seeds:
        for kw in (industry_keywords or []):
            kw = (kw or "").strip().lower()
            if kw:
                seeds.setdefault(kw, None)
    if not seeds:
        for s in _derive_seeds_from_domain(url):
            seeds.setdefault(s, None)
    # Brand term is a useful seed too (brand + product queries), but never the
    # only one — we still want generic product long-tails.
    if brand_name and brand_name.strip():
        seeds.setdefault(brand_name.strip().lower(), None)
    if not seeds:
        seeds.setdefault("product", None)
    return list(seeds.keys())[:MAX_SEEDS]


# --------------------------------------------------------------------------- #
# Intent classification
# --------------------------------------------------------------------------- #
def _classify_intent(text: str) -> str:
    t = f" {text.lower().strip()} "
    if any(f" {tok} " in t or t.strip().startswith(tok + " ") for tok in _COMPARISON_TOKENS):
        return "comparison"
    for tok in _RECOMMENDATION_TOKENS:
        if t.strip().startswith(tok + " ") or f" {tok} " in t:
            return "recommendation"
    for tok in _FACTUAL_TOKENS:
        if t.strip().startswith(tok + " ") or f" {tok} " in t:
            return "factual"
    return "discovery"


def _is_question(text: str) -> bool:
    t = text.lower().strip().rstrip("?")
    if not t:
        return False
    first = t.split()[0]
    return first in _QUESTION_LEADS or text.strip().endswith("?")


# --------------------------------------------------------------------------- #
# Serper extraction
# --------------------------------------------------------------------------- #
def _extract_from_serper(payload: dict) -> tuple[list[str], list[str]]:
    """Return (related_searches, paa_questions) from one Serper response."""
    related: list[str] = []
    for item in payload.get("relatedSearches", []) or []:
        q = (item.get("query") if isinstance(item, dict) else None) or ""
        if q.strip():
            related.append(q.strip())

    questions: list[str] = []
    for item in payload.get("peopleAlsoAsk", []) or []:
        q = (item.get("question") if isinstance(item, dict) else None) or ""
        if q.strip():
            questions.append(q.strip())
    return related, questions


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _add_keyword(bucket: "OrderedDict[str, dict]", keyword: str, source: str) -> None:
    key = keyword.strip().lower()
    if not key or len(key) < 3:
        return
    if key not in bucket:
        bucket[key] = {
            "keyword": keyword.strip(),
            "source": source,
            "intent": _classify_intent(keyword),
        }


def _build_buyer_prompts(seeds: list[str], questions: list[str],
                         keywords: list[dict]) -> list[dict]:
    """Assemble 8-12 AI-search prompts, categorized by intent.

    Mixes real PAA / question-form keywords (high-signal) with templated
    buyer prompts built from the primary product seeds.
    """
    prompts: "OrderedDict[str, dict]" = OrderedDict()

    def _add(prompt: str, intent: str) -> None:
        p = prompt.strip()
        key = p.lower()
        if p and key not in prompts:
            prompts[key] = {"prompt": p, "intent": intent}

    # Primary product term: first non-brand seed, else first seed.
    product = seeds[0] if seeds else "product"

    # 1) Real questions first (most authentic buyer prompts).
    for q in questions[:6]:
        _add(q if q.endswith("?") else q + "?", _classify_intent(q))

    # 2) Templated prompts across the four intent buckets.
    # Register-neutral so they read naturally for both B2B procurement and
    # consumer intent (no "for my home" — off-register for industrial buyers).
    templates = [
        (f"What are the best {product} options to buy?", "recommendation"),
        (f"Which {product} would you recommend?", "recommendation"),
        (f"Which {product} brand is most highly rated?", "recommendation"),
        (f"Compare top {product} brands", "comparison"),
        (f"{product} vs alternatives — which is better?", "comparison"),
        (f"Where can I buy a quality {product}?", "discovery"),
        (f"Who makes durable {product}?", "discovery"),
        (f"How much does a {product} cost?", "factual"),
        (f"How do I choose the right {product}?", "factual"),
    ]
    for prompt, intent in templates:
        _add(prompt, intent)

    # 3) Backfill from high-signal long-tail keywords if still under 10.
    if len(prompts) < 10:
        for kw in keywords:
            if len(prompts) >= 12:
                break
            text = kw["keyword"]
            intent = kw["intent"]
            if _is_question(text):
                _add(text if text.endswith("?") else text + "?", intent)
            elif intent == "comparison":
                _add(f"Which is better: {text}?", intent)
            elif intent == "recommendation":
                _add(f"Is {text} a good choice?", intent)

    return list(prompts.values())[:12]


def _grade(num_keywords: int, num_prompts: int, serper_ok: bool) -> str:
    if num_keywords >= 25 and num_prompts >= 10 and serper_ok:
        return "RICH"
    if num_keywords >= 12 and num_prompts >= 8:
        return "MODERATE"
    return "THIN"


# --------------------------------------------------------------------------- #
# Public probe
# --------------------------------------------------------------------------- #
def probe(url: str, brand_name: str | None = None,
          industry_keywords: list[str] | None = None,
          categories: list[str] | None = None) -> dict:
    """Discover long-tail keywords, buyer questions, and AI-search buyer prompts.

    Args:
        url: target site URL.
        brand_name: optional brand name (used as an extra seed).
        industry_keywords: optional product/industry seed terms (legacy single
            tokens; used only when no categories are given).
        categories: the brand's canonical product-category noun phrases from the
            deep business-understanding pass. Preferred seed source — yields
            grammatical, on-register buyer prompts.

    Returns:
        dict with long_tail_keywords, questions, buyer_prompts, intent_breakdown,
        api_calls_used, and a RICH|MODERATE|THIN grade.
    """
    seeds = _build_seeds(url, brand_name, industry_keywords, categories)
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        logger.info("SERPER_API_KEY not set — autocomplete-only mode.")

    keywords: "OrderedDict[str, dict]" = OrderedDict()
    questions_set: "OrderedDict[str, None]" = OrderedDict()
    api_calls = 0
    serper_ok = False

    for i, seed in enumerate(seeds):
        # Serper (capped at MAX_SEEDS, 1s spacing).
        if api_key:
            if api_calls > 0:
                time.sleep(SERPER_DELAY_SEC)
            payload = _serper_post(seed, api_key)
            api_calls += 1
            if payload:
                serper_ok = True
                related, paa = _extract_from_serper(payload)
                for r in related:
                    _add_keyword(keywords, r, "serper_related")
                for q in paa:
                    _add_keyword(keywords, q, "serper_paa")
                    questions_set.setdefault(q, None)

        # Google autocomplete (free, every seed).
        for sug in _autocomplete(seed)[:10]:
            _add_keyword(keywords, sug, "autocomplete")
            if _is_question(sug):
                questions_set.setdefault(sug, None)

    keyword_list = list(keywords.values())

    # Questions = explicit PAA + question-form keywords (deduped).
    for kw in keyword_list:
        if _is_question(kw["keyword"]):
            questions_set.setdefault(kw["keyword"], None)
    questions = list(questions_set.keys())

    buyer_prompts = _build_buyer_prompts(seeds, questions, keyword_list)

    intent_breakdown = {
        "discovery": 0, "comparison": 0, "recommendation": 0, "factual": 0,
    }
    for p in buyer_prompts:
        intent_breakdown[p["intent"]] = intent_breakdown.get(p["intent"], 0) + 1

    grade = _grade(len(keyword_list), len(buyer_prompts), serper_ok)

    return {
        "target_url": url,
        "brand_name": brand_name,
        "seeds_used": seeds,
        "long_tail_keywords": keyword_list,
        "questions": questions,
        "buyer_prompts": buyer_prompts,
        "intent_breakdown": intent_breakdown,
        "api_calls_used": api_calls,
        "grade": grade,
    }


def main() -> int:
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Long-tail keyword + buyer-prompt discovery")
    ap.add_argument("url")
    ap.add_argument("--brand", default=None)
    ap.add_argument("--keywords", nargs="*", default=None,
                    help="industry/product seed terms")
    args = ap.parse_args()
    result = probe(args.url, args.brand, args.keywords)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
