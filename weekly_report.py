#!/usr/bin/env python3
"""Signed weekly agent report generator (presentation-only).

Unlike report.py — which runs the live audit probe pipeline — this renderer
takes a hand-authored / machine-compiled JSON contribution contract and renders
it through the `weekly-agent-report` template into a client-facing PDF.

    DATA (weekly JSON contract) ─→ Jinja2 (templates/weekly-agent-report) ─→ HTML/PDF

The output folder is kept clean per the deliverable-folder rule: only the final
client-facing PDF lives at the top level; HTML / JSON / intermediates go into a
`_source/` subfolder. Superseded PDFs of the same client+deliverable are removed
AFTER the new PDF is verified to exist.

Usage:
    python weekly_report.py output/weekly-demo/_source/modernshade-week-2026-06-05.json \
        --out output/weekly-demo \
        --pdf-name ModernShade-Weekly-Report-2026-06-05.pdf
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
TEMPLATE_NAME = "weekly-agent-report"


def render_html(data: dict) -> str:
    tmpl_dir = TEMPLATES_DIR / TEMPLATE_NAME
    if not (tmpl_dir / "report.html.j2").is_file():
        raise FileNotFoundError(
            f"template '{TEMPLATE_NAME}' not found at {tmpl_dir}/report.html.j2")
    env = Environment(loader=FileSystemLoader(str(tmpl_dir)), autoescape=True)
    tmpl = env.get_template("report.html.j2")
    # Drop reserved (underscore-prefixed) keys, mirroring report.py's convention.
    return tmpl.render(**{k: v for k, v in data.items() if not k.startswith("_")})


def main() -> int:
    parser = argparse.ArgumentParser(description="Signed weekly agent report generator")
    parser.add_argument("contract", help="Path to the weekly contribution JSON contract")
    parser.add_argument("--out", required=True, help="Output folder (top level holds final PDF)")
    parser.add_argument("--pdf-name", required=True,
                        help="Client-facing PDF filename (no codename / version suffix)")
    args = parser.parse_args()

    contract_path = Path(args.contract).resolve()
    if not contract_path.is_file():
        parser.error(f"contract not found: {contract_path}")
    data = json.loads(contract_path.read_text(encoding="utf-8"))

    out_dir = Path(args.out).resolve()
    source_dir = out_dir / "_source"
    out_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    # 1. Render HTML → write the intermediate into _source/ (process file).
    html = render_html(data)
    stem = Path(args.pdf_name).stem
    html_path = source_dir / f"{stem}.html"
    html_path.write_text(html, encoding="utf-8")

    # 2. Render PDF → top level (the only user-facing file there).
    pdf_path = out_dir / args.pdf_name
    from render.html_to_pdf import html_to_pdf
    html_to_pdf(html_path, pdf_path)

    # 3. Verify the new PDF exists & is non-trivial BEFORE deleting older ones.
    if not pdf_path.is_file() or pdf_path.stat().st_size < 10_000:
        print(f"ERROR: rendered PDF missing or too small: {pdf_path}", file=sys.stderr)
        return 1

    # 4. Clean superseded PDFs: any *.pdf at top level that is not the new one.
    removed = []
    for old in out_dir.glob("*.pdf"):
        if old.resolve() != pdf_path.resolve():
            old.unlink()
            removed.append(old.name)

    print(f"\n{'=' * 60}")
    print(f"  WEEKLY REPORT — {data.get('brand_name', '?')}")
    print(f"{'=' * 60}")
    print(f"  PDF (final):  {pdf_path}")
    print(f"  HTML (source):{html_path}")
    print(f"  Contract:     {contract_path}")
    if removed:
        print(f"  Removed superseded PDFs: {', '.join(removed)}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
