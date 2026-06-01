"""Self-verification / cross-check layer for canreport.

A post-probe reconcile pass that audits high-risk NEGATIVE findings (absence
claims like "未检测到博客板块") against INDEPENDENT signals produced by other
probes. Two distinct corrections are applied per cross-check:

  1. OVERRIDE (contradiction) — a different, independent probe found the very
     thing the finding says is absent. Example: the on-site trust probe
     (TRUST-001) HEAD-guessed /blog and declared "no blog", but the off-site
     sitemap/freshness crawl actually enumerated real /blogs/<handle> URLs.
     The absence claim is FALSE -> suppress the finding (it is rewritten to a
     PASS-style note and dropped from the deduction math, OR fully suppressed).

  2. HONEST-WORDING (unverifiable) — no independent contradiction, but the
     asserting probe never actually had the coverage to prove absence (e.g. it
     only HEAD-checked a hardcoded path list, no sitemap was readable). The
     finding is kept but its severity is downgraded and the title/evidence are
     reworded from a confident absence claim ("未检测到 X") to an honest
     "未通过 <method> 检测到 X" so the report never overclaims.

Design notes
------------
* Brand-agnostic: every predicate reads only structural probe output
  (sitemap URLs, url_categories counts, blog_detected flags) — never the brand
  name or industry. The same registry works for any site.
* Mutates findings in place (these are freshly built dicts owned by
  build_contract, not shared base state) and returns the same module list so
  the call site reads naturally.
* Runs AFTER all modules (base on-site + off-site) are assembled but BEFORE the
  roadmap synthesis, score recalculation, and top-actions ranking, so a
  flipped/downgraded finding correctly stops cascading into the headline score,
  the 90-day plan, and the P0/P1 counts.
"""

from __future__ import annotations

from typing import Any, Callable

# A cross-check predicate reads the whole off-site probe payload and returns
# (signal_fired, human_readable_evidence).
Predicate = Callable[[dict[str, dict]], "tuple[bool, str]"]


# ---------------------------------------------------------------------------
# Independent signal extractors (brand-agnostic — read structural data only)
# ---------------------------------------------------------------------------

def _blog_override_signal(offsite: dict[str, dict]) -> "tuple[bool, str]":
    """Did an INDEPENDENT probe enumerate real blog/editorial URLs?

    Independent sources (NOT the on-site trust probe that raised the finding):
      * freshness (content_freshness_scan): url_categories.blog count,
        blog_detected flag, blog_url. Sitemap-derived.
      * crawlability (crawlability_scan): sitemap.url_count + any blog-shaped
        sample URL it surfaced.

    Returns (True, evidence) the moment any independent source proves a blog
    section exists, so the on-site "no blog" claim is demonstrably false.
    """
    fresh = offsite.get("freshness", {}) or {}
    url_cats = fresh.get("url_categories", {}) or {}
    blog_count = int(url_cats.get("blog", 0) or 0)
    blog_detected = bool(fresh.get("blog_detected", False))
    blog_url = fresh.get("blog_url") or fresh.get("blog_path")

    if blog_count > 0 or blog_detected:
        where = f"（如 {blog_url}）" if blog_url else ""
        return True, (
            f"独立信源（内容新鲜度爬虫的 sitemap 解析）枚举到 {blog_count} 个"
            f"博客/资讯类 URL{where}，证明站点存在内容板块。"
        )

    # Fall back to the crawlability probe's sitemap sample, if present.
    crawl = offsite.get("crawlability", {}) or {}
    sm = crawl.get("sitemap", {}) or {}
    sample = sm.get("urls") or sm.get("sample") or []
    blog_like = [u for u in sample if isinstance(u, str)
                 and ("/blog" in u.lower() or "/news" in u.lower()
                      or "/articles" in u.lower())]
    if blog_like:
        return True, (
            f"独立信源（技术爬虫的 sitemap）发现 {len(blog_like)} 个博客/资讯类 "
            f"URL（如 {blog_like[0]}），证明站点存在内容板块。"
        )

    return False, ""


def _blog_verifiable_signal(offsite: dict[str, dict]) -> "tuple[bool, str]":
    """Could the absence be HONESTLY verified, or was it only a path guess?

    Returns (could_verify, method_label):
      * True  — a sitemap was readable, so "no blog URL in sitemap" is a real,
        verified negative; the confident wording is justified.
      * False — no sitemap was readable; the on-site probe only HEAD-guessed a
        hardcoded path list. We can't truthfully claim absence; the wording
        must be softened to "未通过路径探测检测到 …".
    """
    fresh = offsite.get("freshness", {}) or {}
    crawl = offsite.get("crawlability", {}) or {}
    sm = crawl.get("sitemap", {}) or {}

    sitemap_readable = bool(
        fresh.get("sitemap_found")
        or fresh.get("total_urls_in_sitemap", 0)
        or sm.get("present")
        or sm.get("url_count", 0)
    )
    if sitemap_readable:
        return True, "sitemap"
    return False, "路径探测 (HEAD)"


