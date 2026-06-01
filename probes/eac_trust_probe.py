#!/usr/bin/env python3
"""
EAC trust probe — audits trust-building content on a website.

Checks:
- Blog/content section existence (common paths: /blog, /news, etc.)
- Product page trust signals (specs table, testimonials, badges, comparison)
- SSL/HTTPS verification
- Customer testimonials / review widgets

Standalone: python probes/eac_trust_probe.py https://example.com
Optional:   python probes/eac_trust_probe.py https://example.com --product https://example.com/product/x
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

try:
    # Normal package import (engine loads this module by file path with the
    # probes/ dir on sys.path, so the sibling import resolves).
    from probes.sitemap_discovery import discover_urls
except ImportError:  # pragma: no cover - fallback for path-only loads
    try:
        from sitemap_discovery import discover_urls  # type: ignore
    except ImportError:
        discover_urls = None  # type: ignore

UA = "EAC-Audit/1.0 (Canlah AI; Google EAC Partner)"
TIMEOUT = 10.0


def _detect_site_context(html: str, url: str) -> dict[str, str]:
    """Extract industry/product context from homepage for personalized recommendations."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = title_m.group(1).strip() if title_m else ""

    desc_m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html, re.I)
    desc = desc_m.group(1).strip() if desc_m else ""

    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    h1 = re.sub(r"<[^>]+>", "", h1_m.group(1)).strip() if h1_m else ""

    domain = urlparse(url).netloc.replace("www.", "")
    brand = title.split("|")[0].split("-")[0].split("—")[0].strip() if title else domain

    context_text = f"{title} {desc} {h1}".lower()

    def _has_word(keywords: list[str]) -> bool:
        for kw in keywords:
            if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", context_text):
                return True
        return False

    industry = "综合企业"
    product = "核心产品/服务"
    if _has_word(["outdoor", "shade", "pergola", "gazebo", "凉亭", "遮阳"]):
        industry = "户外建材/遮阳产品"
        product = "户外凉亭/遮阳棚"
    elif _has_word(["robot", "automation", "机器人", "自动化"]):
        industry = "智能机器人/自动化"
        product = "工业机器人/自动化设备"
    elif _has_word(["3d print", "maker", "制造"]):
        industry = "3D打印/智能制造"
        product = "3D打印设备/解决方案"
    elif _has_word(["education", "academy", "learning", "教育", "培训"]):
        industry = "教育/培训服务"
        product = "课程/培训项目"
    elif _has_word(["earplug", "audio", "hearing", "耳塞"]):
        industry = "听力保护/音频设备"
        product = "专业耳塞/听力保护产品"
    elif _has_word(["delivery", "vehicle", "物流", "配送车"]):
        industry = "物流配送设备"
        product = "配送车辆/物流设备"
    elif _has_word(["food", "restaurant", "餐厅", "饮品", "boba", "cafe", "coffee"]):
        industry = "餐饮/食品品牌"
        product = "餐饮产品/饮品"
    elif _has_word(["artwork", "painting", "bindery", "绘画", "艺术品"]):
        industry = "艺术/创意产品"
        product = "艺术用品/创作工具"
    elif _has_word(["ai", "software", "saas", "platform", "tech", "数据"]):
        industry = "AI/科技服务"
        product = "AI解决方案/SaaS平台"
    elif _has_word(["ecommerce", "e-commerce", "shop", "store", "电商", "跨境"]):
        industry = "跨境电商"
        product = "电商产品"
    elif _has_word(["fashion", "clothing", "apparel", "服装"]):
        industry = "服装/时尚品牌"
        product = "服装/配饰产品"
    elif _has_word(["beauty", "cosmetic", "skincare", "美妆"]):
        industry = "美妆/护肤品牌"
        product = "美妆/护肤产品"
    elif _has_word(["health", "supplement", "wellness", "保健"]):
        industry = "健康/保健品牌"
        product = "保健产品/健康方案"
    elif _has_word(["furniture", "home", "decor", "家居"]):
        industry = "家居/家具品牌"
        product = "家居产品/装饰"
    elif _has_word(["electronics", "device", "gadget", "电子"]):
        industry = "消费电子/智能设备"
        product = "电子产品/智能设备"

    return {
        "brand": brand,
        "industry": industry,
        "product": product,
        "title": title,
        "description": desc,
    }


