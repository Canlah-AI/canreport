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
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

import contract as contract_mod
import engine as engine_mod
from eac_audit import run_audit

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

    url = args.url if urlparse(args.url).scheme else "https://" + args.url
    domain = urlparse(url).netloc or "unknown"
    brand = args.brand or domain

    # 1. On-site infra audit (4 base modules)
    logger.info("running on-site infra audit for %s", url)
    base = run_audit(
        url=url, form_url=args.form_url, thank_you_url=args.thank_you_url,
        product_page=args.product_page, company=brand, markets=args.market)

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
        offsite = engine_mod.run_offsite_probes(
            url, brand,
            industry_hint=detected.get("industry"),
            product_hint=detected.get("product"),
            geography=geography, region_code=region_code)
        data = contract_mod.build_contract(base, offsite)

    # 3. Render via selected template
    logger.info("rendering with template '%s'", args.template)
    html = render_html(data, args.template)

    out_dir = Path(args.output_dir) if args.output_dir else BASE_DIR / "output" / domain
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_date = data.get("audit_date", date.today().strftime("%Y-%m-%d"))
    stem = f"{args.template}-audit-{audit_date}"

    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

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

    if args.open:
        import subprocess
        target = pdf_path or html_path or json_path
        subprocess.run(["open", str(target)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
