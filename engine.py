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
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger("engine")

# Python interpreter that has camoufox installed (browser_ai_capture needs it;
# the default python3 does NOT). Overridable via env for portability.
SUPERSCRAPE_PY = os.environ.get(
    "SUPERSCRAPE_PY",
    str(Path.home() / "Desktop" / "Canlah+Marketing" / "code"
        / "superscrape" / "venv" / "bin" / "python"))

# crawlability_scan uses the Playwright SYNC api. Playwright-sync CRASHES when
# driven from a ThreadPoolExecutor worker thread (Node driver EPIPE / "sync api
# inside thread"). We therefore run it in a DEDICATED SUBPROCESS, never in the
# pool — this both dodges the thread crash and isolates any Node-side crash from
# the report process. Timeout is generous because it renders up to 80 pages.
# Measured: a full 80-page Playwright crawl of a real Shopify site takes ~300s,
# so the default timeout has headroom above that; it graceful-degrades on overrun.
CRAWLABILITY_TIMEOUT_SEC = int(os.environ.get("CRAWLABILITY_TIMEOUT_SEC", "420"))
BROWSER_AI_TIMEOUT_SEC = int(os.environ.get("BROWSER_AI_TIMEOUT_SEC", "120"))

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
    # prompt_discovery: url + brand + industry_keywords (like news/backlink).
    # Pure HTTP (Serper + autocomplete) so it's pool-safe.
    ("prompts", "prompt_discovery", "probe", True),
    # gsc_connector: url-only; never raises (returns awaiting_authorization CTA
    # when no creds). Pure HTTP, pool-safe.
    ("gsc", "gsc_connector", "probe", False),
    # NOTE: "crawlability" is deliberately NOT here — it uses Playwright-sync
    # which crashes in a pool worker thread. It runs via _run_crawlability in a
    # dedicated subprocess, sequentially, in run_offsite_probes. See below.
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


# Probes whose probe() accepts a 3rd `industry_keywords` arg. For news/backlink
# it disambiguates same-name namesakes; for prompts it seeds long-tail/keyword
# discovery with the detected product/industry terms.
_INDUSTRY_KEYWORD_PROBES = {"news", "backlink", "prompts"}


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


# ---------------------------------------------------------------------------
# Subprocess probe runners (process isolation for crashy / venv-only probes)
# ---------------------------------------------------------------------------
# Both crawlability_scan (Playwright-sync) and browser_ai_capture (camoufox)
# drive Node-backed browsers. Run them in a SEPARATE PROCESS so:
#   1. Playwright-sync isn't in a ThreadPoolExecutor worker (which crashes), and
#   2. a Node-side crash can't take down the whole report process.
# The child probe writes its JSON result to a temp file (not stdout) so a noisy
# Node teardown on stdout can't corrupt the payload, then hard-exits with
# os._exit(0) before Node teardown can fire.

# Inline child script: loads ONE probe by path under a unique module name (same
# trick as _load_probe_module) and dumps its result to an out-file path.
_SUBPROCESS_CHILD = (
    "import sys, importlib.util, json, os\n"
    "from pathlib import Path\n"
    "root = Path(sys.argv[1]); mod_name = sys.argv[2]; out_path = sys.argv[3]\n"
    "args = json.loads(sys.argv[4])\n"
    "sys.path.append(str(root))\n"
    "p = root / 'probes' / (mod_name + '.py')\n"
    "spec = importlib.util.spec_from_file_location('_engine_probe_' + mod_name, p)\n"
    "m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m\n"
    "spec.loader.exec_module(m)\n"
    "try:\n"
    "    result = m.probe(*args)\n"
    "    Path(out_path).write_text(json.dumps(result), encoding='utf-8')\n"
    "    os._exit(0)\n"
    "except Exception as e:\n"
    "    Path(out_path).write_text(json.dumps(\n"
    "        {'_probe_status': 'error', '_reason': str(e)[:300]}), encoding='utf-8')\n"
    "    os._exit(0)\n"
)