# ---------------------------------------------------------------------------
# Content section paths to probe
# ---------------------------------------------------------------------------

CONTENT_PATHS = [
    "/blog", "/news", "/resources", "/articles",
    "/insights", "/case-studies", "/cases", "/stories",
]

# ---------------------------------------------------------------------------
# Trust signal detection patterns
# ---------------------------------------------------------------------------

# Product specification table indicators
SPEC_TABLE_TERMS = re.compile(
    r"(?:specification|specs|technical\s+data|参数|规格|型号|material|dimension"
    r"|weight|capacity|voltage|power|size|length|width|height)",
    re.I,
)

# Testimonial / review patterns
TESTIMONIAL_PATTERNS = [
    re.compile(r'class\s*=\s*"[^"]*\b(?:testimonial|review|client-say|feedback|quote)\b[^"]*"', re.I),
    re.compile(r"<blockquote\b[^>]*>", re.I),
    re.compile(r'"@type"\s*:\s*"(?:Review|AggregateRating)"', re.I),
]

# Review widget patterns (reused from reviews_scan.py)
REVIEW_WIDGET_PATTERNS = [
    re.compile(r"staticw2\.yotpo\.com|cdn-widgetsrepository\.yotpo\.com|yotpo-widget", re.I),
    re.compile(r"cdn\.judge\.me/|jdgm-", re.I),
    re.compile(r"loox\.io/widget/", re.I),
    re.compile(r"cdn1?\.stamped\.io/", re.I),
    re.compile(r"okendo\.io|d3hw6dc1ow8pp2\.cloudfront\.net", re.I),
    re.compile(r"widget\.trustpilot\.com|tp-widget", re.I),
    re.compile(r"widget\.reviews\.co\.uk/", re.I),
    re.compile(r"elfsight\.com/google-reviews", re.I),
]

# Trust badge / certification patterns
TRUST_BADGE_PATTERNS = [
    re.compile(r"\bISO\s*\d{4,5}\b", re.I),
    re.compile(r"\bCE\s*(?:mark|certified|certification)\b", re.I),
    re.compile(r"\bFDA\s*(?:approved|cleared|registered)\b", re.I),
    re.compile(r"\bSGS\b", re.I),
    re.compile(r"\bUL\s*(?:listed|certified)\b", re.I),
    re.compile(r"\bRoHS\b", re.I),
    re.compile(r"\bGMP\b", re.I),
    re.compile(r"(?:ssl|secure|security|trust|verified|certified)\s*(?:badge|seal|icon|logo)", re.I),
    re.compile(r"(?:mcafee|norton|comodo|digicert|globalsign|sectigo)\s*(?:secure|seal|verified)", re.I),
    re.compile(r'(?:src|alt|class)\s*=\s*"[^"]*(?:badge|certification|trust-seal|security-seal)[^"]*"', re.I),
]

