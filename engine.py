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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger("engine")

# ---------------------------------------------------------------------------
# Cost + API-call observability
# ---------------------------------------------------------------------------
# Paid-equivalent unit costs (USD per external API call). Serper is on the free
# tier here but we cost it at the paid-equivalent rate so the report reflects the
# true marginal cost of a run at scale. PSI / autocomplete / HTTP scraping are
# free and contribute $0.
SERPER_COST_PER_CALL = float(os.environ.get("SERPER_COST_PER_CALL", "0.001"))
GEMINI_COST_PER_CALL = float(os.environ.get("GEMINI_COST_PER_CALL", "0.0005"))
# Bright Data SERP (AI-Overview engine in live_ai_search) — pay-for-success.
BRIGHTDATA_COST_PER_CALL = float(os.environ.get("BRIGHTDATA_COST_PER_CALL", "0.0015"))


def estimate_cost(serper_calls: int, gemini_calls: int, brightdata_calls: int = 0) -> float:
    """Estimated paid-equivalent USD cost for a set of external API calls."""
    return round(serper_calls * SERPER_COST_PER_CALL
                 + gemini_calls * GEMINI_COST_PER_CALL
                 + brightdata_calls * BRIGHTDATA_COST_PER_CALL, 6)


def _split_ai_citation_calls(output: dict) -> tuple[int, int, int]:
    """Split ai_citation's combined api_calls_made into (serper, gemini, brightdata).

    live_ai_search counts Serper + Gemini + Bright Data calls together in
    api_calls_made. We recover the split from the results list: each live
    gemini_search row is a Gemini call; each live google_ai_overview row is a
    Bright Data SERP call (BD is now the authoritative AI-Overview engine).
    Serper is the remainder. Conservative + never negative.
    """
    total = int(output.get("api_calls_made", 0) or 0)
    if total <= 0:
        return 0, 0, 0
    results = output.get("results", []) or []
    gemini = sum(1 for r in results
                 if isinstance(r, dict) and r.get("engine") == "gemini_search")
    brightdata = sum(1 for r in results
                     if isinstance(r, dict) and r.get("engine") == "google_ai_overview")
    gemini = min(gemini, total)
    brightdata = min(brightdata, max(0, total - gemini))
    serper = max(0, total - gemini - brightdata)
    return serper, gemini, brightdata