def _blog_crawl_override_signal(offsite: dict[str, dict]) -> "tuple[bool, str]":
    """SECOND, independent-of-sitemap signal that a blog section exists.

    The original ``_blog_override_signal`` leans on sitemap-derived data
    (freshness.url_categories / crawlability.sitemap). When the sitemap is
    ABSENT — exactly the case where TRUST-001's HEAD-guess fires — that signal
    has nothing to read, so the override would be circular (same missing
    sitemap the asserting probe lacked).

    This extractor instead reads what the crawlability probe ACTUALLY CRAWLED:
    nav-link mining to click-depth 2 + redirect/canonical evidence. If the
    crawler ever followed a link to a ``/blog``-shaped URL (e.g. it recorded a
    redirect chain starting at /blog, or surfaced a /blog canonical), that is a
    real, sitemap-independent confirmation the section is reachable from the
    site's own navigation.

    Returns (True, evidence) when such a crawled blog URL is found.
    """
    crawl = offsite.get("crawlability", {}) or {}

    def _looks_blog(u: object) -> bool:
        if not isinstance(u, str):
            return False
        low = u.lower()
        return ("/blog" in low or "/news" in low or "/articles" in low
                or "/journal" in low)

    crawled_urls: list[str] = []
    # Redirect chains/loops the crawler actually walked (nav-followed pages).
    redirects = crawl.get("redirects", {}) or {}
    for bucket in ("loops", "chains"):
        for entry in (redirects.get(bucket) or []):
            if isinstance(entry, dict):
                if isinstance(entry.get("start"), str):
                    crawled_urls.append(entry["start"])
                for u in (entry.get("chain") or []):
                    crawled_urls.append(u)
    # Canonical-tag scan over crawled pages (also a crawl-derived URL list).
    canonical = crawl.get("canonical", {}) or {}
    for key in ("missing", "cross_canonical"):
        crawled_urls.extend(canonical.get(key) or [])
    # Orphan / internal-link mining, if the probe surfaced explicit URLs.
    internal = crawl.get("internal_links", {}) or {}
    crawled_urls.extend(internal.get("orphan_pages") or [])

    blog_like = [u for u in crawled_urls if _looks_blog(u)]
    if blog_like:
        return True, (
            f"第二独立信源（技术爬虫沿首页导航爬取至 depth 2，不依赖 sitemap）"
            f"实际访问到 {len(blog_like)} 个博客/资讯类 URL（如 {blog_like[0]}），"
            f"证明该板块从站点导航可达。"
        )
    return False, ""


def _blog_independent_override(offsite: dict[str, dict]) -> "tuple[bool, str]":
    """Combine BOTH independent blog signals so the override is non-circular.

    Fires if EITHER the sitemap-based signal OR the crawl-nav-based signal
    confirms a blog. The crawl-nav signal is the one that survives an absent
    sitemap, breaking the original shared-source flaw where the override read
    the same sitemap the asserting HEAD-probe never had.
    """
    fired, ev = _blog_override_signal(offsite)
    if fired:
        return True, ev
    return _blog_crawl_override_signal(offsite)


# ---------------------------------------------------------------------------
# Reputation independent signals (REPUT-*)
# ---------------------------------------------------------------------------
# These absence claims are judged from ONE SERP region / ONE surface probe.
# Absence-of-evidence is NOT evidence-of-absence, so they are softened — except
# Facebook, which an INDEPENDENT probe (social_influence_scan) can override.

def _serp_region_label(offsite: dict[str, dict]) -> str:
    """Best-effort human label for the SERP region the reputation probe used.

    The reputation/reviews probe queries ONE SERP locale. We surface that
    locale so the softened wording reads honestly ("未在 {region} SERP 检测到")
    instead of implying a global negative. Falls back to a neutral phrase when
    no region code is propagated.
    """
    rep = offsite.get("reputation", {}) or {}
    for key in ("serp_region", "region", "region_code", "gl", "market"):
        val = rep.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().upper()
    return "单一 SERP 区域"