# Comparison table pattern
COMPARISON_PATTERNS = [
    re.compile(r'class\s*=\s*"[^"]*\bcompar(?:ison|e)\b[^"]*"', re.I),
    re.compile(r"<table\b[^>]*>(?=.*?(?:vs\.?|compare|comparison|competitor))", re.I | re.S),
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url: str) -> tuple[str, int]:
    """GET a URL, return (body, status_code). Returns ('', status) on error."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        return resp.text, resp.status_code
    except Exception:
        return "", 0


def _head(url: str) -> int:
    """HEAD request, return status code. 0 on network error."""
    try:
        resp = requests.head(
            url,
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        return resp.status_code
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Content section checker
# ---------------------------------------------------------------------------

def _check_content_paths(base_url: str) -> list[dict[str, Any]]:
    """Check common content paths in parallel. Returns per-path status."""
    base = base_url.rstrip("/")
    results: list[dict[str, Any]] = []

    def _check_one(path: str) -> dict[str, Any]:
        url = base + path
        status = _head(url)
        exists = 200 <= status < 400
        return {"path": path, "status": status, "exists": exists}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_check_one, p): p for p in CONTENT_PATHS}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:
                path = futs[fut]
                results.append({"path": path, "status": 0, "exists": False})

    # Sort by path order for deterministic output
    path_order = {p: i for i, p in enumerate(CONTENT_PATHS)}
    results.sort(key=lambda r: path_order.get(r["path"], 999))
    return results


def _detect_blog(base_url: str) -> dict[str, Any]:
    """Sitemap-first blog detection.

    The sitemap (robots.txt Sitemap: directives + /sitemap.xml + child
    sitemaps) is the single source of truth for what real URLs a site has.
    Shopify stores blog at /blogs/<handle> (e.g. /blogs/gazebo-tips), which the
    old hardcoded /blog HEAD-check missed entirely -> false "no blog" verdict.

    Strategy:
      1. Try sitemap discovery. ANY URL categorized as blog (incl. Shopify
         /blogs/... and the sitemap_blogs_N.xml child sitemap) => has_blog,
         with the REAL blog URL as evidence.
      2. Only fall back to HEAD-checking the hardcoded CONTENT_PATHS when the
         sitemap is unavailable / yields no blog signal.

    Returns:
        {
          "has_blog": bool,
          "method": "sitemap" | "path_guess",
          "blog_urls": [real urls],          # from sitemap (possibly empty)
          "blog_url": str | None,            # first real blog URL for evidence
          "blog_count": int,                 # sitemap blog URL count
          "sitemap_found": bool,
          "content_paths": [path-guess results],  # always run for output parity
        }
    """
    # Always run the legacy path probe so the output keeps `content_paths`
    # (downstream report/template may render it) and so we have a fallback.
    content_paths = _check_content_paths(base_url)
    path_guess_has_blog = any(p["exists"] for p in content_paths)

    blog_urls: list[str] = []
    sitemap_found = False
    if discover_urls is not None:
        try:
            sm = discover_urls(base_url)
            sitemap_found = sm.sitemap_found
            blog_urls = list(sm.blog_urls)
        except Exception:  # noqa: BLE001 — discovery must never abort the probe
            blog_urls = []
            sitemap_found = False

    if blog_urls:
        return {
            "has_blog": True,
            "method": "sitemap",
            "blog_urls": blog_urls,
            "blog_url": blog_urls[0],
            "blog_count": len(blog_urls),
            "sitemap_found": sitemap_found,
            "content_paths": content_paths,
        }

    # No blog evidence from sitemap -> fall back to path guessing.
    fallback_url: str | None = None
    if path_guess_has_blog:
        for p in content_paths:
            if p["exists"]:
                fallback_url = base_url.rstrip("/") + p["path"]
                break
    return {
        "has_blog": path_guess_has_blog,
        "method": "path_guess",
        "blog_urls": [],
        "blog_url": fallback_url,
        "blog_count": 0,
        "sitemap_found": sitemap_found,
        "content_paths": content_paths,
    }


# ---------------------------------------------------------------------------
# Product page analysis
# ---------------------------------------------------------------------------

def _analyze_product_page(html: str) -> dict[str, bool]:
    """Analyze a product page for trust signals."""
    # Specs table: need a <table> AND product-related terms nearby
    has_table = bool(re.search(r"<table\b", html, re.I))
    has_spec_terms = bool(SPEC_TABLE_TERMS.search(html))
    has_specs_table = has_table and has_spec_terms

    # Testimonials: class-based, blockquote, JSON-LD, or review widgets
    has_testimonials = any(p.search(html) for p in TESTIMONIAL_PATTERNS)
    if not has_testimonials:
        has_testimonials = any(p.search(html) for p in REVIEW_WIDGET_PATTERNS)

    # Trust badges / certifications
    has_trust_badges = any(p.search(html) for p in TRUST_BADGE_PATTERNS)

    # Comparison tables
    has_comparison = any(p.search(html) for p in COMPARISON_PATTERNS)

    return {
        "has_specs_table": has_specs_table,
        "has_testimonials": has_testimonials,
        "has_trust_badges": has_trust_badges,
        "has_comparison": has_comparison,
    }


# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------

def _blog_content_matrix(ctx: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a blog content matrix PERSONALIZED to the detected industry."""
    industry = (ctx or {}).get("industry", "综合企业")
    product = (ctx or {}).get("product", "核心产品/服务")
    brand = (ctx or {}).get("brand", "贵公司")

    return {
        "description": f"内容矩阵分类指南 -- 为{industry}行业按三个维度规划博客内容",
        "categories": [
            {
                "name": "行业趋势",
                "purpose": "吸引决策层关注，建立行业权威",
                "topics": [
                    f"2026年{industry}行业全球市场趋势与机遇分析",
                    f"目标市场政策法规变化对{industry}的影响解读",
                    f"{industry}行业竞争格局：头部品牌策略对比",
                    f"AI/数字化技术如何赋能{industry}产业升级",
                    f"海外买家采购{product}时最关注的 5 个因素",
                ],
                "frequency": "每月 2 篇",
                "seo_value": "长尾关键词覆盖，提升自然搜索排名",
            },
            {
                "name": "产品实操",
                "purpose": "转化技术型买家，展示专业能力",
                "topics": [
                    f"{product}选型指南：如何根据应用场景选择合适的型号",
                    f"{product}安装/部署/使用全流程教程（图文+视频）",
                    f"{product}常见问题解答 (FAQ) 与故障排除手册",
                    f"{product}日常维护保养最佳实践与注意事项",
                    f"客户案例：{brand}如何帮助客户解决实际问题",
                ],
                "frequency": "每月 3-4 篇",
                "seo_value": "产品关键词密度提升，直接驱动询盘",
            },
            {
                "name": "产品动态",
                "purpose": "维护现有客户关系，传递品牌活力",
                "topics": [
                    f"新品发布：{brand}全新{product}系列正式上线",
                    f"参展回顾：{brand}亮相行业展会精彩回顾",
                    f"认证更新：获得新的国际认证（CE/UL/ISO 等）",
                    f"产能升级：{brand}年产能与质量管理最新动态",
                    f"合作伙伴：{brand}与行业伙伴达成战略合作",
                ],
                "frequency": "每月 1-2 篇",
                "seo_value": "品牌关键词强化，提升品牌搜索权重",
            },
        ],
    }