def _extract_calls(key: str, output: dict) -> tuple[int, int, int]:
    """Return (serper_calls, gemini_calls, brightdata_calls) for one probe's output.

    Reads whatever the probe reported:
      - ai_citation: api_calls_made (mixed) → split into serper/gemini/brightdata
      - backlink / prompts / news: api_calls_used (Serper-only)
      - everything else: no reported counts → (0, 0, 0) honestly
    Probes that make Serper calls but do not expose a count (reviews/community/
    social/nap) are recorded as 0 rather than guessed — see _run_trace note.
    """
    if not isinstance(output, dict):
        return 0, 0, 0
    if key == "ai_citation":
        return _split_ai_citation_calls(output)
    serper = int(output.get("api_calls_used", 0) or 0)
    return serper, 0, 0

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
                                  brand: str | None,
                                  english_product_hint: str | None = None,
                                  use_case_hint: str | None = None) -> tuple[str, str]:
    """Derive an English product noun + use-case from the detected context.

    Brand-agnostic. Preference order:
      1. EXPLICIT english_product_hint from the on-site context detector
         (eac_trust_probe._detect_site_context now LLM-classifies industry +
         product into an on-category English buyer noun — generalizes to ANY
         industry without a keyword table). This is the robust path.
      2. Legacy keyword map over the (possibly Chinese) industry/product hints.
      3. Generic fallback.
    """
    # 1. Explicit on-category English noun from the detector (LLM or keyword).
    if english_product_hint and english_product_hint.strip():
        noun = english_product_hint.strip()
        # Treat the detector's own placeholder as "no signal" so we still try
        # the legacy map below before giving up.
        if noun.lower() != "products in this category":
            use_case = (use_case_hint or "").strip() or "general use"
            return noun, use_case

    # 2. Legacy keyword map over the raw (Chinese/English) hints.
    blob = " ".join(filter(None, [industry_hint, product_hint])).lower()
    for keywords, product, use_case in _PRODUCT_NOUN_MAP:
        if any(k.lower() in blob for k in keywords):
            return product, use_case

    # 3. Fallback: raw product hint if ASCII/English, else a generic noun.
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
                        geography: str = "United States",
                        english_product_hint: str | None = None,
                        use_case_hint: str | None = None) -> list[str]:
    """Build exactly 5 buyer-intent queries spanning 4 intent types.

    Templates (brand-agnostic). A blank ``geography`` yields broad-market
    queries (no geo suffix), which is correct for multi-region targets like
    "US,EU,SEA" where pinning to one country would skew the citation test.

      1. best {product} brands[ {geo}]          (discovery)
      2. {product} reviews[ {geo}]              (evaluation)
      3. {product} comparison                   (comparison)
      4. where to buy affordable {product}[ {geo}](transactional)
      5. best {product} for {use_case}          (problem-led)

    ``english_product_hint`` / ``use_case_hint`` (on-category English noun from
    the LLM/keyword detector) take precedence so queries stay on-category, e.g.
    "best AI cloud platforms" instead of "best products in this category".
    """
    product, use_case = _english_product_and_usecase(
        industry_hint, product_hint, brand,
        english_product_hint, use_case_hint)
    geo = (geography or "").strip()
    suffix = f" {geo}" if geo else ""
    # NOTE: short consumer-phrased "AIO-bait" queries were tried to coax Google
    # into showing an AI-Overview, but (a) Google still doesn't surface AIO for
    # this B2B category and (b) they surfaced an Answer-Box citation of a
    # brand-ADJACENT domain (emdoorrugged.com) that the engine's loose brand
    # match wrongly credited to the audited domain (emdoor.com), silently
    # flipping the headline P0. Reverted until the citation detector matches the
    # audited domain strictly (same fix family as the Perplexity in_sources gate).
    return [
        f"best {product} brands{suffix}",
        f"{product} reviews{suffix}",
        f"{product} comparison",
        f"where to buy affordable {product}{suffix}",
        f"best {product} for {use_case}",
    ]


def run_ai_citation(engine_root: Path, url: str, brand: str | None,
                    industry_hint: str | None = None,
                    product_hint: str | None = None,
                    geography: str = "United States",
                    region_code: str = "us",
                    english_product_hint: str | None = None,
                    use_case_hint: str | None = None) -> dict:
    """Run live_ai_search with EXPLICIT brand-relevant buyer-intent queries.

    Bypasses the generic template system (the `queries=` param overrides it
    entirely — see live_ai_search.run_citation_test line 469).
    """
    import asyncio
    from dataclasses import asdict

    mod = _load_probe_module(engine_root, "live_ai_search")
    queries = build_buyer_queries(
        industry_hint, product_hint, brand, geography,
        english_product_hint, use_case_hint)
    logger.info("ai_citation buyer queries: %s", queries)
    import inspect
    citation_kwargs = dict(
        target_url=url,
        brand_name=brand or "",
        # Pass the detected industry through; fall back to the generic default
        # only when classification produced nothing (avoids mislabeling a B2B
        # brand as dtc-ecommerce in any downstream scoring/labeling).
        industry=industry_hint or "dtc-ecommerce",
        geography=geography,
        region_code=region_code,
        queries=queries,
        enable_gemini=True,
        enable_serper=True,
        # BD SERP is the authoritative AI-Overview engine (gl=us); self-gates on
        # BRIGHTDATA_API_KEY in env, so this is a no-op when the key is absent.
        enable_brightdata=True,
    )
    # Cross-repo version skew: older pinned engines (canmarket-site-audit-v1.1)
    # predate the Bright Data passthrough and reject enable_brightdata. Drop any
    # kwarg the resolved engine's run_citation_test signature doesn't accept
    # rather than letting one unknown kwarg crash the whole AI-citation probe.
    # If the engine accepts **kwargs, pass everything through unfiltered — the
    # signature would expose only the var-keyword param and naive filtering would
    # wrongly strip every real argument.
    sig_params = inspect.signature(mod.run_citation_test).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig_params.values()):
        dropped = [k for k in citation_kwargs if k not in sig_params]
        if dropped:
            logger.info("ai_citation: engine %s does not accept %s; dropping",
                        engine_root, dropped)
        citation_kwargs = {k: v for k, v in citation_kwargs.items() if k in sig_params}
    report = asyncio.run(mod.run_citation_test(**citation_kwargs))
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
    # Put the engine's probes/ dir on sys.path so a path-loaded probe can import
    # its own siblings (e.g. live_ai_search's `import _geo_serp`, the Bright Data
    # AI-Overview source of truth). Without this the sibling import silently
    # fails, _GEO_SERP_OK stays False, and the BD AI-Overview engine self-gates
    # OFF even when BRIGHTDATA_API_KEY is set — 0 BD calls, AI-Overview missing.
    probes_dir = str(probe_path.parent)
    if probes_dir not in sys.path:
        sys.path.insert(0, probes_dir)
    spec.loader.exec_module(mod)
    return mod


