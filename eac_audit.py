#!/usr/bin/env python3
"""
EAC Infrastructure Audit — runs all 4 probes and generates a Google EAC-branded report.

Usage:
    python eac_audit.py https://example.com --form-url https://example.com/contact \
        --thank-you-url https://example.com/thank-you \
        --product-page https://example.com/product/abc \
        --company "某某科技" --market US,EU

Output:
    output/{domain}/eac-audit-{date}.html
    output/{domain}/eac-audit-{date}.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

from scoring import compute_overall_score

TEMPLATE_DIR = Path(__file__).parent / "templates" / "google"


def _run_speed(url: str, markets: str = "全球") -> dict:
    from probes.eac_speed_probe import probe
    return probe(url, markets=markets)


def _run_tracking(url: str, form_url: str | None, thank_you_url: str | None) -> dict:
    from probes.eac_tracking_probe import probe
    return probe(url, form_url=form_url, thank_you_url=thank_you_url)


def _run_form(form_url: str) -> dict:
    from probes.eac_form_probe import probe
    return probe(form_url)


def _run_trust(url: str, product_page: str | None, company: str | None) -> dict:
    from probes.eac_trust_probe import probe
    return probe(url, product_page_url=product_page, company_name=company)


def _severity_order(s: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "PASS": 3}.get(s.upper(), 9)


def _compute_module_score(findings: list[dict], probe_status: str = "ok") -> int:
    """Compute module score. Returns -1 (N/A) for skipped/errored probes."""
    if probe_status in ("skipped", "error"):
        return -1
    if not findings:
        return 100
    deductions = {"P0": 30, "P1": 15, "P2": 5}
    total = sum(deductions.get(f.get("severity", "").upper(), 0) for f in findings)
    return max(0, 100 - total)


def run_audit(
    url: str,
    form_url: str | None = None,
    thank_you_url: str | None = None,
    product_page: str | None = None,
    company: str | None = None,
    markets: str = "全球",
) -> dict:
    """Run all 4 EAC probes and assemble report data."""
    print(f"[EAC] Starting audit for {url}")
    t0 = time.time()

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_run_speed, url, markets): "speed",
            pool.submit(_run_tracking, url, form_url, thank_you_url): "tracking",
        }
        if form_url:
            futures[pool.submit(_run_form, form_url)] = "form"
        else:
            results["form"] = {"_probe_status": "skipped", "findings": []}

        futures[pool.submit(_run_trust, url, product_page, company)] = "trust"

        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
                status = results[name].get("_probe_status", "unknown")
                print(f"  [{name}] {status}")
            except Exception as e:
                results[name] = {"_probe_status": "error", "_reason": str(e)[:300], "findings": []}
                print(f"  [{name}] ERROR: {e}")

    duration = time.time() - t0
    print(f"[EAC] All probes done in {duration:.1f}s")

    all_findings = []
    for probe_name in ["speed", "form", "trust", "tracking"]:
        for f in results.get(probe_name, {}).get("findings", []):
            f["_source_probe"] = probe_name
            all_findings.append(f)

    all_findings.sort(key=lambda f: _severity_order(f.get("severity", "P2")))

    p0 = [f for f in all_findings if f.get("severity", "").upper() == "P0"]
    p1 = [f for f in all_findings if f.get("severity", "").upper() == "P1"]
    p2 = [f for f in all_findings if f.get("severity", "").upper() == "P2"]
    passes = [f for f in all_findings if f.get("severity", "").upper() == "PASS"]

    speed_findings = [f for f in all_findings if f.get("_source_probe") == "speed"]
    form_findings = [f for f in all_findings if f.get("_source_probe") == "form"]
    trust_findings = [f for f in all_findings if f.get("_source_probe") == "trust"]
    tracking_findings = [f for f in all_findings if f.get("_source_probe") == "tracking"]

    # Exclude findings from skipped/errored probes when computing overall score
    _active_probes = {
        name for name in ["speed", "form", "trust", "tracking"]
        if results.get(name, {}).get("_probe_status") not in ("skipped", "error")
    }
    # overall_score is computed below from the assembled module list (see the
    # `modules = [...]` block) via the shared compute_overall_score helper —
    # weighted module-health average + bounded P0 cap, not a saturating subtraction.

    top_actions = []
    for f in (p0 + p1)[:3]:
        top_actions.append({
            "title": f.get("action_zh", f.get("title_zh", "")),
            "impact": f"{f.get('severity', 'P1')} · {f.get('impact_zh', '')}",
            "effort": "30 分钟" if f.get("code_snippet") else "需评估",
        })

    tracking_result = results.get("tracking", {})
    leakage_risk = tracking_result.get("leakage_risk", "low")
    leakage_desc = tracking_result.get("leakage_description", "")
    readiness_items = tracking_result.get("readiness_items", [])

    speed_data = results.get("speed", {})
    speed_table = None
    metrics = speed_data.get("metrics_summary")
    if metrics:
        speed_table = {
            "headers": ["指标", "数值", "阈值", "状态"],
            "rows": [],
        }
        ttfb = metrics.get("ttfb_desktop_ms")
        lcp = metrics.get("lcp_desktop_ms")
        if ttfb is not None:
            status = "✅ 达标" if ttfb <= 2000 else "❌ 超标"
            speed_table["rows"].append(["TTFB (Desktop)", f"{ttfb:,.0f} ms", "≤ 2,000 ms", status])
        if lcp is not None:
            status = "✅ 达标" if lcp <= 2500 else "❌ 超标"
            speed_table["rows"].append(["LCP (Desktop)", f"{lcp:,.0f} ms", "≤ 2,500 ms", status])

        ttfb_m = metrics.get("ttfb_mobile_ms")
        lcp_m = metrics.get("lcp_mobile_ms")
        if ttfb_m is not None:
            status = "✅ 达标" if ttfb_m <= 2000 else "❌ 超标"
            speed_table["rows"].append(["TTFB (Mobile)", f"{ttfb_m:,.0f} ms", "≤ 2,000 ms", status])
        if lcp_m is not None:
            status = "✅ 达标" if lcp_m <= 2500 else "❌ 超标"
            speed_table["rows"].append(["LCP (Mobile)", f"{lcp_m:,.0f} ms", "≤ 2,500 ms", status])

        cdn = speed_data.get("cdn_detected")
        if cdn:
            speed_table["rows"].append(["CDN", cdn.title(), "—", "✅ 已配置"])
        else:
            speed_table["rows"].append(["CDN", "未检测到", "—", "❌ 缺失"])

        country = speed_data.get("server_country", "未知")
        speed_table["rows"].append(["服务器位置", country, "—", "—"])

    speed_summary = ""
    if speed_data.get("cdn_detected"):
        speed_summary = f"<p>已检测到 {speed_data['cdn_detected'].title()} CDN 服务。</p>"
    else:
        speed_summary = "<p>未检测到 CDN 服务，海外用户访问延迟可能较高。</p>"

    form_result = results.get("form", {})
    form_summary = ""
    captcha = form_result.get("captcha_detected")
    forms_count = form_result.get("forms_found", 0)
    if forms_count == 0:
        form_summary = "<p>未在页面上检测到表单。</p>"
    elif captcha:
        form_summary = f"<p>检测到 {forms_count} 个表单，已安装 {captcha} 保护。</p>"
    else:
        form_summary = f"<p>检测到 {forms_count} 个表单，但缺少反垃圾保护。</p>"

    trust_result = results.get("trust", {})
    has_blog = trust_result.get("has_blog", False)
    is_https = trust_result.get("is_https", True)
    trust_summary = "<p>"
    if is_https:
        trust_summary += "网站已启用 HTTPS 加密。"
    else:
        trust_summary += "⚠️ 网站未启用 HTTPS。"
    if has_blog:
        trust_summary += "已检测到博客/内容板块。"
    else:
        trust_summary += "未检测到博客/资讯内容板块。"
    trust_summary += "</p>"

    # Extract rich recommendation data from probes for embedded deliverables
    trust_recommendations = trust_result.get("recommendations", {})
    form_best_practices = form_result.get("form_best_practices", {})

    tracking_tags = tracking_result.get("tags_detected", {})
    tracking_summary = "<p>"
    detected_tags = [name for name, found in tracking_tags.items() if found]
    if detected_tags:
        tracking_summary += f"已检测到: {', '.join(detected_tags)}。"
    else:
        tracking_summary += "未检测到任何追踪代码。"
    tracking_summary += f" 广告预算追踪风险: {leakage_risk.upper()}。</p>"

    domain = urlparse(url).netloc or "unknown"
    brand = company or domain

    # Map probe names to their statuses for scoring
    _probe_statuses = {
        name: results.get(name, {}).get("_probe_status", "unknown")
        for name in ["speed", "form", "trust", "tracking"]
    }

    modules = [
        {
            "icon": "🚀",
            "title_zh": "全球网站访问加速",
            "title_en": "Global Website Speed Optimization",
            "score": _compute_module_score(speed_findings, _probe_statuses["speed"]),
            "summary_html": speed_summary,
            "data_table": speed_table,
            "findings": speed_findings,
        },
        {
            "icon": "📝",
            "title_zh": "留资页面检查",
            "title_en": "Lead Form Security Audit",
            "score": _compute_module_score(form_findings, _probe_statuses["form"]),
            "summary_html": form_summary,
            "data_table": None,
            "findings": form_findings,
        },
        {
            "icon": "🔍",
            "title_zh": "信任度内容梳理",
            "title_en": "Trust Content Audit",
            "score": _compute_module_score(trust_findings, _probe_statuses["trust"]),
            "summary_html": trust_summary,
            "data_table": None,
            "findings": trust_findings,
        },
        {
            "icon": "📊",
            "title_zh": "表单提交数据追踪",
            "title_en": "Conversion Tracking Audit",
            "score": _compute_module_score(tracking_findings, _probe_statuses["tracking"]),
            "summary_html": tracking_summary,
            "data_table": None,
            "findings": tracking_findings,
        },
    ]

    overall_score = compute_overall_score(modules)

    report_data = {
        "brand_name": brand,
        "target_url": url,
        "audit_date": date.today().strftime("%Y-%m-%d"),
        "target_markets": markets,
        "overall_score": overall_score,
        "p0_count": len(p0),
        "p1_count": len(p1),
        "p2_count": len(p2),
        "pass_count": len(passes),
        "leakage_risk": leakage_risk,
        "leakage_description": leakage_desc,
        "top_actions": top_actions,
        "modules": modules,
        "readiness_items": readiness_items,
        "next_steps_text": f"本报告中标记为 P0 的 {len(p0)} 个问题建议在 7 天内优先处理。如需协助实施，请联系我们的技术团队。",
        "contact_email": "admin@canlah.ai",
        "contact_wechat": "CanlahAI",
        "contact_phone": "",
        "recommendations": trust_recommendations,
        "form_best_practices": form_best_practices,
        "_raw_probes": results,
        "_duration_seconds": duration,
    }

    return report_data


def render_report(data: dict, output_dir: Path, generate_pdf: bool = True) -> tuple[Path, Path, Path | None]:
    """Render HTML report, save JSON, and optionally generate PDF."""
    output_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    tmpl = env.get_template("report.html.j2")
    html = tmpl.render(**{k: v for k, v in data.items() if not k.startswith("_")})

    audit_date = data.get("audit_date", date.today().strftime("%Y-%m-%d"))
    html_path = output_dir / f"eac-audit-{audit_date}.html"
    html_path.write_text(html, encoding="utf-8")

    json_data = {k: v for k, v in data.items() if k != "_raw_probes"}
    json_path = output_dir / f"eac-audit-{audit_date}.json"
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    pdf_path = None
    if generate_pdf:
        try:
            from render.html_to_pdf import html_to_pdf
            pdf_path = html_to_pdf(html_path)
        except Exception as e:
            print(f"  [PDF] generation failed: {e}")

    return html_path, json_path, pdf_path


def main():
    parser = argparse.ArgumentParser(description="EAC Infrastructure Audit")
    parser.add_argument("url", help="Target website URL")
    parser.add_argument("--form-url", help="Lead form page URL")
    parser.add_argument("--thank-you-url", help="Thank You page URL")
    parser.add_argument("--product-page", help="Product detail page URL")
    parser.add_argument("--company", help="Company name (Chinese)")
    parser.add_argument("--market", default="全球", help="Target markets (e.g., US,EU,SEA)")
    parser.add_argument("--output-dir", help="Output directory (default: output/{domain})")
    parser.add_argument("--open", action="store_true", help="Open HTML in browser after generation")
    args = parser.parse_args()

    url = args.url
    if not urlparse(url).scheme:
        url = "https://" + url

    data = run_audit(
        url=url,
        form_url=args.form_url,
        thank_you_url=args.thank_you_url,
        product_page=args.product_page,
        company=args.company,
        markets=args.market,
    )

    domain = urlparse(url).netloc or "unknown"
    output_dir = Path(args.output_dir) if args.output_dir else Path("output") / domain

    html_path, json_path, pdf_path = render_report(data, output_dir)

    print(f"\n{'=' * 60}")
    print(f"  EAC AUDIT COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Score:    {data['overall_score']}/100")
    print(f"  P0:       {data['p0_count']} critical")
    print(f"  P1:       {data['p1_count']} warnings")
    print(f"  P2:       {data['p2_count']} info")
    print(f"  Leakage:  {data['leakage_risk'].upper()}")
    print(f"  Duration: {data.get('_duration_seconds', 0):.1f}s")
    print(f"{'=' * 60}")
    print(f"  HTML: {html_path}")
    if pdf_path:
        print(f"  PDF:  {pdf_path}")
    print(f"  JSON: {json_path}")
    print(f"{'=' * 60}")

    if args.open:
        import subprocess
        subprocess.run(["open", str(html_path)])

    return 0


if __name__ == "__main__":
    sys.exit(main())