def _run_probe_subprocess(engine_root: Path, module_name: str,
                          probe_args: list, python_exe: str,
                          timeout_sec: int) -> dict:
    """Run a single probe in an isolated subprocess; return its dict or a
    graceful-skip dict. Never raises.
    """
    import tempfile
    out_fd, out_path = tempfile.mkstemp(suffix=".json", prefix=f"{module_name}_")
    os.close(out_fd)
    try:
        proc = subprocess.run(
            [python_exe, "-c", _SUBPROCESS_CHILD,
             str(engine_root), module_name, out_path, json.dumps(probe_args)],
            capture_output=True, timeout=timeout_sec, text=True)
        data = Path(out_path).read_text(encoding="utf-8").strip()
        if data:
            return json.loads(data)
        reason = (proc.stderr or "no output").strip()[-300:]
        logger.warning("subprocess probe %s produced no result: %s",
                       module_name, reason)
        return {"_probe_status": "skipped", "reason": reason}
    except subprocess.TimeoutExpired:
        logger.warning("subprocess probe %s timed out after %ss",
                       module_name, timeout_sec)
        return {"_probe_status": "skipped",
                "reason": f"timeout after {timeout_sec}s"}
    except Exception as e:  # noqa: BLE001
        logger.warning("subprocess probe %s failed: %s", module_name, e)
        return {"_probe_status": "skipped", "reason": str(e)[:300]}
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _run_crawlability(engine_root: Path, url: str) -> dict:
    """Run crawlability_scan in an isolated subprocess (default python3).

    Playwright-sync cannot run in a pool worker thread; running it in its own
    process also shields the report from any Node-side crash. Uses the same
    interpreter that runs the report (sys.executable) — Playwright lives there.
    """
    return _run_probe_subprocess(
        engine_root, "crawlability_scan", [url],
        python_exe=sys.executable, timeout_sec=CRAWLABILITY_TIMEOUT_SEC)


def _run_browser_ai(engine_root: Path, url: str, brand: str | None,
                    queries: list[str]) -> dict:
    """Run browser_ai_capture (Camoufox Perplexity) via the superscrape venv
    python, because camoufox is NOT installed in the default python3.

    Graceful-skips on any failure (missing venv, timeout, crash) so the report
    never breaks. Keyed "perplexity_browser" by the caller.
    """
    if not Path(SUPERSCRAPE_PY).exists():
        logger.warning("browser_ai_capture skipped: venv python not found at %s",
                       SUPERSCRAPE_PY)
        return {"_probe_status": "skipped",
                "reason": f"superscrape venv python not found: {SUPERSCRAPE_PY}"}
    return _run_probe_subprocess(
        engine_root, "browser_ai_capture", [url, brand or "", queries[:5]],
        python_exe=SUPERSCRAPE_PY, timeout_sec=BROWSER_AI_TIMEOUT_SEC)


def _browser_ai_queries(offsite: dict[str, dict], url: str,
                        brand: str | None, industry_hint: str | None,
                        product_hint: str | None) -> list[str]:
    """Pick brand-relevant buyer prompts for the Perplexity browser capture.

    Prefer the prompt_discovery buyer_prompts (real PAA + templated), else fall
    back to the same buyer queries the ai_citation probe uses.
    """
    prompts = offsite.get("prompts", {}) or {}
    bp = prompts.get("buyer_prompts", []) or []
    picked = [p.get("prompt") for p in bp if p.get("prompt")][:5]
    if picked:
        return picked
    return build_buyer_queries(industry_hint, product_hint, brand)


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

    # --- crawlability: Playwright-sync, MUST run outside the pool (subprocess).
    logger.info("running crawlability_scan (Playwright, isolated subprocess)")
    results["crawlability"] = _run_crawlability(engine_root, url)
    logger.info("  [crawlability] %s",
                results["crawlability"].get("_probe_status",
                                            results["crawlability"].get("render_engine", "ok")))

    # --- perplexity_browser: Camoufox capture via the superscrape venv python.
    # Uses brand-relevant buyer prompts pulled from the prompts probe result.
    queries = _browser_ai_queries(results, url, brand, industry_hint, product_hint)
    logger.info("running browser_ai_capture (Perplexity, venv subprocess): %s", queries)
    results["perplexity_browser"] = _run_browser_ai(engine_root, url, brand, queries)
    logger.info("  [perplexity_browser] %s",
                results["perplexity_browser"].get("_probe_status",
                                                  results["perplexity_browser"].get("engine", "ok")))
    return results