# Probes whose probe() accepts a 3rd `industry_keywords` arg. For news/backlink
# it disambiguates same-name namesakes; for prompts it seeds long-tail/keyword
# discovery with the detected product/industry terms.
_INDUSTRY_KEYWORD_PROBES = {"news", "backlink", "prompts"}


def _run_one(engine_root: Path, key: str, module_name: str, fn_name: str,
             needs_brand: bool, url: str, brand: str | None,
             industry_hint: str | None = None,
             product_hint: str | None = None,
             geography: str = "United States",
             region_code: str = "us",
             english_product_hint: str | None = None,
             use_case_hint: str | None = None) -> tuple[str, dict, float]:
    """Run one probe; return (key, output, duration_s). Never raises."""
    t0 = time.monotonic()
    try:
        # ai_citation uses a dedicated runner with brand-relevant queries.
        if key == "ai_citation":
            output = run_ai_citation(
                engine_root, url, brand,
                industry_hint=industry_hint, product_hint=product_hint,
                geography=geography, region_code=region_code,
                english_product_hint=english_product_hint,
                use_case_hint=use_case_hint)
            return key, output, time.monotonic() - t0
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
        output = result if isinstance(result, dict) else {"value": result}
        return key, output, time.monotonic() - t0
    except Exception as e:  # noqa: BLE001 — one probe failing must not abort the run
        logger.warning("off-site probe %s failed: %s", module_name, e)
        return key, {"_probe_status": "error", "_reason": str(e)[:300]}, time.monotonic() - t0


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
                        product_hint: str | None,
                        geography: str = "United States",
                        english_product_hint: str | None = None,
                        use_case_hint: str | None = None) -> list[str]:
    """Pick brand-relevant buyer prompts for the Perplexity browser capture.

    Prefer the prompt_discovery buyer_prompts (real PAA + templated), else fall
    back to the same buyer queries the ai_citation probe uses (same geography).
    """
    prompts = offsite.get("prompts", {}) or {}
    bp = prompts.get("buyer_prompts", []) or []
    picked = [p.get("prompt") for p in bp if p.get("prompt")][:5]
    if picked:
        return picked
    return build_buyer_queries(
        industry_hint, product_hint, brand, geography,
        english_product_hint, use_case_hint)


