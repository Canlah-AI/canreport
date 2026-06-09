#!/usr/bin/env python3
"""
EAC speed probe — global website speed audit for cross-border e-commerce sellers.

Combines PSI (mobile + desktop), CDN header detection, server geolocation via
ipinfo.io, and HTTP/2 check into structured findings with severity rules.

Dependencies: requests (already in project), lighthouse_psi (sibling probe).
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import sys
from typing import Any
from urllib.parse import urlparse

import requests

# Sibling probe — import at module level so the orchestrator can catch ImportError.
# Supports both `python -m probes.eac_speed_probe` and `python probes/eac_speed_probe.py`.
try:
    from probes import lighthouse_psi
except ImportError:
    import lighthouse_psi  # type: ignore[import-untyped]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 12.0

# CDN detection rules now come from the external signature registry
# (probes/detection_signatures.json). Falls back to bundled defaults when the
# JSON is absent — behavior identical to the old inline list, plus modern /
# non-Western vendors (Vercel, Netlify, Bunny, Alibaba, Tencent, Gcore, …).
try:
    from probes.signatures_config import get_cdn_header_rules
except ImportError:
    from signatures_config import get_cdn_header_rules  # type: ignore[no-redef]

CDN_HEADER_RULES: list[tuple[str, str, str]] = get_cdn_header_rules()

# Countries generally considered "China mainland".
CN_COUNTRIES = {"CN"}


def _resolve_ip(hostname: str) -> str | None:
    """Resolve hostname to IPv4. Returns None on failure."""
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, OSError):
        return None


def _geolocate_ip(ip: str) -> dict[str, str | None]:
    """Query ipinfo.io (free, no key, 50K/month) for IP geolocation."""
    result: dict[str, str | None] = {
        "ip": ip, "country": None, "region": None, "city": None, "org": None,
    }
    try:
        resp = requests.get(
            f"https://ipinfo.io/{ip}/json",
            headers={"User-Agent": "canmarket-audit/1.1"},
            timeout=8.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            result["country"] = data.get("country")
            result["region"] = data.get("region")
            result["city"] = data.get("city")
            result["org"] = data.get("org")
    except Exception:
        pass
    return result


def _detect_cdn(headers: dict[str, str]) -> tuple[str | None, str | None]:
    """Return (cdn_label, evidence_string) from response headers."""
    lower_headers = {k.lower(): v for k, v in headers.items()}
    for header_name, pattern, cdn_label in CDN_HEADER_RULES:
        value = lower_headers.get(header_name)
        if value and re.search(pattern, value):
            evidence = f"{header_name}: {value[:120]}"
            return cdn_label, evidence
    return None, None


def _check_http2(hostname: str) -> bool:
    """Check HTTP/2 support via ALPN negotiation on port 443."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    try:
        with socket.create_connection((hostname, 443), timeout=8.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                proto = ssock.selected_alpn_protocol()
                return proto == "h2"
    except Exception:
        return False


def _extract_ttfb(psi_result: dict) -> float | None:
    """Extract TTFB in ms from a PSI strategy result dict."""
    result = psi_result.get("result", psi_result)
    return result.get("ttfb_ms")


def _extract_lcp(psi_result: dict) -> float | None:
    result = psi_result.get("result", psi_result)
    return result.get("lcp_ms")


def _extract_fcp(psi_result: dict) -> float | None:
    result = psi_result.get("result", psi_result)
    return result.get("fcp_ms")


def _build_findings(
    ttfb_desktop: float | None,
    ttfb_mobile: float | None,
    cdn_detected: str | None,
    server_country: str | None,
    http2_supported: bool,
    market_label: str = "全球",
) -> list[dict[str, Any]]:
    """Apply rule engine and return structured findings list."""
    findings: list[dict[str, Any]] = []

    # SPEED-001: TTFB > 2000ms
    ttfb_worst = max(
        t for t in [ttfb_desktop, ttfb_mobile] if t is not None
    ) if any(t is not None for t in [ttfb_desktop, ttfb_mobile]) else None

    if ttfb_worst is not None and ttfb_worst > 2000:
        source = "desktop" if ttfb_desktop and ttfb_desktop >= (ttfb_mobile or 0) else "mobile"
        findings.append({
            "rule_id": "SPEED-001",
            "severity": "P0",
            "title_zh": f"{market_label} TTFB 超过 2 秒阈值",
            "evidence": f"TTFB from {source}: {ttfb_worst:,.0f}ms (目标市场: {market_label})",
            "confidence": 0.95,
            "impact_zh": (
                "B2B 客户在背景调查阶段首次访问您的网站，如果 3 秒内未加载完成，"
                "53% 的用户会直接离开。TTFB 是所有后续加载指标的基础瓶颈。"
            ),
            "action_zh": (
                "优先配置 CDN (Cloudflare 免费方案即可) 将 TTFB 降至 < 800ms，"
                "同时检查服务器端响应时间是否存在数据库慢查询或未缓存的动态渲染。"
            ),
            "code_snippet": None,
            "code_language": None,
            "paste_location": None,
            "needs_account": False,
        })

    # SPEED-002: No CDN detected
    if cdn_detected is None:
        findings.append({
            "rule_id": "SPEED-002",
            "severity": "P1",
            "title_zh": "未检测到 CDN 服务",
            "evidence": (
                "HTTP 响应头中未发现 Cloudflare / Akamai / AWS CloudFront 等 CDN 特征。"
            ),
            "confidence": 0.90,
            "impact_zh": (
                "所有海外请求直接回源到源站服务器，跨洋传输延迟不可避免。"
                "CDN 是成本最低、效果最显著的全球加速手段。"
            ),
            "action_zh": (
                "注册 Cloudflare 账号 → 添加域名 → 修改 DNS 到 Cloudflare nameserver → "
                "开启代理模式。免费方案即可覆盖大部分场景。"
            ),
            "code_snippet": (
                "# Cloudflare DNS 配置示例\n"
                "# 1. 登录 cloudflare.com → Add Site\n"
                "# 2. 选择 Free 方案\n"
                "# 3. 修改域名 DNS 到:\n"
                "#    ns1.cloudflare.com\n"
                "#    ns2.cloudflare.com\n"
                "# 4. 在 DNS 页面开启代理 (橙色云图标)"
            ),
            "code_language": "配置指南",
            "paste_location": "域名注册商 DNS 设置",
            "needs_account": False,
        })

    # SPEED-003: Server in China but target market is US/EU
    if server_country and server_country in CN_COUNTRIES:
        findings.append({
            "rule_id": "SPEED-003",
            "severity": "P1",
            "title_zh": "源站与目标市场距离过远",
            "evidence": f"服务器 IP 地理位置: {server_country} (中国大陆)",
            "confidence": 0.85,
            "impact_zh": (
                "中国大陆到北美/欧洲的网络延迟通常 200-400ms (单程)，"
                "加上 TLS 握手和跨境线路不稳定，实际 TTFB 可达 1-4 秒。"
                "这是跨境电商独立站最常见的性能瓶颈。"
            ),
            "action_zh": (
                "短期: 配置 CDN 缓存静态资源到目标市场边缘节点。"
                "长期: 考虑将源站迁移到目标市场区域 (如 AWS us-east-1 或 eu-west-1)。"
            ),
            "code_snippet": None,
            "code_language": None,
            "paste_location": None,
            "needs_account": False,
        })

    # SPEED-004: No HTTP/2
    if not http2_supported:
        findings.append({
            "rule_id": "SPEED-004",
            "severity": "P2",
            "title_zh": "未启用 HTTP/2 协议",
            "evidence": "TLS ALPN 协商结果未包含 h2 协议",
            "confidence": 0.90,
            "impact_zh": (
                "HTTP/2 支持多路复用和头部压缩，可以显著减少页面加载所需的连接数和传输量。"
                "现代 CDN 和 Web 服务器默认启用 HTTP/2。"
            ),
            "action_zh": (
                "如果使用 Nginx，在 listen 指令添加 http2; "
                "如果使用 CDN (如 Cloudflare)，HTTP/2 通常自动启用。"
            ),
            "code_snippet": (
                "# Nginx 启用 HTTP/2 示例\n"
                "server {\n"
                "    listen 443 ssl http2;\n"
                "    server_name example.com;\n"
                "    # ... SSL 证书配置 ...\n"
                "}"
            ),
            "code_language": "nginx",
            "paste_location": "Nginx 配置文件 (通常 /etc/nginx/sites-available/)",
            "needs_account": False,
        })

    return findings


def _market_label(markets: str) -> str:
    """Convert market codes to Chinese labels for findings text."""
    labels: dict[str, str] = {
        "US": "美国市场", "EU": "欧洲市场", "SEA": "东南亚市场",
        "JP": "日本市场", "KR": "韩国市场", "UK": "英国市场",
        "AU": "澳洲市场", "CA": "加拿大市场", "MX": "墨西哥市场",
        "IN": "印度市场", "BR": "巴西市场", "DE": "德国市场",
        "全球": "全球",
    }
    parts = [m.strip() for m in markets.split(",")]
    translated = [labels.get(p, p + "市场") for p in parts]
    return "/".join(translated)


def probe(url: str, markets: str = "全球") -> dict[str, Any]:
    """Run the full EAC speed probe. Returns structured result dict."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    hostname = parsed.netloc or parsed.path.split("/")[0]

    # --- 1. PSI mobile + desktop (reuse sibling probe) ---
    psi_result = lighthouse_psi.probe_both(url)
    psi_mobile = psi_result.get("mobile", {})
    psi_desktop = psi_result.get("desktop", {})

    # --- 2. CDN detection via HTTP headers ---
    cdn_detected: str | None = None
    cdn_evidence: str | None = None
    response_headers: dict[str, str] = {}
    try:
        resp = requests.head(
            url, headers={"User-Agent": UA}, timeout=TIMEOUT,
            allow_redirects=True,
        )
        response_headers = dict(resp.headers)
        cdn_detected, cdn_evidence = _detect_cdn(response_headers)
    except Exception:
        # Fall back to GET if HEAD fails (some servers block HEAD).
        try:
            resp = requests.get(
                url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                allow_redirects=True, stream=True,
            )
            response_headers = dict(resp.headers)
            cdn_detected, cdn_evidence = _detect_cdn(response_headers)
            resp.close()
        except Exception:
            pass

    # --- 3. Server IP geolocation ---
    server_ip = _resolve_ip(hostname)
    geo: dict[str, str | None] = {
        "ip": server_ip, "country": None, "region": None, "city": None, "org": None,
    }
    if server_ip:
        geo = _geolocate_ip(server_ip)

    # --- 4. HTTP/2 check ---
    http2_supported = _check_http2(hostname)

    # --- 5. Extract metrics ---
    ttfb_desktop = _extract_ttfb(psi_desktop)
    ttfb_mobile = _extract_ttfb(psi_mobile)
    lcp_desktop = _extract_lcp(psi_desktop)
    lcp_mobile = _extract_lcp(psi_mobile)
    fcp_desktop = _extract_fcp(psi_desktop)
    fcp_mobile = _extract_fcp(psi_mobile)

    # --- 6. Build findings ---
    findings = _build_findings(
        ttfb_desktop=ttfb_desktop,
        ttfb_mobile=ttfb_mobile,
        cdn_detected=cdn_detected,
        server_country=geo.get("country"),
        http2_supported=http2_supported,
        market_label=_market_label(markets),
    )

    return {
        "_probe_status": "ok",
        "url": url,
        "psi_mobile": psi_mobile,
        "psi_desktop": psi_desktop,
        "metrics_summary": {
            "ttfb_desktop_ms": ttfb_desktop,
            "ttfb_mobile_ms": ttfb_mobile,
            "lcp_desktop_ms": lcp_desktop,
            "lcp_mobile_ms": lcp_mobile,
            "fcp_desktop_ms": fcp_desktop,
            "fcp_mobile_ms": fcp_mobile,
        },
        "cdn_detected": cdn_detected,
        "cdn_evidence": cdn_evidence,
        "server_ip": geo.get("ip"),
        "server_country": geo.get("country"),
        "server_region": geo.get("region"),
        "server_city": geo.get("city"),
        "server_org": geo.get("org"),
        "http2_supported": http2_supported,
        "findings": findings,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: eac_speed_probe.py <url>", file=sys.stderr)
        return 2
    url = sys.argv[1]
    if not urlparse(url).scheme:
        url = "https://" + url
    result = probe(url)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