def _facebook_override_signal(offsite: dict[str, dict]) -> "tuple[bool, str]":
    """Did the INDEPENDENT social probe find a Facebook URL?

    The reputation probe's "未链接 Facebook 商业主页" reads
    reputation.facebook.page_url. The social_influence_scan probe independently
    extracts on-site social links into social.platforms.facebook.url. If THAT
    probe found a Facebook URL, the reputation "no Facebook" claim is false.
    """
    social = offsite.get("social", {}) or {}
    platforms = social.get("platforms", {}) or {}
    fb = platforms.get("facebook", {}) or {}
    fb_url = fb.get("url")
    if isinstance(fb_url, str) and fb_url.strip():
        return True, (
            f"独立信源（社媒影响力扫描的站内链接挖掘）在站点上检测到 Facebook 主页"
            f"链接（{fb_url}），声誉探针的「无 Facebook」判定不成立。"
        )
    return False, ""


def _reput_region_verifiable(offsite: dict[str, dict]) -> "tuple[bool, str]":
    """Reputation SERP absences are NEVER region-complete -> always soften.

    A single-locale SERP/listing query cannot prove a brand has no Google
    reviews / no TripAdvisor listing globally. We always return could_verify
    =False so the finding is reworded to "未在 {region} SERP 检测到 …", carrying
    the region label as the method so the honest wording is region-scoped.
    """
    label = _serp_region_label(offsite)
    # Avoid a double "SERP" when the fallback label already says it.
    method = label if "SERP" in label else f"{label} SERP"
    return False, method


# ---------------------------------------------------------------------------
# On-site single-source absences with no independent off-site signal (TRACK/SPEED)
# ---------------------------------------------------------------------------
# These come from ONE homepage-HTML / ONE response-header scrape. There is no
# independent probe in `offsite` that could contradict them, so they can only be
# DOWNGRADED to honest "未通过X检测到" wording — never overridden, never asserted
# as flat absence.

def _homepage_html_verifiable(_offsite: dict[str, dict]) -> "tuple[bool, str]":
    """Tracking absence is from a single homepage-HTML scrape -> soften.

    GTM-injected tags, server-side tagging, and consent-gated tags are all
    invisible to a one-shot homepage HTML fetch. We can never honestly assert
    "no tracking at all", so always soften to "首页 HTML 未检测到 …".
    """
    return False, "首页 HTML"


def _response_header_verifiable(_offsite: dict[str, dict]) -> "tuple[bool, str]":
    """CDN absence is from a single response-header probe -> soften.

    CNAME-flattened Cloudflare, origin-pull CDNs, and header-stripping configs
    routinely hide from a single header read. We never assert "no CDN", only
    "未通过响应头检测到 CDN".
    """
    return False, "响应头"


# ---------------------------------------------------------------------------
# CrossCheck registry entry
# ---------------------------------------------------------------------------

class CrossCheck:
    """One absence-claim guard.

    rule_id_prefix : findings whose rule_id starts with this are guarded.
    guards         : human label of the absence claim (for logs / evidence).
    absence_terms  : substrings in title/evidence we rewrite to honest wording
                     when the claim is unverifiable (e.g. "未检测到").
    thing_zh       : the noun the claim is about (e.g. "博客/资讯内容板块").
    override       : Predicate -> (contradicted, evidence). True suppresses.
    verifiable     : Predicate -> (could_verify, method). False softens wording.
    downgrade_to   : severity to drop to when softened (default P2).
    suppress       : when overridden, drop the finding entirely (True) vs.
                     convert it to a PASS-style note kept for transparency.
    """

    def __init__(
        self,
        rule_id_prefix: str,
        guards: str,
        thing_zh: str,
        override: Predicate | None = None,
        verifiable: Predicate | None = None,
        absence_terms: tuple[str, ...] = (
            "未检测到", "未发现", "缺少", "没有", "无内容",
            "无收录", "无 ", "未链接", "未出现",
        ),
        downgrade_to: str = "P2",
        suppress: bool = True,
    ) -> None:
        self.rule_id_prefix = rule_id_prefix
        self.guards = guards
        self.thing_zh = thing_zh
        self.override = override
        self.verifiable = verifiable
        self.absence_terms = absence_terms
        self.downgrade_to = downgrade_to
        self.suppress = suppress

    def matches(self, finding: dict[str, Any]) -> bool:
        rule_id = str(finding.get("rule_id", ""))
        return rule_id.startswith(self.rule_id_prefix)


# ---------------------------------------------------------------------------
# Cross-reference map: high-risk NEGATIVE findings -> how to cross-check them
# ---------------------------------------------------------------------------
# Keyed by the rule_id prefix of the absence-claim finding. To add a new guard,
# append a CrossCheck with its override/verifiable predicates — no other code
# changes needed.

