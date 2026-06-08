#!/usr/bin/env python3
"""One-command audit report generator.

Runs the full pipeline end to end:

    on-site infra audit (eac_audit)  ┐
                                     ├─→ contract ─→ template ─→ HTML/PDF
    off-site probes (engine)         ┘

The presentation layer is template-driven: `--template google` renders the
Google EAC-branded report; drop a new folder under templates/ to add your
own (e.g. templates/canmarket/) and select it with --template canmarket.
The data layer never changes when you swap templates.

Usage:
    python report.py https://example.com --template google --format pdf \
        --brand "Example Corp" --market US,EU,SEA

    # on-site only (skip off-site probes / no Serper calls):
    python report.py https://example.com --no-offsite
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

import contract as contract_mod
import engine as engine_mod
from eac_audit import run_audit
from probes.understand_business import understand_business

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("report")

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Map the FIRST target-market token → (geography label, probe region_code).
# region_code must be one the live_ai_search probe supports: sg/us/uk/au/eu.
_MARKET_GEO = {
    "US": ("United States", "us"),
    "USA": ("United States", "us"),
    "EU": ("Europe", "eu"),
    "EUROPE": ("Europe", "eu"),
    "SEA": ("Southeast Asia", "sg"),
    "SG": ("Singapore", "sg"),
    "SINGAPORE": ("Singapore", "sg"),
    "UK": ("United Kingdom", "uk"),
    "GB": ("United Kingdom", "uk"),
    "AU": ("Australia", "au"),
    "AUSTRALIA": ("Australia", "au"),
}


def _derive_geography(market: str | None) -> tuple[str, str]:
    """Derive (geography, region_code) from the FIRST target-market token.

    Defaults to ("United States", "us") for unknown/blank markets (incl. the
    "全球" default), so the AI-citation queries are never silently pinned to
    Singapore when the brand actually targets US/EU/SEA.
    """
    first = (market or "").split(",")[0].strip().upper()
    return _MARKET_GEO.get(first, ("United States", "us"))


def resolve_canonical_url(url: str) -> str:
    """Follow redirects to find the canonical/final URL for a target site.

    Many apex hosts 301-redirect to www (e.g. cloudsway.ai → www.cloudsway.ai),
    where the real content (e.g. /blog) lives. Probing the apex host directly
    produces false negatives (no blog, generic industry fallback). We resolve
    the final URL once here and pass it to EVERY probe so they all hit www.

    A HEAD is tried first (cheap); if the server rejects HEAD we fall back to a
    GET. On any failure we return the original url unchanged.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        import requests
        try:
            resp = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
            # Some servers don't support HEAD (405/501) — retry with GET.
            if resp.status_code >= 400:
                resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        except requests.RequestException:
            resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        final = resp.url or url
    except Exception as exc:  # noqa: BLE001 — resolution must never abort the run
        logger.warning("canonical host resolve failed for %s (%s); using original", url, exc)
        return url

    if urlparse(final).netloc != urlparse(url).netloc:
        logger.info("canonical host resolved: %s → %s", url, final)
    return final


def list_templates() -> list[str]:
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(
        p.name for p in TEMPLATES_DIR.iterdir()
        if p.is_dir() and (p / "report.html.j2").is_file()
    )


def render_html(data: dict, template: str) -> str:
    tmpl_dir = TEMPLATES_DIR / template
    if not (tmpl_dir / "report.html.j2").is_file():
        avail = list_templates()
        raise FileNotFoundError(
            f"template '{template}' not found (looked for {tmpl_dir}/report.html.j2). "
            f"Available: {avail or 'none'}")
    env = Environment(loader=FileSystemLoader(str(tmpl_dir)), autoescape=True)
    tmpl = env.get_template("report.html.j2")
    return tmpl.render(**{k: v for k, v in data.items() if not k.startswith("_")})


