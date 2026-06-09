#!/usr/bin/env python3
"""
EAC tracking probe — conversion tracking & Google ecosystem readiness audit.

Checks GA4, GTM, Google Ads, Meta Pixel, Consent Mode v2, Enhanced Conversions,
and conversion events across homepage, form page, and thank-you page.
Calculates ad spend leakage risk and returns structured findings.

Dependencies: requests (already in project).
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 12.0

# Tracking-tag and feature detectors now come from the external signature
# registry (probes/detection_signatures.json). Falls back to bundled defaults
# when the JSON is absent — behavior identical to the old inline lists (path-
# context anchored, V3 lesson), with added non-Western/modern ad+analytics
# vendors (TikTok/Pinterest/LinkedIn/Reddit/Snap pixels, Bing UET, Baidu
# Tongji, Plausible, Umami, Matomo, Clarity, Hotjar).
try:
    from probes.signatures_config import get_tag_detectors, get_feature_detectors
except ImportError:
    from signatures_config import get_tag_detectors, get_feature_detectors  # type: ignore[no-redef]

TAG_DETECTORS: list[tuple[str, str, str]] = get_tag_detectors()
FEATURE_DETECTORS: list[tuple[str, str, str]] = get_feature_detectors()

# PDF / download link detector.
PDF_LINK_RE = re.compile(
    r"""<a\s[^>]*href\s*=\s*["']([^"']*\.(?:pdf|zip|doc|docx|xlsx|pptx?))["'][^>]*>""",
    re.IGNORECASE,
)
DOWNLOAD_EVENT_RE = re.compile(
    r"""gtag\s*\(\s*['"]event['"]\s*,\s*['"](?:file_download|generate_lead|download)['"]""",
    re.IGNORECASE,
)


def _fetch(url: str) -> tuple[str, str, int]:
    """Fetch URL. Returns (final_url, body, status_code). Raises on hard errors."""
    resp = requests.get(
        url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True,
    )
    return resp.url, resp.text, resp.status_code


def _detect_tags(html: str) -> dict[str, dict[str, Any]]:
    """Run all tag detectors against HTML. Returns {key: {found, matched_text}}."""
    results: dict[str, dict[str, Any]] = {}
    for key, label, pattern in TAG_DETECTORS:
        m = re.search(pattern, html)
        results[key] = {
            "key": key,
            "label": label,
            "found": m is not None,
            "matched_text": m.group(0)[:120] if m else None,
        }
    return results


def _detect_features(html: str) -> dict[str, dict[str, Any]]:
    """Run advanced feature detectors against HTML. Returns {key: {found, ...}}."""
    results: dict[str, dict[str, Any]] = {}
    for key, label, pattern in FEATURE_DETECTORS:
        m = re.search(pattern, html)
        results[key] = {
            "key": key,
            "label": label,
            "found": m is not None,
            "matched_text": m.group(0)[:120] if m else None,
        }
    return results


def _detect_pdf_links(html: str) -> list[str]:
    """Find PDF/document download links in HTML."""
    return [m.group(1) for m in PDF_LINK_RE.finditer(html)]


def _extract_tag_ids(html: str) -> dict[str, list[str]]:
    """Extract specific tag IDs (GA4 G-xxx, GTM GTM-xxx, AW-xxx)."""
    return {
        "ga4_ids": re.findall(r"G-[A-Z0-9]{8,12}\b", html),
        "gtm_ids": re.findall(r"GTM-[A-Z0-9]{5,9}\b", html),
        "ads_ids": re.findall(r"AW-[0-9]{8,12}\b", html),
    }


def _calculate_leakage(
    has_ads_tag: bool,
    has_conversion_on_thankyou: bool,
    has_consent_mode: bool,
) -> tuple[str, str]:
    """Calculate ad spend leakage risk. Returns (level, description)."""
    if has_ads_tag and not has_conversion_on_thankyou:
        return "high", (
            "检测到 Google Ads 广告代码，但 Thank You 页面没有转化追踪。"
            "您的广告点击无法关联到实际询盘/订单，预算效率完全不可衡量。"
            "建议立即在 Thank You 页面部署转化追踪代码。"
        )
    if has_ads_tag and has_conversion_on_thankyou and not has_consent_mode:
        return "medium", (
            "广告转化追踪已部署，但未配置 Consent Mode v2。"
            "来自欧盟/英国的流量在用户拒绝 cookie 后将丢失转化数据，"
            "可能导致 Google Ads 智能出价模型偏差 15-30%。"
        )
    if has_ads_tag and has_conversion_on_thankyou and has_consent_mode:
        return "low", (
            "广告追踪体系完整: Google Ads 转化追踪 + Consent Mode v2 均已配置。"
            "建议进一步验证 Enhanced Conversions 是否启用以提升归因精度。"
        )
    if not has_ads_tag:
        return "low", (
            "未检测到 Google Ads 广告代码。如果您目前未投放 Google Ads，"
            "则不存在广告预算泄漏风险。"
        )
    return "low", "追踪状态正常。"


def _build_readiness(
    tags_home: dict[str, dict[str, Any]],
    features_all: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Build Google ecosystem readiness checklist."""
    items: list[dict[str, str]] = []

    # GA4
    ga4 = tags_home.get("ga4", {})
    items.append({
        "name": "Google Analytics 4",
        "status": "ready" if ga4.get("found") else "not-ready",
        "description": (
            f"已检测到 GA4 代码 ({ga4.get('matched_text', '')})"
            if ga4.get("found")
            else "首页未检测到 GA4 代码 (googletagmanager.com/gtag/js?id=G-xxx)"
        ),
    })

    # GTM
    gtm = tags_home.get("gtm", {})
    items.append({
        "name": "Google Tag Manager",
        "status": "ready" if gtm.get("found") else "not-ready",
        "description": (
            f"已检测到 GTM 容器 ({gtm.get('matched_text', '')})"
            if gtm.get("found")
            else "未检测到 GTM 容器。建议通过 GTM 管理所有追踪代码，方便后续维护。"
        ),
    })

    # Consent Mode v2
    cm = features_all.get("consent_mode_v2", {})
    items.append({
        "name": "Consent Mode v2",
        "status": "ready" if cm.get("found") else "not-ready",
        "description": (
            "已配置 Consent Mode v2 默认设置"
            if cm.get("found")
            else (
                "未检测到 Consent Mode v2 配置。"
                "2024 年 3 月起 Google 要求欧盟流量必须实施 Consent Mode v2。"
            )
        ),
    })

    # Enhanced Conversions
    ec = features_all.get("enhanced_conversions", {})
    items.append({
        "name": "Enhanced Conversions",
        "status": "ready" if ec.get("found") else "not-ready",
        "description": (
            "已配置 Enhanced Conversions (user_data 设置)"
            if ec.get("found")
            else (
                "未检测到 Enhanced Conversions 配置。"
                "启用后可通过 first-party 用户数据 (邮箱/电话) 提升 5-17% 归因精度。"
            )
        ),
    })

    # reCAPTCHA
    recaptcha_any = any(
        features_all.get(k, {}).get("found")
        for k in ("recaptcha_v2", "recaptcha_v3", "recaptcha_enterprise", "turnstile")
    )
    if recaptcha_any:
        found_names = [
            features_all[k]["label"]
            for k in ("recaptcha_v2", "recaptcha_v3", "recaptcha_enterprise", "turnstile")
            if features_all.get(k, {}).get("found")
        ]
        items.append({
            "name": "reCAPTCHA / Turnstile",
            "status": "ready",
            "description": f"已检测到反垃圾保护: {', '.join(found_names)}",
        })
    else:
        items.append({
            "name": "reCAPTCHA / Turnstile",
            "status": "not-ready",
            "description": "未检测到 reCAPTCHA 或 Turnstile 等反垃圾保护。",
        })

    return items


def _build_findings(
    tags_home: dict[str, dict[str, Any]],
    features_all: dict[str, dict[str, Any]],
    has_conversion_on_thankyou: bool,
    thankyou_probed: bool,
    pdf_links: list[str],
    has_download_tracking: bool,
) -> list[dict[str, Any]]:
    """Apply rule engine and return structured findings list."""
    findings: list[dict[str, Any]] = []
    has_ads = tags_home.get("google_ads", {}).get("found", False)

    # TRACK-001: No conversion tracking on Thank You page
    if thankyou_probed and not has_conversion_on_thankyou:
        severity = "P0" if has_ads else "P1"
        findings.append({
            "rule_id": "TRACK-001",
            "severity": severity,
            "title_zh": "Thank You 页面未检测到转化追踪代码",
            "evidence": (
                "Thank You 页面 HTML 中未发现 gtag('event', 'conversion') 或 "
                "gtag('event', 'purchase') 调用。"
            ),
            "confidence": 0.90,
            "impact_zh": (
                "没有转化追踪，Google Ads 无法将广告点击与实际询盘/订单关联。"
                "智能出价 (tCPA/tROAS) 将因缺少转化信号而无法优化，"
                "导致预算浪费。这是跨境电商广告 ROI 的第一大盲区。"
                if has_ads else
                "即使当前未投放广告，提前部署转化追踪有助于积累数据，"
                "为后续广告投放建立 baseline。"
            ),
            "action_zh": (
                "在 Thank You 页面的 <head> 中添加以下代码 (需替换 AW-xxx 和 "
                "conversion label):"
            ),
            "code_snippet": (
                "<!-- Google Ads 转化追踪 -->\n"
                "<script>\n"
                "  gtag('event', 'conversion', {\n"
                "    'send_to': 'AW-XXXXXXXXXX/CONVERSION_LABEL',\n"
                "    'value': 1.0,\n"
                "    'currency': 'USD'\n"
                "  });\n"
                "</script>"
            ),
            "code_language": "HTML",
            "paste_location": "Thank You 页面 <head> 标签内，gtag.js 加载之后",
            "needs_account": True,
        })

    # TRACK-002: No Consent Mode v2
    if not features_all.get("consent_mode_v2", {}).get("found"):
        findings.append({
            "rule_id": "TRACK-002",
            "severity": "P1",
            "title_zh": "未配置 Google Consent Mode v2",
            "evidence": (
                "所有已扫描页面的 HTML/JS 中未发现 "
                "gtag('consent', 'default', {...}) 调用。"
            ),
            "confidence": 0.85,
            "impact_zh": (
                "2024 年 3 月起，Google 要求向欧盟/英国/瑞士用户展示广告的广告主"
                "必须实施 Consent Mode v2。未实施将导致欧盟流量的再营销受众"
                "和转化数据不可用，影响智能出价效果。"
            ),
            "action_zh": (
                "在 gtag.js 初始化之前添加 Consent Mode 默认配置。"
                "建议配合 CMP (如 Cookiebot / OneTrust) 使用。"
            ),
            "code_snippet": (
                "<!-- Consent Mode v2 默认设置 (放在 gtag.js 之前) -->\n"
                "<script>\n"
                "  window.dataLayer = window.dataLayer || [];\n"
                "  function gtag(){dataLayer.push(arguments);}\n"
                "\n"
                "  gtag('consent', 'default', {\n"
                "    'ad_storage': 'denied',\n"
                "    'ad_user_data': 'denied',\n"
                "    'ad_personalization': 'denied',\n"
                "    'analytics_storage': 'granted',\n"
                "    'wait_for_update': 500\n"
                "  });\n"
                "</script>"
            ),
            "code_language": "HTML",
            "paste_location": "所有页面 <head> 标签内，gtag.js 脚本之前",
            "needs_account": False,
        })

    # TRACK-003: No Enhanced Conversions
    if has_ads and not features_all.get("enhanced_conversions", {}).get("found"):
        findings.append({
            "rule_id": "TRACK-003",
            "severity": "P1",
            "title_zh": "未启用 Enhanced Conversions (增强型转化)",
            "evidence": (
                "已扫描页面中未发现 gtag('set', 'user_data', {...}) 调用。"
            ),
            "confidence": 0.80,
            "impact_zh": (
                "Enhanced Conversions 使用 first-party 用户数据 (邮箱/电话号码的哈希值) "
                "补充 cookie-based 归因。在 cookie 逐步淘汰的趋势下，"
                "启用后可提升 5-17% 的转化归因精度。"
            ),
            "action_zh": (
                "在转化页面中使用 gtag 设置已哈希的用户数据。"
                "需要在 Google Ads 后台开启 Enhanced Conversions 功能。"
            ),
            "code_snippet": (
                "<!-- Enhanced Conversions 用户数据传递 -->\n"
                "<script>\n"
                "  gtag('set', 'user_data', {\n"
                "    'email': document.querySelector('[name=email]')?.value,\n"
                "    'phone_number': document.querySelector('[name=phone]')?.value\n"
                "  });\n"
                "</script>"
            ),
            "code_language": "HTML",
            "paste_location": "转化页面 (Thank You / 订单确认页)，表单提交后",
            "needs_account": True,
        })

    # TRACK-004: PDF/whitepaper download not tracked
    if pdf_links and not has_download_tracking:
        findings.append({
            "rule_id": "TRACK-004",
            "severity": "P2",
            "title_zh": "文档下载未配置事件追踪",
            "evidence": (
                f"检测到 {len(pdf_links)} 个可下载文档链接 "
                f"(如 {pdf_links[0][:80]}...)，但未发现对应的 "
                "gtag('event', 'file_download') 或 gtag('event', 'generate_lead') 调用。"
            ),
            "confidence": 0.75,
            "impact_zh": (
                "白皮书/目录下载是跨境 B2B 重要的转化行为之一。"
                "未追踪意味着无法衡量内容营销的实际效果，也无法对下载用户做再营销。"
            ),
            "action_zh": (
                "为所有下载链接添加 click 事件监听，触发 GA4 事件。"
                "如果使用 GTM，可通过「链接点击」触发器自动捕获。"
            ),
            "code_snippet": (
                "<!-- GA4 文档下载追踪 -->\n"
                "<script>\n"
                "  document.querySelectorAll('a[href$=\".pdf\"]').forEach(link => {\n"
                "    link.addEventListener('click', () => {\n"
                "      gtag('event', 'file_download', {\n"
                "        'file_name': link.href.split('/').pop(),\n"
                "        'file_extension': 'pdf',\n"
                "        'link_url': link.href\n"
                "      });\n"
                "    });\n"
                "  });\n"
                "</script>"
            ),
            "code_language": "HTML",
            "paste_location": "包含下载链接的页面，放在 </body> 前",
            "needs_account": False,
        })

    return findings


def probe(
    url: str,
    form_url: str | None = None,
    thank_you_url: str | None = None,
) -> dict[str, Any]:
    """Run the full EAC tracking probe. Returns structured result dict."""
    if "://" not in url:
        url = "https://" + url

    pages_probed: list[dict[str, Any]] = []
    all_html: dict[str, str] = {}  # page_key -> html

    # --- 1. Fetch pages ---
    page_map: dict[str, str] = {"home": url}
    if form_url:
        page_map["form"] = (
            form_url if "://" in form_url else urljoin(url.rstrip("/") + "/", form_url)
        )
    if thank_you_url:
        page_map["thank_you"] = (
            thank_you_url if "://" in thank_you_url
            else urljoin(url.rstrip("/") + "/", thank_you_url)
        )

    for page_key, page_url in page_map.items():
        try:
            final_url, body, status = _fetch(page_url)
            pages_probed.append({
                "page": page_key,
                "url": page_url,
                "final_url": final_url,
                "status": "ok",
                "status_code": status,
                "html_size": len(body),
            })
            all_html[page_key] = body
        except Exception as e:
            pages_probed.append({
                "page": page_key,
                "url": page_url,
                "status": f"error: {e}",
            })

    # --- 2. Detect tags on homepage ---
    home_html = all_html.get("home", "")
    tags_home = _detect_tags(home_html)

    # --- 3. Detect features across ALL pages ---
    combined_html = "\n".join(all_html.values())
    features_all = _detect_features(combined_html)

    # --- 4. Check conversion on thank-you page ---
    thankyou_html = all_html.get("thank_you", "")
    thankyou_probed = "thank_you" in all_html
    thankyou_features = _detect_features(thankyou_html) if thankyou_probed else {}
    has_conversion_on_thankyou = (
        thankyou_features.get("conversion_event", {}).get("found", False)
        or thankyou_features.get("purchase_event", {}).get("found", False)
    )

    # --- 5. PDF / download tracking ---
    pdf_links = _detect_pdf_links(combined_html)
    has_download_tracking = bool(DOWNLOAD_EVENT_RE.search(combined_html))

    # --- 6. Extract tag IDs ---
    tag_ids = _extract_tag_ids(combined_html)

    # --- 7. Leakage risk ---
    has_ads = tags_home.get("google_ads", {}).get("found", False)
    has_consent = features_all.get("consent_mode_v2", {}).get("found", False)
    leakage_risk, leakage_description = _calculate_leakage(
        has_ads_tag=has_ads,
        has_conversion_on_thankyou=has_conversion_on_thankyou,
        has_consent_mode=has_consent,
    )

    # --- 8. Readiness items ---
    readiness_items = _build_readiness(tags_home, features_all)

    # --- 9. Build findings ---
    findings = _build_findings(
        tags_home=tags_home,
        features_all=features_all,
        has_conversion_on_thankyou=has_conversion_on_thankyou,
        thankyou_probed=thankyou_probed,
        pdf_links=pdf_links,
        has_download_tracking=has_download_tracking,
    )

    return {
        "_probe_status": "ok",
        "url": url,
        "pages_probed": pages_probed,
        "tags_detected": {k: v for k, v in tags_home.items()},
        "features_detected": {k: v for k, v in features_all.items()},
        "tag_ids": tag_ids,
        "leakage_risk": leakage_risk,
        "leakage_description": leakage_description,
        "readiness_items": readiness_items,
        "findings": findings,
        "disclaimer": (
            "Tracking tags may load dynamically via GTM, after cookie consent, "
            "or on pages not probed. Tags inside iframes or loaded by SPA "
            "hydration may not be detected by static HTML analysis. "
            "Absence of evidence is not evidence of absence."
        ),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "usage: eac_tracking_probe.py <url> [--form <form_url>] "
            "[--thankyou <thank_you_url>]",
            file=sys.stderr,
        )
        return 2

    url = sys.argv[1]
    if not urlparse(url).scheme:
        url = "https://" + url

    form_url: str | None = None
    thank_you_url: str | None = None

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--form" and i + 1 < len(args):
            form_url = args[i + 1]
            i += 2
        elif args[i] == "--thankyou" and i + 1 < len(args):
            thank_you_url = args[i + 1]
            i += 2
        else:
            i += 1

    result = probe(url, form_url=form_url, thank_you_url=thank_you_url)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