CROSS_CHECKS: list[CrossCheck] = [
    # On-site trust probe TRUST-001: "未检测到博客/资讯内容板块".
    # Independent contradiction: the off-site sitemap/freshness crawl OR the
    # crawlability nav-link mining found real blog URLs (classic Shopify
    # /blogs/<handle> false-negative).
    #
    # FIX (shared-source flaw): the override now reads TWO independent signals
    # via _blog_independent_override — the sitemap-based one AND a second,
    # crawl-nav-based one (_blog_crawl_override_signal). The asserting probe
    # HEAD-guessed a path list with NO sitemap; the crawl-nav signal does not
    # depend on a sitemap, so the override is no longer circular.
    CrossCheck(
        rule_id_prefix="TRUST-001",
        guards="no blog / content hub",
        thing_zh="博客/资讯内容板块",
        override=_blog_independent_override,
        verifiable=_blog_verifiable_signal,
        downgrade_to="P2",
        suppress=True,
    ),

    # --- Reputation: single-SERP-region absence claims (REPUT-*) ------------
    # REPUT-001: "无 Google Business Profile 评价" — judged from ONE SERP region.
    # Absence-of-evidence ≠ evidence-of-absence -> soften to
    # "未在 {region} SERP 检测到 …". No independent override exists.
    CrossCheck(
        rule_id_prefix="REPUT-001",
        guards="no Google reviews in SERP (single region)",
        thing_zh="Google Business Profile 评价",
        override=None,
        verifiable=_reput_region_verifiable,
        downgrade_to="P2",
        suppress=False,
    ),
    # REPUT-004: "未链接 Facebook 商业主页" — reputation probe read ONE surface.
    # OVERRIDE: the independent social_influence_scan probe may have found a
    # Facebook URL on the site. If so, suppress the false "no Facebook" claim.
    # If not, soften to "未在 {region} SERP 检测到 Facebook 主页".
    CrossCheck(
        rule_id_prefix="REPUT-004",
        guards="no Facebook page (single-surface)",
        thing_zh="Facebook 商业主页",
        override=_facebook_override_signal,
        verifiable=_reput_region_verifiable,
        downgrade_to="P2",
        suppress=True,
    ),
    # REPUT-003 / REPUT-005: "无 TripAdvisor 收录" — single-region listing query.
    # No independent contradiction available -> soften to region-scoped wording.
    CrossCheck(
        rule_id_prefix="REPUT-003",
        guards="no TripAdvisor listing (single region)",
        thing_zh="TripAdvisor 收录",
        override=None,
        verifiable=_reput_region_verifiable,
        downgrade_to="P2",
        suppress=False,
    ),
    CrossCheck(
        rule_id_prefix="REPUT-005",
        guards="no TripAdvisor listing (single region)",
        thing_zh="TripAdvisor 收录",
        override=None,
        verifiable=_reput_region_verifiable,
        downgrade_to="P2",
        suppress=False,
    ),

    # --- Tracking: single homepage-HTML scrape (TRACK-*) -------------------
    # TRACK-001: "Thank You 页面未检测到转化追踪代码" — a one-shot HTML scrape
    # misses GTM-container / server-side / consent-gated tags. Never assert flat
    # absence; soften to "首页 HTML 未检测到 …（可能为服务端/GTM 容器加载）".
    CrossCheck(
        rule_id_prefix="TRACK-001",
        guards="no conversion tracking (single homepage HTML scrape)",
        thing_zh="追踪代码（可能为服务端/GTM 容器加载）",
        override=None,
        verifiable=_homepage_html_verifiable,
        downgrade_to="P2",
        suppress=False,
    ),

    # --- Speed: single response-header CDN probe (SPEED-*) -----------------
    # SPEED-002: "未检测到 CDN 服务" — one header read; CNAME-flattened Cloudflare
    # and origin-pull CDNs are often header-invisible. Soften to
    # "未通过响应头检测到 CDN" — never flat absence.
    CrossCheck(
        rule_id_prefix="SPEED-002",
        guards="no CDN (single response-header probe)",
        thing_zh="CDN",
        override=None,
        verifiable=_response_header_verifiable,
        downgrade_to="P2",
        suppress=False,
    ),
]


# ---------------------------------------------------------------------------
# Wording helpers
# ---------------------------------------------------------------------------

