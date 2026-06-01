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
        absence_terms: tuple[str, ...] = ("未检测到", "未发现", "缺少", "没有", "无内容"),
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
    # Independent contradiction: the off-site sitemap/freshness crawl found real
    # blog URLs (classic Shopify /blogs/<handle> false-negative).
    CrossCheck(
        rule_id_prefix="TRUST-001",
        guards="no blog / content hub",
        thing_zh="博客/资讯内容板块",
        override=_blog_override_signal,
        verifiable=_blog_verifiable_signal,
        downgrade_to="P2",
        suppress=True,
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
    if not text:
        return f"未通过{method}检测到{thing_zh}（该信号未经独立验证）。"
    # Map each confident absence verb to its honest "未通过<method>…" form so the
    # sentence stays grammatical (e.g. "未检测到" -> "未通过路径探测检测到").
    honest = {
        "未检测到": f"未通过{method}检测到",
        "未发现": f"未通过{method}发现",
        "缺少": f"未通过{method}发现",
        "没有": f"未通过{method}发现",
        "无内容": f"未通过{method}发现内容",
    }
    for term in absence_terms:
        if term in text:
            return text.replace(term, honest.get(term, f"未通过{method}{term}"), 1)
    # No matched absence term — prepend an honest qualifier.
    return f"（未通过{method}独立验证）{text}"


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
    title = finding.get("title_zh", "")
    finding["title_zh"] = _soften_wording(title, cc.thing_zh, method, cc.absence_terms)
    ev = finding.get("evidence", "")
    finding["evidence"] = _soften_wording(ev, cc.thing_zh, method, cc.absence_terms)
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