def _whitepaper_template() -> dict[str, Any]:
    """Return the whitepaper structure recommendation template."""
    return {
        "description": "白皮书内容结构标准规范",
        "structure": [
            {
                "section": "封面",
                "content": "标题 + 副标题 + 公司 logo + 日期",
                "notes": "标题用数字和痛点吸引（如'2026年跨境电商出海成本降低30%的5个策略'）",
            },
            {
                "section": "目录",
                "content": "章节列表 + 页码",
                "notes": "控制在 8-15 页",
            },
            {
                "section": "摘要",
                "content": "核心观点概述（300字以内）",
                "notes": "让读者 30 秒内决定是否继续阅读",
            },
            {
                "section": "行业背景",
                "content": "市场规模 + 增长趋势 + 痛点分析",
                "notes": "用第三方数据增强可信度（Statista/McKinsey/行业报告）",
            },
            {
                "section": "解决方案",
                "content": "分 3-5 个子章节展开",
                "notes": "每个子章节：问题描述 -> 解决路径 -> 数据佐证 -> 案例",
            },
            {
                "section": "案例研究",
                "content": "1-2 个客户成功案例",
                "notes": "格式：挑战 -> 方案 -> 结果（用具体数字）",
            },
            {
                "section": "行动建议",
                "content": "3-5 条具体可执行的建议",
                "notes": "每条建议注明实施难度和预期效果",
            },
            {
                "section": "留资转化",
                "content": "CTA + 联系方式 + 二维码",
                "notes": "白皮书的最终目的是获取联系方式",
            },
        ],
        "conversion_design": (
            "在白皮书下载页设置表单（姓名+邮箱+公司），"
            "下载完成后跳转 Thank You 页面并触发转化追踪代码"
        ),
    }