def _soften_wording(text: str, thing_zh: str, method: str,
                    absence_terms: tuple[str, ...]) -> str:
    """Rewrite a confident absence claim into honest "未通过X检测到" wording.

    "未检测到博客/资讯内容板块" -> "未通过路径探测 (HEAD) 检测到博客/资讯内容板块".
    Falls back to a generic honest sentence if no absence term is present.
    """
    # A "SERP"/region scope reads more naturally as "未在 {region} 检测到" than
    # "未通过 {region} 检测到" — pick the right preposition by method shape.
    is_scope = method.endswith("SERP") or method.endswith("区域")
    lead = f"未在{method}" if is_scope else f"未通过{method}"
    if not text:
        return f"{lead}检测到{thing_zh}（该信号未经独立验证）。"
    # Map each confident absence verb to its honest "未在/未通过<method>…" form so
    # the sentence stays grammatical (e.g. "未检测到" -> "未通过路径探测检测到",
    # "无收录" -> "未在 US SERP 检测到收录").
    honest = {
        "未检测到": f"{lead}检测到",
        "未发现": f"{lead}发现",
        "缺少": f"{lead}发现",
        "没有": f"{lead}发现",
        "无内容": f"{lead}发现内容",
        "无收录": f"{lead}检测到收录",
        "无 ": f"{lead}检测到 ",
        "未链接": f"{lead}检测到",
        "未出现": f"{lead}检测到",
    }
    for term in absence_terms:
        if term in text:
            return text.replace(term, honest.get(term, f"{lead}{term}"), 1)
    # No matched absence term — prepend an honest qualifier.
    return f"（{lead}独立验证）{text}"


def _mark_overridden(finding: dict[str, Any], cc: CrossCheck, evidence: str) -> None:
    """Convert an overridden absence finding into a PASS-style note in place."""
    finding["severity"] = "PASS"
    finding["title_zh"] = f"{cc.thing_zh}：已通过独立信源确认存在"
    finding["impact_zh"] = (
        f"原判定为「缺失{cc.thing_zh}」，但交叉核验后被独立信源推翻——"
        "该判定已撤销，不计入风险评分。"
    )
    finding["evidence"] = evidence
    finding["_reconciled"] = "overridden"
    # Drop any action that assumed the thing was missing.
    finding["action_zh"] = ""


def _mark_softened(finding: dict[str, Any], cc: CrossCheck, method: str) -> None:
    """Downgrade + reword an unverifiable absence finding in place."""
    finding["severity"] = cc.downgrade_to
    # Reword every field that can carry a confident absence claim, so the report
    # never overclaims in the title, the impact narrative, OR the raw evidence.
    for field in ("title_zh", "impact_zh", "evidence"):
        text = finding.get(field, "")
        if text:
            finding[field] = _soften_wording(text, cc.thing_zh, method, cc.absence_terms)
    finding["_reconciled"] = "softened"
    finding["confidence"] = min(float(finding.get("confidence", 0.9) or 0.9), 0.6)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reconcile_modules(modules: list[dict], offsite: dict[str, dict]) -> list[dict]:
    """Cross-check absence claims across all modules against independent signals.

    For each finding matching a CrossCheck:
      1. If an independent probe CONTRADICTS the absence claim -> suppress it
         (drop entirely, or flip to a PASS note for transparency).
      2. Else if the asserting probe could NOT honestly verify absence ->
         downgrade severity and reword to "未通过X检测到" wording.
      3. Else leave it untouched (a genuine, verified negative).

    Returns the same `modules` list (mutated in place) so the caller can write
    `modules = reconcile_modules(modules, offsite)` naturally.
    """
    if not modules:
        return modules

    for module in modules:
        findings = module.get("findings") or []
        kept: list[dict] = []
        for finding in findings:
            cc = _match_check(finding)
            if cc is None:
                kept.append(finding)
                continue

            # 1. Independent contradiction wins — the claim is false.
            contradicted, ev = (False, "")
            if cc.override is not None:
                contradicted, ev = cc.override(offsite)
            if contradicted:
                if cc.suppress:
                    # Drop it entirely from the rendered findings + deduction math.
                    continue
                _mark_overridden(finding, cc, ev)
                kept.append(finding)
                continue

            # 2. No contradiction, but was absence even verifiable?
            could_verify, method = (True, "")
            if cc.verifiable is not None:
                could_verify, method = cc.verifiable(offsite)
            if not could_verify:
                _mark_softened(finding, cc, method)
                kept.append(finding)
                continue

            # 3. Genuine, verified negative — keep as-is.
            kept.append(finding)

        module["findings"] = kept

    return modules


def _match_check(finding: dict[str, Any]) -> CrossCheck | None:
    for cc in CROSS_CHECKS:
        if cc.matches(finding):
            return cc
    return None