def run_offsite_probes(url: str, brand: str | None = None,
                       max_workers: int = 3,
                       industry_hint: str | None = None,
                       product_hint: str | None = None,
                       geography: str = "United States",
                       region_code: str = "us",
                       english_product_hint: str | None = None,
                       use_case_hint: str | None = None) -> dict[str, dict]:
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
    run_t0 = time.monotonic()
    engine_root = locate_engine()
    # Put the engine root on sys.path so engine probes that do
    # `from probes import X` for their own siblings resolve correctly.
    if str(engine_root) not in sys.path:
        sys.path.append(str(engine_root))
    logger.info("using engine at %s", engine_root)

    # Per-probe trace rows: {probe, duration_s, serper_calls, gemini_calls, status}
    per_probe: list[dict] = []

    def _probe_status(output: dict) -> str:
        st = output.get("_probe_status")
        if st in ("error", "skipped"):
            return st
        return "ok"

    def _record(key: str, output: dict, duration_s: float) -> None:
        serper, gemini, brightdata = _extract_calls(key, output)
        per_probe.append({
            "probe": key,
            "duration_s": round(duration_s, 3),
            "serper_calls": serper,
            "gemini_calls": gemini,
            "brightdata_calls": brightdata,
            "status": _probe_status(output),
        })

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_run_one, engine_root, key, mod, fn, nb, url, brand,
                        industry_hint, product_hint, geography, region_code,
                        english_product_hint, use_case_hint)
            for (key, mod, fn, nb) in OFFSITE_PROBES
        ]
        for fut in as_completed(futures):
            key, output, duration_s = fut.result()
            results[key] = output
            _record(key, output, duration_s)
            status = output.get("_probe_status", "ok")
            logger.info("  [%s] %s (%.1fs)", key, status, duration_s)

    # --- crawlability: Playwright-sync, MUST run outside the pool (subprocess).
    logger.info("running crawlability_scan (Playwright, isolated subprocess)")
    c_t0 = time.monotonic()
    results["crawlability"] = _run_crawlability(engine_root, url)
    _record("crawlability", results["crawlability"], time.monotonic() - c_t0)
    logger.info("  [crawlability] %s",
                results["crawlability"].get("_probe_status",
                                            results["crawlability"].get("render_engine", "ok")))

    # --- perplexity_browser: Camoufox capture via the superscrape venv python.
    # Uses brand-relevant buyer prompts pulled from the prompts probe result.
    queries = _browser_ai_queries(results, url, brand, industry_hint,
                                  product_hint, geography,
                                  english_product_hint, use_case_hint)
    logger.info("running browser_ai_capture (Perplexity, venv subprocess): %s", queries)
    p_t0 = time.monotonic()
    results["perplexity_browser"] = _run_browser_ai(engine_root, url, brand, queries)
    _record("perplexity_browser", results["perplexity_browser"],
            time.monotonic() - p_t0)
    logger.info("  [perplexity_browser] %s",
                results["perplexity_browser"].get("_probe_status",
                                                  results["perplexity_browser"].get("engine", "ok")))

    # --- Assemble the run-trace summary (cost + timing + API-call counts).
    total_serper = sum(p["serper_calls"] for p in per_probe)
    total_gemini = sum(p["gemini_calls"] for p in per_probe)
    total_brightdata = sum(p.get("brightdata_calls", 0) for p in per_probe)
    probes_ok = sum(1 for p in per_probe if p["status"] == "ok")
    probes_failed = sum(1 for p in per_probe if p["status"] in ("error", "skipped"))
    results["_run_trace"] = {
        "total_duration_s": round(time.monotonic() - run_t0, 3),
        "per_probe": sorted(per_probe, key=lambda p: p["duration_s"], reverse=True),
        "total_serper_calls": total_serper,
        "total_gemini_calls": total_gemini,
        "total_brightdata_calls": total_brightdata,
        "est_cost_usd": estimate_cost(total_serper, total_gemini, total_brightdata),
        "probes_ok": probes_ok,
        "probes_failed": probes_failed,
        "note": ("serper_calls/gemini_calls reflect probe-reported counts; some "
                 "probes (reviews/community/social/nap) make Serper calls but do "
                 "not expose a count and show 0 here."),
    }
    return results