def _product_page_template() -> dict[str, Any]:
    """Return the high-conversion product detail page template."""
    return {
        "description": "高转化产品详情页模板与设计规范",
        "sections": [
            {
                "name": "Hero 区域",
                "elements": [
                    "产品主图（白底高清，至少 1200x1200px）",
                    "产品名称 + 一句话卖点",
                    "核心参数快速预览（3-5个）",
                    "CTA 按钮（询价/下载资料）",
                ],
                "priority": "必须",
            },
            {
                "name": "可信度数据",
                "elements": [
                    "年产量/出货量",
                    "服务客户数量",
                    "行业经验年数",
                    "质检通过率",
                ],
                "priority": "必须",
            },
            {
                "name": "产品工作原理",
                "elements": [
                    "技术原理图/流程图",
                    "核心技术优势说明",
                    "应用场景展示",
                ],
                "priority": "推荐",
            },
            {
                "name": "产品参数",
                "elements": [
                    "完整规格参数表（表格形式）",
                    "包装信息",
                    "物流/交期信息",
                ],
                "priority": "必须",
            },
            {
                "name": "合规与安全认证",
                "elements": [
                    "ISO/CE/FDA/SGS 认证 logo",
                    "检测报告编号（可查询）",
                    "合规声明",
                ],
                "priority": "必须（尤其对欧美市场）",
            },
            {
                "name": "客户背书",
                "elements": [
                    "客户评价/推荐信",
                    "合作品牌 logo 墙",
                    "案例研究链接",
                ],
                "priority": "强烈推荐",
            },
            {
                "name": "相似产品横向对比",
                "elements": [
                    "对比表格（vs 竞品 A/B/C）",
                    "差异化优势高亮",
                    "价格区间参考",
                ],
                "priority": "推荐",
            },
        ],
    }