def main() -> int:
    parser = argparse.ArgumentParser(description="One-command audit report generator")
    parser.add_argument("url", nargs="?", help="Target website URL")
    parser.add_argument("--template", default="google", help="Presentation template (default: google)")
    parser.add_argument("--format", choices=["html", "pdf", "both"], default="both",
                        help="Output format (default: both)")
    parser.add_argument("--brand", "--company", dest="brand", help="Brand / company name")
    parser.add_argument("--market", default="全球", help="Target markets, comma-separated")
    parser.add_argument("--form-url", help="Lead form page URL (on-site form probe)")
    parser.add_argument("--thank-you-url", help="Thank-you page URL (conversion tracking)")
    parser.add_argument("--product-page", help="Product detail page URL (trust probe)")
    parser.add_argument("--owned-domains", default="",
                        help="Comma-separated brand-owned alt/regional domains to "
                             "exclude from competitor/mention/citation counting "
                             "(e.g. acme.cn,acme-global.com)")
    parser.add_argument("--no-offsite", action="store_true", help="Skip off-site probes")
    parser.add_argument("--output-dir", help="Output dir (default: output/{domain})")
    parser.add_argument("--open", action="store_true", help="Open the report after generation")
    parser.add_argument("--list-templates", action="store_true", help="List templates and exit")
    args = parser.parse_args()

    if args.list_templates:
        print("Available templates:", ", ".join(list_templates()) or "none")
        return 0
    if not args.url:
        parser.error("url is required (or use --list-templates)")

    raw_url = args.url if urlparse(args.url).scheme else "https://" + args.url
    # Resolve the canonical/final URL by following redirects so every probe hits
    # the real host (e.g. apex cloudsway.ai → www.cloudsway.ai where /blog lives).
    url = resolve_canonical_url(raw_url)
    domain = urlparse(url).netloc or "unknown"
    brand = args.brand or domain

    # 1. On-site infra audit (4 base modules)
    logger.info("running on-site infra audit for %s", url)
    base = run_audit(
        url=url, form_url=args.form_url, thank_you_url=args.thank_you_url,
        product_page=args.product_page, company=brand, markets=args.market)

    # 1.5 Deep business understanding — read multiple pages to learn what the
    # company ACTUALLY does (ALL segments), BEFORE auditing. Drives clean,
    # segment-spanning AI-citation categories + a "what you do" report section.
    # Best-effort: a failure here never blocks the audit.
    logger.info("deep business-understanding read for %s", url)
    try:
        business_profile = understand_business(url)
    except Exception as e:  # noqa: BLE001
        logger.warning("business understanding failed: %s", e)
        business_profile = None
    if business_profile:
        logger.info("business: %d pages read, segments=%s, categories=%s",
                    business_profile.get("_pages_read", 0),
                    business_profile.get("segments"),
                    business_profile.get("canonical_categories"))

    # 2. Off-site probes (3 extra modules) via the engine
    if args.no_offsite:
        logger.info("off-site probes skipped (--no-offsite)")
        data = base
        data.setdefault("audit_date", date.today().strftime("%Y-%m-%d"))
    else:
        logger.info("running off-site probes via engine")
        # Pass the detected industry/product hint so ai_citation runs
        # BRAND-RELEVANT buyer-intent queries (not generic Singapore ones).
        detected = (base.get("recommendations", {}) or {}).get("detected_context", {}) or {}
        geography, region_code = _derive_geography(args.market)
        logger.info("AI-citation geography=%s region_code=%s (from market=%s)",
                    geography, region_code, args.market)
        # Prefer the deep business-understanding profile (all segments) over the
        # shallow homepage one-shot for the AI-citation query category.
        bp = business_profile or {}
        offsite = engine_mod.run_offsite_probes(
            url, brand,
            industry_hint=detected.get("industry"),
            product_hint=detected.get("product"),
            geography=geography, region_code=region_code,
            english_product_hint=(bp.get("primary_category")
                                  or detected.get("english_product")),
            use_case_hint=detected.get("use_case"),
            categories=bp.get("canonical_categories"))
        # Pop the run-trace out of the probe results before building the contract
        # so it never leaks into a rendered module; we attach it under
        # data["_run_trace"] (a reserved, non-rendered key) ourselves.
        run_trace = offsite.pop("_run_trace", None)
        owned = [d.strip() for d in (args.owned_domains or "").split(",") if d.strip()]
        if owned:
            logger.info("excluding brand-owned domains from counting: %s", owned)
        data = contract_mod.build_contract(base, offsite, owned_domains=owned)
        if run_trace is not None:
            run_trace["generated_at"] = datetime.now(timezone.utc).isoformat()
            data["_run_trace"] = run_trace

    # Attach the business-understanding profile so the template can open with a
    # "what this company actually does" section.
    if business_profile:
        data["business_profile"] = business_profile

    # 3. Render via selected template
    logger.info("rendering with template '%s'", args.template)
    html = render_html(data, args.template)

    out_dir = Path(args.output_dir) if args.output_dir else BASE_DIR / "output" / domain
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_date = data.get("audit_date", date.today().strftime("%Y-%m-%d"))
    stem = f"{args.template}-audit-{audit_date}"

    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # Ops sidecar: write the run-trace (cost + timing + API calls) for ops, and
    # append a single line to the persistent cross-run ledger. Best-effort —
    # never break the report if these writes fail.
    run_trace = data.get("_run_trace")
    if run_trace:
        try:
            trace_dir = out_dir / "_source"
            trace_dir.mkdir(parents=True, exist_ok=True)
            trace_path = trace_dir / f"{stem}-trace.json"
            trace_path.write_text(
                json.dumps(run_trace, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        except OSError as e:
            logger.warning("trace sidecar write failed: %s", e)
        try:
            ledger_path = BASE_DIR / "output" / "_runs.jsonl"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_line = json.dumps({
                "generated_at": run_trace.get("generated_at"),
                "domain": domain,
                "date": audit_date,
                "duration_s": run_trace.get("total_duration_s"),
                "est_cost_usd": run_trace.get("est_cost_usd"),
                "serper_calls": run_trace.get("total_serper_calls"),
                "gemini_calls": run_trace.get("total_gemini_calls"),
                "brightdata_calls": run_trace.get("total_brightdata_calls"),
                "probes_ok": run_trace.get("probes_ok"),
                "probes_failed": run_trace.get("probes_failed"),
            }, ensure_ascii=False, default=str)
            with ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(ledger_line + "\n")
        except OSError as e:
            logger.warning("runs ledger append failed: %s", e)

    html_path = pdf_path = None
    if args.format in ("html", "both"):
        html_path = out_dir / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
    if args.format in ("pdf", "both"):
        # Write HTML to a temp/real path first (WeasyPrint reads from file for relative assets)
        src_html = html_path or (out_dir / f"{stem}.html")
        if html_path is None:
            src_html.write_text(html, encoding="utf-8")
        try:
            from render.html_to_pdf import html_to_pdf
            pdf_path = html_to_pdf(src_html, out_dir / f"{stem}.pdf")
        except Exception as e:  # noqa: BLE001
            logger.warning("PDF generation failed: %s", e)
        if html_path is None:
            src_html.unlink(missing_ok=True)  # was only a scratch file for PDF

    # Core probes whose silent failure makes the whole report misleading (the
    # GEO money-shot). If one errors, we fail LOUD + exit non-zero instead of
    # shipping a complete-looking report that quietly dropped its core module.
    CORE_PROBES = {"ai_citation"}
    core_degraded = False

    print(f"\n{'=' * 60}")
    print(f"  AUDIT COMPLETE — {brand}  (template: {args.template})")
    print(f"{'=' * 60}")
    print(f"  Score:   {data.get('overall_score', '?')}/100")
    print(f"  P0/P1/P2: {data.get('p0_count', 0)}/{data.get('p1_count', 0)}/{data.get('p2_count', 0)}")
    print(f"  Modules: {len(data.get('modules', []))}")
    print(f"{'=' * 60}")
    if html_path:
        print(f"  HTML: {html_path}")
    if pdf_path:
        print(f"  PDF:  {pdf_path}")
    print(f"  JSON: {json_path}")
    print(f"{'=' * 60}")

    # --- Run trace: cost + timing + API calls (printed every run) ----------
    if run_trace:
        per_probe = run_trace.get("per_probe", []) or []
        print(f"  RUN TRACE")
        print(f"  Total time:  {run_trace.get('total_duration_s', '?')}s")
        print(f"  Est. cost:   ${run_trace.get('est_cost_usd', 0):.4f} "
              f"({run_trace.get('total_serper_calls', 0)} Serper + "
              f"{run_trace.get('total_gemini_calls', 0)} Gemini + "
              f"{run_trace.get('total_brightdata_calls', 0)} BrightData calls)")
        print(f"  Probes:      {run_trace.get('probes_ok', 0)} ok / "
              f"{run_trace.get('probes_failed', 0)} failed")
        slowest = per_probe[:3]  # already sorted slowest-first by the engine
        if slowest:
            print(f"  Slowest 3:")
            for p in slowest:
                print(f"    - {p['probe']:<20} {p['duration_s']:>6.1f}s  "
                      f"({p['serper_calls']}S/{p['gemini_calls']}G/{p.get('brightdata_calls', 0)}B, {p['status']})")
        failed = [p for p in per_probe if p["status"] in ("error", "skipped")]
        if failed:
            print(f"  Failed/skipped:")
            for p in failed:
                print(f"    - {p['probe']:<20} {p['status']}")
        core_degraded = any(p["probe"] in CORE_PROBES and p["status"] == "error"
                            for p in per_probe)
        print(f"  Trace JSON:  {out_dir / '_source' / (stem + '-trace.json')}")
        print(f"  Ledger:      {BASE_DIR / 'output' / '_runs.jsonl'}")
        print(f"{'=' * 60}")

    if core_degraded:
        print(f"\n{'!' * 60}")
        print(f"  ⚠️  DEGRADED REPORT — a CORE probe errored "
              f"({', '.join(sorted(CORE_PROBES))}).")
        print(f"  The AI-citation module is the GEO money-shot; its absence "
              f"makes this report MISLEADING. Do NOT send to a client.")
        print(f"  Re-run after fixing the probe. Exiting non-zero.")
        print(f"{'!' * 60}")

    if args.open:
        import subprocess
        target = pdf_path or html_path or json_path
        subprocess.run(["open", str(target)])
    return 2 if core_degraded else 0


if __name__ == "__main__":
    sys.exit(main())
