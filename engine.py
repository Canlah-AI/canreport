#!/usr/bin/env python3
"""Adapter to the canmarket-site-audit probe engine.

The probe engine (data layer) lives in a separate repo. This module locates
it, runs the off-site SEO probes, and returns their raw output keyed by a
short name the contract builder understands.

The engine path is resolved in this order:
  1. $CANMARKET_AUDIT_PATH env var
  2. ~/dev/canmarket-site-audit-v1.1  (default dev location)
  3. ~/dev/canmarket-site-audit

Off-site probes run concurrently. Each is independent; a failure in one
returns an empty dict for that probe rather than aborting the whole run.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger("engine")

# (result_key, module_name, callable, needs_brand)
# "ai_citation" uses a dedicated runner (run_ai_citation) instead of the generic
# probe() wrapper, because the generic wrapper hard-codes generic Singapore
# queries; we need BRAND-RELEVANT buyer-intent queries derived from the
# detected industry/product. The engine dispatches it specially in _run_one.
OFFSITE_PROBES: list[tuple[str, str, str, bool]] = [
    ("ai_citation", "live_ai_search", "probe", True),
    ("reputation", "reviews_scan", "probe", True),
    ("backlink", "backlink_scan", "probe", True),
    ("community", "community_mention_scan", "probe", True),
    ("news", "news_coverage_scan", "probe", True),
    ("social", "social_influence_scan", "probe", True),
    ("nap", "nap_consistency_scan", "probe", True),
    ("schema", "schema_validator", "probe", False),
    ("freshness", "content_freshness_scan", "probe", False),
]


# ---------------------------------------------------------------------------
# Brand-relevant buyer-intent query generation for live_ai_search
# ---------------------------------------------------------------------------
# The default live_ai_search template system emits generic "best providers in
# {geo}" queries that surface garbage (telco/banking/healthcare). For an honest
# GEO citation test we feed EXPLICIT buyer-intent queries built from the
# detected product noun. Five intents: discovery, evaluation, comparison,
# transactional, problem-led.

# Map detected (Chinese) industry/product keywords → an English product noun
# usable in buyer-search query templates. Fallback handles unknown industries.
_PRODUCT_NOUN_MAP: list[tuple[tuple[str, ...], str, str]] = [
    # (match keywords found in detected industry/product, english_product, use_case)
    (("凉亭", "遮阳", "户外建材", "gazebo", "pergola", "shade", "outdoor"),
     "outdoor gazebos", "backyard"),
    (("服装", "时尚", "apparel", "fashion", "clothing"),
     "clothing brands", "everyday wear"),
    (("护肤", "美妆", "化妆", "skincare", "beauty", "cosmetic"),
     "skincare products", "sensitive skin"),
    (("家具", "furniture"),
     "furniture", "small spaces"),
    (("食品", "饮料", "food", "beverage", "snack"),
     "food brands", "healthy eating"),
    (("软件", "saas", "software", "app", "平台", "工具"),
     "software tools", "small business"),
]


def _english_product_and_usecase(industry_hint: str | None,
                                  product_hint: str | None,
                                  brand: str | None) -> tuple[str, str]:
    """Derive an English product noun + use-case from the detected context.

    Brand-agnostic: reads detected industry/product strings (may be Chinese)
    and maps to an English buyer-search noun. Falls back to a generic noun.
    """
    blob = " ".join(filter(None, [industry_hint, product_hint])).lower()
    for keywords, product, use_case in _PRODUCT_NOUN_MAP:
        if any(k.lower() in blob for k in keywords):
            return product, use_case
    # Fallback: use the raw product hint if it's already ASCII/English, else
    # a generic noun built from the brand.
    if product_hint and product_hint.isascii() and product_hint.strip():
        return product_hint.strip(), "home use"
    return "products in this category", "everyday use"


# Map detected (Chinese/English) industry/product keywords → a small list of
# English industry keywords used by the news + backlink probes to disambiguate
# genuine brand coverage from same-name (namesake) entities in other industries.
_INDUSTRY_KEYWORD_MAP: list[tuple[tuple[str, ...], list[str]]] = [
    (("凉亭", "遮阳", "户外建材", "gazebo", "pergola", "shade", "outdoor", "awning",
      "patio", "canopy"),
     ["gazebo", "shade", "awning", "pergola", "patio", "canopy", "outdoor",
      "sail", "umbrella"]),
    (("服装", "时尚", "apparel", "fashion", "clothing"),
     ["apparel", "fashion", "clothing", "wear", "outfit", "style"]),
    (("护肤", "美妆", "化妆", "skincare", "beauty", "cosmetic"),
     ["skincare", "beauty", "cosmetic", "serum", "moisturizer", "cream"]),
    (("家具", "furniture"),
     ["furniture", "sofa", "chair", "table", "decor", "home"]),
    (("食品", "饮料", "food", "beverage", "snack"),
     ["food", "beverage", "snack", "drink", "nutrition", "flavor"]),
    (("软件", "saas", "software", "app", "平台", "工具"),
     ["software", "saas", "app", "platform", "tool", "cloud"]),
]


def derive_industry_keywords(industry_hint: str | None,
                             product_hint: str | None) -> list[str]:
    """Build a small English industry-keyword list from detected hints.

    Brand-agnostic. Used by the news_coverage_scan + backlink_scan probes to
    separate genuine brand coverage from same-name (namesake) entities in other
    industries. Always includes any ASCII words from the raw hints so unmapped
    industries still get a usable signal.
    """
    blob = " ".join(filter(None, [industry_hint, product_hint])).lower()
    keywords: list[str] = []
    for match_words, kws in _INDUSTRY_KEYWORD_MAP:
        if any(k.lower() in blob for k in match_words):
            keywords.extend(kws)
            break
    # Always fold in ASCII tokens from the raw hints (handles unmapped industries).
    for token in blob.replace("/", " ").replace(",", " ").split():
        if token.isascii() and token.isalpha() and len(token) > 2:
            keywords.append(token)
    # De-duplicate, preserve order.
    seen: set[str] = set()
    result: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def build_buyer_queries(industry_hint: str | None,
                        product_hint: str | None,
                        brand: str | None,
                        geography: str = "Singapore") -> list[str]:
    """Build exactly 5 buyer-intent queries spanning 4 intent types.

    Templates (brand-agnostic):
      1. best {product} brands {geo}            (discovery)
      2. {product} reviews {geo}                (evaluation)
      3. {product} comparison                   (comparison)
      4. where to buy affordable {product} {geo}(transactional)
      5. best {product} for {use_case}          (problem-led)
    """
    product, use_case = _english_product_and_usecase(industry_hint, product_hint, brand)
    geo = geography.strip() or "Singapore"
    return [
        f"best {product} brands {geo}",
        f"{product} reviews {geo}",
        f"{product} comparison",
        f"where to buy affordable {product} {geo}",
        f"best {product} for {use_case}",
    ]


def run_ai_citation(engine_root: Path, url: str, brand: str | None,
                    industry_hint: str | None = None,
                    product_hint: str | None = None,
                    geography: str = "Singapore",
                    region_code: str = "sg") -> dict:
    """Run live_ai_search with EXPLICIT brand-relevant buyer-intent queries.

    Bypasses the generic template system (the `queries=` param overrides it
    entirely — see live_ai_search.run_citation_test line 469).
    """
    import asyncio
    from dataclasses import asdict

    mod = _load_probe_module(engine_root, "live_ai_search")
    queries = build_buyer_queries(industry_hint, product_hint, brand, geography)
    logger.info("ai_citation buyer queries: %s", queries)
    report = asyncio.run(mod.run_citation_test(
        target_url=url,
        brand_name=brand or "",
        industry="dtc-ecommerce",
        geography=geography,
        region_code=region_code,
        queries=queries,
        enable_gemini=True,
        enable_serper=True,
    ))
    return asdict(report)


def locate_engine() -> Path:
    """Return the path to the canmarket-site-audit repo, or raise."""
    candidates = []
    env = os.environ.get("CANMARKET_AUDIT_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "dev" / "canmarket-site-audit-v1.1")
    candidates.append(Path.home() / "dev" / "canmarket-site-audit")
    for c in candidates:
        if (c / "probes").is_dir():
            return c
    raise FileNotFoundError(
        "canmarket-site-audit engine not found. Set $CANMARKET_AUDIT_PATH or "
        f"clone it to one of: {[str(c) for c in candidates]}"
    )


def _load_probe_module(engine_root: Path, module_name: str):
    """Load a probe module by FILE PATH from the engine's probes/ dir.

    We deliberately avoid `import probes.<name>` because this repo also has a
    `probes` package (the on-site EAC infra probes). Importing by path under a
    unique module name (`_engine_probe_<name>`) dodges that name collision and
    lets the engine's probe import its own siblings via the path entry.
    """
    probe_path = engine_root / "probes" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"_engine_probe_{module_name}", probe_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {probe_path}")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: Python 3.12+ @dataclass / typing look the module up
    # via sys.modules[cls.__module__]; an unregistered path-loaded module makes
    # that return None and crash with "'NoneType' has no attribute '__dict__'".
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Probes whose probe() accepts a 3rd `industry_keywords` arg for entity
# disambiguation (separating genuine brand mentions from same-name namesakes).
_INDUSTRY_KEYWORD_PROBES = {"news", "backlink"}


def _run_one(engine_root: Path, key: str, module_name: str, fn_name: str,
             needs_brand: bool, url: str, brand: str | None,
             industry_hint: str | None = None,
             product_hint: str | None = None) -> tuple[str, dict]:
    try:
        # ai_citation uses a dedicated runner with brand-relevant queries.
        if key == "ai_citation":
            return key, run_ai_citation(
                engine_root, url, brand,
                industry_hint=industry_hint, product_hint=product_hint)
        mod = _load_probe_module(engine_root, module_name)
        fn = getattr(mod, fn_name)
        # news + backlink probes take industry_keywords (3rd arg) to exclude
        # same-name (namesake) entities from other industries.
        if key in _INDUSTRY_KEYWORD_PROBES:
            industry_keywords = derive_industry_keywords(industry_hint, product_hint)
            logger.info("%s industry_keywords: %s", key, industry_keywords)
            result = fn(url, brand, industry_keywords)
        elif needs_brand:
            result = fn(url, brand)
        else:
            result = fn(url)
        return key, (result if isinstance(result, dict) else {"value": result})
    except Exception as e:  # noqa: BLE001 — one probe failing must not abort the run
        logger.warning("off-site probe %s failed: %s", module_name, e)
        return key, {"_probe_status": "error", "_reason": str(e)[:300]}


def run_offsite_probes(url: str, brand: str | None = None,
                       max_workers: int = 3,
                       industry_hint: str | None = None,
                       product_hint: str | None = None) -> dict[str, dict]:
    """Run all off-site probes and return {key: probe_output}.

    Concurrency is capped at 3 (not 9) on purpose: each probe fires 4-6 Serper
    calls, so running all at once bursts ~40 calls in seconds and trips the
    Serper free-tier rate limit (HTTP 400). Three-at-a-time spreads the load
    while still finishing in well under a minute.

    industry_hint / product_hint (read by report.py from the on-site audit's
    recommendations.detected_context) drive BRAND-RELEVANT buyer-intent queries
    for the ai_citation probe — without them the citation test would run the
    generic Singapore template that surfaces garbage competitors.
    """
    engine_root = locate_engine()
    # Put the engine root on sys.path so engine probes that do
    # `from probes import X` for their own siblings resolve correctly.
    if str(engine_root) not in sys.path:
        sys.path.append(str(engine_root))
    logger.info("using engine at %s", engine_root)

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_run_one, engine_root, key, mod, fn, nb, url, brand,
                        industry_hint, product_hint)
            for (key, mod, fn, nb) in OFFSITE_PROBES
        ]
        for fut in as_completed(futures):
            key, output = fut.result()
            results[key] = output
            status = output.get("_probe_status", "ok")
            logger.info("  [%s] %s", key, status)
    return results