def _build_recommendations(
    has_blog: bool,
    product_analysis: dict[str, bool] | None,
    site_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the rich recommendations dict attached to the probe output."""
    recs: dict[str, Any] = {}
    if site_context:
        recs["detected_context"] = site_context
    if not has_blog:
        recs["blog_content_matrix"] = _blog_content_matrix(site_context)
        recs["whitepaper_template"] = _whitepaper_template()
    if product_analysis is not None:
        missing = (
            not product_analysis["has_specs_table"]
            or not product_analysis["has_trust_badges"]
            or not product_analysis["has_comparison"]
            or not product_analysis["has_testimonials"]
        )
        if missing:
            recs["product_page_template"] = _product_page_template()
    return recs


def _build_findings(
    content_paths: list[dict],
    has_blog: bool,
    is_https: bool,
    product_analysis: dict[str, bool] | None,
    blog_detection: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate structured findings from trust analysis."""
    findings: list[dict[str, Any]] = []

    # TRUST-004: No SSL (HTTP only) -- highest severity
    if not is_https:
        findings.append({
            "severity": "P0",
            "rule_id": "TRUST-004",
            "title_zh": "网站未启用 HTTPS 加密",
            "evidence": "目标 URL 使用 HTTP 协议，未检测到 SSL/TLS 证书。",
            "confidence": 0.98,
            "impact_zh": (
                "HTTP 明文传输会导致浏览器显示'不安全'警告，"
                "严重影响用户信任。Google 已将 HTTPS 作为排名信号，"
                "且表单数据在传输中可能被截获。"
            ),
            "action_zh": (
                "安装 SSL 证书并启用 HTTPS。推荐使用 Let's Encrypt"
                "（免费）或 Cloudflare（自动 SSL）。配置 301 重定向"
                "将所有 HTTP 请求跳转到 HTTPS。"
            ),
            "needs_account": False,
            "code_snippet": None,
            "code_language": None,
            "paste_location": None,
        })

    # TRUST-001: No blog/content section.
    # Sitemap-first: this finding must NOT fire when the sitemap shows real
    # blog URLs (e.g. Shopify /blogs/<handle>). has_blog already reflects the
    # sitemap-first verdict; the evidence text is built to match the method
    # actually used so we never list the wrong (guessed) paths as "proof".
    if not has_blog:
        method = (blog_detection or {}).get("method", "path_guess")
        if method == "sitemap":
            # Sitemap was readable but contained no blog/editorial URLs.
            evidence = (
                "已解析站点 sitemap（robots.txt Sitemap 指令 + /sitemap.xml + "
                "子 sitemap），其中未发现任何博客/资讯类 URL"
                "（如 /blog、/blogs/<handle>、/news、/articles）。"
            )
        else:
            checked_paths = ", ".join(p["path"] for p in content_paths)
            evidence = (
                f"未能解析到 sitemap；HEAD 探测路径 {checked_paths} 均返回 404 "
                "或不可访问。站点中未发现活跃的内容板块。"
            )
        findings.append({
            "severity": "P1",
            "rule_id": "TRUST-001",
            "title_zh": "未检测到博客/资讯内容板块",
            "evidence": evidence,
            "confidence": 0.9,
            "impact_zh": (
                "博客/资讯内容是 Google SEO 的核心信号，也是建立行业权威"
                "的主要途径。无内容板块意味着放弃了自然搜索流量，"
                "且 AI 搜索引擎（Google AI Overviews、ChatGPT）"
                "缺乏可引用的深度内容。"
            ),
            "action_zh": (
                "建议按三类内容矩阵规划博客内容:\n\n"
                "[1] 行业趋势（每月 2 篇，吸引决策者）\n"
                "  - 2026年本行业出海趋势报告\n"
                "  - 目标市场准入政策变化解读\n"
                "  - 跨境电商品类竞争格局分析\n"
                "  - AI 技术如何重塑供应链\n"
                "  - 消费者偏好变化与选品策略\n\n"
                "[2] 产品实操（每月 3-4 篇，转化技术买家）\n"
                "  - 产品选型指南\n"
                "  - 安装/使用教程（图文+视频）\n"
                "  - 常见问题解答 (FAQ) 与故障排除\n"
                "  - 维护保养最佳实践\n"
                "  - 客户案例与效率提升数据\n\n"
                "[3] 产品动态（每月 1-2 篇，传递品牌活力）\n"
                "  - 新品发布公告\n"
                "  - 参展/认证/产线升级动态\n"
                "  - 合作伙伴与战略合作\n\n"
                "同步制作行业白皮书（8-15页），通过下载留资获取销售线索。"
                "白皮书标准结构：封面 -> 目录 -> 摘要 -> 行业背景 -> "
                "解决方案 -> 案例研究 -> 行动建议 -> 留资CTA。"
            ),
            "needs_account": False,
            "code_snippet": None,
            "code_language": None,
            "paste_location": None,
        })

    # TRUST-002: Product page missing specs/certs
    if product_analysis is not None:
        missing_elements: list[str] = []
        if not product_analysis["has_specs_table"]:
            missing_elements.append("产品规格表")
        if not product_analysis["has_trust_badges"]:
            missing_elements.append("认证/资质徽章（ISO/CE/FDA/SGS）")
        if not product_analysis["has_comparison"]:
            missing_elements.append("产品对比表")

        if missing_elements:
            # Build a detailed section-by-section remediation plan
            section_details: list[str] = []
            if not product_analysis["has_specs_table"]:
                section_details.append(
                    "[缺失] 产品参数区域 -- 添加完整规格参数表（表格形式），"
                    "包含包装信息和物流/交期信息"
                )
            if not product_analysis["has_trust_badges"]:
                section_details.append(
                    "[缺失] 合规与安全认证区域 -- 添加 ISO/CE/FDA/SGS 认证 logo、"
                    "检测报告编号（可查询）、合规声明。对欧美市场尤其重要"
                )
            if not product_analysis["has_comparison"]:
                section_details.append(
                    "[缺失] 相似产品横向对比 -- 添加对比表格（vs 竞品 A/B/C），"
                    "高亮差异化优势，标注价格区间参考"
                )
            if not product_analysis["has_testimonials"]:
                section_details.append(
                    "[缺失] 客户背书区域 -- 添加客户评价/推荐信、"
                    "合作品牌 logo 墙、案例研究链接"
                )

            findings.append({
                "severity": "P1",
                "rule_id": "TRUST-002",
                "title_zh": "产品详情页缺少可信度元素",
                "evidence": (
                    f"产品页面缺少以下可信度元素: "
                    f"{', '.join(missing_elements)}。"
                ),
                "confidence": 0.85,
                "impact_zh": (
                    "B2B 采购决策者在评估供应商时，产品规格表、"
                    "认证资质和竞品对比是核心参考依据。缺少这些元素"
                    "会降低页面专业度，增加客户询盘前的犹豫时间。"
                    "完整的产品详情页应包含 7 个标准区域。"
                ),
                "action_zh": (
                    "高转化产品详情页应包含以下 7 个标准区域:\n\n"
                    "[1] Hero 区域（必须）\n"
                    "  - 产品主图（白底高清，至少 1200x1200px）\n"
                    "  - 产品名称 + 一句话卖点\n"
                    "  - 核心参数快速预览（3-5个）\n"
                    "  - CTA 按钮（询价/下载资料）\n\n"
                    "[2] 可信度数据（必须）\n"
                    "  - 年产量/出货量、服务客户数量、行业经验年数、质检通过率\n\n"
                    "[3] 产品工作原理（推荐）\n"
                    "  - 技术原理图/流程图、核心技术优势、应用场景展示\n\n"
                    "[4] 产品参数（必须）\n"
                    "  - 完整规格参数表（表格形式）、包装信息、物流/交期\n\n"
                    "[5] 合规与安全认证（必须，尤其欧美市场）\n"
                    "  - ISO/CE/FDA/SGS 认证 logo + 可查询检测报告编号\n\n"
                    "[6] 客户背书（强烈推荐）\n"
                    "  - 客户评价/推荐信、合作品牌 logo 墙、案例研究链接\n\n"
                    "[7] 相似产品横向对比（推荐）\n"
                    "  - 对比表格（vs 竞品），差异化优势高亮，价格区间参考\n\n"
                    "当前页面缺失情况:\n"
                    + "\n".join(f"  {d}" for d in section_details)
                ),
                "needs_account": False,
                "code_snippet": None,
                "code_language": None,
                "paste_location": None,
            })

    # TRUST-003: No testimonials/reviews
    has_testimonials_anywhere = (
        product_analysis is not None and product_analysis["has_testimonials"]
    )
    if not has_testimonials_anywhere:
        findings.append({
            "severity": "P2",
            "rule_id": "TRUST-003",
            "title_zh": "未检测到客户评价",
            "evidence": (
                "页面中未检测到客户评价/推荐元素 "
                "(testimonial class, blockquote, Review/AggregateRating schema, "
                "或第三方评价 widget 如 Trustpilot/Yotpo/Judge.me)。"
            ),
            "confidence": 0.8,
            "impact_zh": (
                "92% 的消费者在购买前会查看客户评价（BrightLocal 2025）。"
                "缺少社会证明会增加潜在客户的信任门槛，"
                "尤其对新访客和高客单价产品影响更大。"
            ),
            "action_zh": (
                "建议添加至少 3-5 条带真实姓名、职位、公司的客户推荐。"
                "可安装评价 widget（Trustpilot/Google Reviews embed）"
                "展示第三方验证评价。"
            ),
            "needs_account": False,
            "code_snippet": None,
            "code_language": None,
            "paste_location": None,
        })

    return findings


# ---------------------------------------------------------------------------
# Main probe entry point
# ---------------------------------------------------------------------------

def probe(
    url: str,
    product_page_url: str | None = None,
    company_name: str | None = None,
) -> dict[str, Any]:
    """Audit trust-building content on a website.

    Args:
        url: Homepage URL to audit.
        product_page_url: Optional product page URL for deeper analysis.
        company_name: Optional company name (reserved for future media
            coverage search).

    Returns a dict with _probe_status, content path checks, product page
    analysis, SSL status, and structured findings.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    # Determine HTTPS status from the URL scheme
    is_https = parsed.scheme == "https"

    # If URL is HTTP, also check if HTTPS version responds
    if not is_https:
        https_url = "https://" + parsed.netloc + parsed.path
        try:
            resp = requests.head(
                https_url,
                headers={"User-Agent": UA},
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if 200 <= resp.status_code < 400:
                is_https = True
        except Exception:
            pass

    # Fetch homepage
    home_html, home_status = _fetch(url)
    if home_status == 0:
        return {
            "_probe_status": "error",
            "base_url": url,
            "error": "Failed to fetch homepage",
            "content_paths_checked": [],
            "has_blog": False,
            "blog_detection_method": None,
            "blog_url": None,
            "blog_urls": [],
            "blog_url_count": 0,
            "sitemap_found": False,
            "product_page_analysis": None,
            "is_https": is_https,
            "findings": [],
            "recommendations": {},
        }

    # Detect site industry/product context for personalized recommendations
    site_context = _detect_site_context(home_html, url)
    if company_name:
        site_context["brand"] = company_name

    # Sitemap-first blog detection (falls back to path-guessing internally).
    blog_detection = _detect_blog(url)
    content_paths = blog_detection["content_paths"]
    has_blog = blog_detection["has_blog"]

    # Check homepage for testimonials (even without a product page)
    home_testimonials = any(p.search(home_html) for p in TESTIMONIAL_PATTERNS)
    if not home_testimonials:
        home_testimonials = any(p.search(home_html) for p in REVIEW_WIDGET_PATTERNS)

    # Analyze product page if provided
    product_analysis: dict[str, bool] | None = None
    if product_page_url:
        prod_parsed = urlparse(product_page_url)
        if not prod_parsed.scheme:
            product_page_url = "https://" + product_page_url
        prod_html, prod_status = _fetch(product_page_url)
        if prod_status == 200 and prod_html:
            product_analysis = _analyze_product_page(prod_html)
    else:
        # If no product page provided, check homepage for trust signals
        # so TRUST-003 can still fire meaningfully
        product_analysis = {
            "has_specs_table": False,
            "has_testimonials": home_testimonials,
            "has_trust_badges": any(p.search(home_html) for p in TRUST_BADGE_PATTERNS),
            "has_comparison": any(p.search(home_html) for p in COMPARISON_PATTERNS),
        }

    findings = _build_findings(
        content_paths, has_blog, is_https, product_analysis, blog_detection,
    )
    recommendations = _build_recommendations(has_blog, product_analysis, site_context)

    return {
        "_probe_status": "ok",
        "base_url": url,
        "content_paths_checked": content_paths,
        "has_blog": has_blog,
        "blog_detection_method": blog_detection["method"],
        "blog_url": blog_detection["blog_url"],
        "blog_urls": blog_detection["blog_urls"],
        "blog_url_count": blog_detection["blog_count"],
        "sitemap_found": blog_detection["sitemap_found"],
        "product_page_analysis": product_analysis,
        "is_https": is_https,
        "findings": findings,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit trust-building content on a website.",
    )
    parser.add_argument("url", help="Homepage URL to audit")
    parser.add_argument(
        "--product", dest="product_page_url", default=None,
        help="Product page URL for deeper trust signal analysis",
    )
    parser.add_argument(
        "--company", dest="company_name", default=None,
        help="Company name (reserved for future media coverage search)",
    )
    args = parser.parse_args()

    result = probe(args.url, args.product_page_url, args.company_name)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
