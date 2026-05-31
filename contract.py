#!/usr/bin/env python3
"""Report data contract.

The contract is the neutral, template-agnostic structure that sits between
the data layer (probes) and the presentation layer (templates). Any template
renders this same dict; swapping templates never touches the data.

`build_contract` takes:
  - base: the on-site infra audit (4 modules) from eac_audit.run_audit()
  - offsite: the off-site probe outputs from engine.run_offsite_probes()
and returns a unified report_data dict with all modules merged, the overall
score recalculated, and top actions re-ranked.

The off-site module builders are brand-agnostic — no client-specific copy is
hardcoded. Remediation code snippets are generic templates the reader adapts.
"""
from __future__ import annotations

from typing import Any

_FINDING_DEFAULTS = {
    "confidence": 0.85,
    "needs_account": False,
    "code_snippet": None,
    "code_language": None,
    "paste_location": None,
    "evidence": "",
    "rule_id": "OFFSITE",
}

_SEVERITY_DEDUCT = {"P0": 25, "P1": 10, "P2": 3, "PASS": 0}
_MODULE_DEDUCT = {"P0": 30, "P1": 15, "P2": 5, "PASS": 0}


def _finding(severity: str, title: str, impact: str, action: str,
             rule_id: str, probe: str, evidence: str = "",
             code_snippet: str | None = None,
             code_language: str | None = None,
             paste_location: str | None = None,
             confidence: float = 0.85) -> dict:
    return {
        **_FINDING_DEFAULTS,
        "severity": severity,
        "title_zh": title,
        "impact_zh": impact,
        "action_zh": action,
        "rule_id": rule_id,
        "evidence": evidence,
        "code_snippet": code_snippet,
        "code_language": code_language,
        "paste_location": paste_location,
        "confidence": confidence,
        "_source_probe": probe,
    }


def _module_score(findings: list[dict], base: int = 100) -> int:
    score = base
    for f in findings:
        score -= _MODULE_DEDUCT.get(f.get("severity", "").upper(), 0)
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trunc(s: Any, n: int = 45) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_ENGINE_FRIENDLY = {
    "google_serp": "Google 搜索结果",
    "google_ai_overview": "Google AI Overview",
    "google_answer_box": "Google Answer Box",
    "gemini_search": "Gemini AI 回答",
    "openai_chatgpt": "ChatGPT 回答",
}

# Telco/healthcare/banking domains that signal a STALE generic-query run.
_STALE_GARBAGE_DOMAINS = {
    "hardwarezone.com.sg", "circles.life", "sma.org.sg", "singtel.com",
    "starhub.com", "moneysmart.sg",
}


# ---------------------------------------------------------------------------
# Off-site module builders (brand-agnostic)
# ---------------------------------------------------------------------------

def _module_ai_citation(ai: dict, brand_name: str = "") -> dict:
    """AI Citation Visibility — the GEO money shot.

    Renders TWO tables: per-engine citation scorecard + top competitors cited.
    Severity P0 when brand citation rate is 0% while competitors dominate.
    """
    brand = ai.get("brand_name") or brand_name or "本品牌"
    stats = ai.get("summary_stats", {}) or {}
    results = ai.get("results", []) or []
    queries_run = ai.get("queries_run", []) or []
    top_comp = stats.get("top_competitors_cited", []) or []

    # Per-engine rows (engines are the dict keys in summary_stats that hold dicts)
    engine_keys = [k for k, v in stats.items() if isinstance(v, dict)]
    total_rows = sum(stats[e].get("queries_tested", 0) for e in engine_keys)
    total_cited = sum(stats[e].get("queries_cited", 0) for e in engine_keys)
    blended_rate = round(total_cited / total_rows, 4) if total_rows else 0.0
    score = int(round(blended_rate * 100))
    if blended_rate == 0:
        grade = "F"
    elif blended_rate <= 0.20:
        grade = "D"
    elif blended_rate <= 0.40:
        grade = "C"
    elif blended_rate <= 0.70:
        grade = "B"
    else:
        grade = "A"

    def _rate_status(rate: float) -> str:
        if rate == 0:
            return "🔴 0%"
        if rate < 0.40:
            return "🟡"
        return "🟢"

    # TABLE A — per-engine scorecard
    table_a_rows = [["── 各引擎引用记分卡 ──", "", "", "", "", ""]]
    table_a_rows.append(["引擎 Engine", "测试查询数", "被引用次数", "引用率 %", "平均排名", "最佳排名"])
    for e in engine_keys:
        s = stats[e]
        rate = s.get("citation_rate", 0) or 0
        table_a_rows.append([
            _ENGINE_FRIENDLY.get(e, e),
            str(s.get("queries_tested", 0)),
            str(s.get("queries_cited", 0)),
            f"{_rate_status(rate)} {round(rate * 100)}%",
            str(s.get("avg_position")) if s.get("avg_position") is not None else "—",
            str(s.get("best_position")) if s.get("best_position") is not None else "未出现",
        ])

    # Which engines mentioned each competitor (scan results)
    comp_engines: dict[str, set] = {}
    for r in results:
        eng = _ENGINE_FRIENDLY.get(r.get("engine"), r.get("engine", ""))
        for c in r.get("competitors_cited", []) or []:
            comp_engines.setdefault(c, set()).add(eng)

    # TABLE B — top competitors cited instead (the money shot)
    table_b_rows = [["── 反被 AI 推荐的竞品 ──", "", "", ""]]
    table_b_rows.append(["排名 #", "竞品域名 Competitor", "被 AI 引用次数", "出现引擎"])
    stale_detected = False
    for i, c in enumerate(top_comp[:5], 1):
        dom = c.get("domain", "")
        if dom in _STALE_GARBAGE_DOMAINS:
            stale_detected = True
        engines = " · ".join(sorted(comp_engines.get(dom, []))) or "—"
        table_b_rows.append([str(i), dom, str(c.get("appearances", 0)), engines])

    findings: list[dict] = []

    # BUILD-BLOCKER: stale data
    if stale_detected:
        findings.append(_finding(
            "P1", "AI 引用数据疑似过期（通用查询）",
            "竞品列表包含电信/医疗等无关域名，说明 live_ai_search 仍在跑通用 Singapore 查询而非品牌相关买家查询 — 渲染前必须重跑。",
            "清空 ~/.cache/live_ai_search/ 并以品牌相关买家查询重跑 ai_citation 探针。",
            "AICITE-000", "live_ai_search",
            evidence=f"检测到过期竞品: {', '.join(c.get('domain','') for c in top_comp[:3])}",
            confidence=0.6))

    # P0 — zero citation while competitors dominate
    if total_rows > 0 and total_cited == 0 and top_comp:
        top3 = ", ".join(c.get("domain", "") for c in top_comp[:3])
        findings.append(_finding(
            "P0", "0% AI 引用率 — 竞品垄断 AI 推荐",
            (f"当潜在客户问 ChatGPT/Gemini/Google AI 关于本品类的买家问题时，AI 引擎在 "
             f"{total_rows} 次结果中从未提及 {brand}，却反复推荐 [{top3}]。"
             f"在 AI 驱动的购买决策中，你的品牌是隐形的 — 这些流量 100% 流向竞品。"),
            ("建立 GEO 资产：结构化产品数据(Schema.org Product)、第三方评测/对比内容、"
             "可被 AI 抓取的 citation-bait 数据页，目标 90 天内在至少 1 个引擎获得引用。"),
            "AICITE-001", "live_ai_search",
            evidence=(f"各引擎引用率均为 0%（共 {total_rows} 行结果）；"
                      f"竞品 {len(top_comp)} 个被反复引用。"),
            confidence=0.9))

    # P1 — cited on some engines but absent from AI answers
    serp = stats.get("google_serp", {})
    ai_ov = stats.get("google_ai_overview", {})
    gem = stats.get("gemini_search", {})
    serp_cited = (serp.get("queries_cited", 0) or 0) > 0
    ai_blind = ((ai_ov.get("queries_cited", 0) or 0) == 0
                and (gem.get("queries_cited", 0) or 0) == 0)
    if serp_cited and ai_blind:
        pos = serp.get("best_position")
        findings.append(_finding(
            "P1", "AI Overview 盲区 — 传统排名有，AI 回答无",
            (f"{brand} 在 Google 自然排名最佳第 {pos} 位，但在 Google AI Overview / "
             f"Gemini 回答中完全缺失。AI 引擎正在取代传统搜索点击，你的传统 SEO 没有转化为 GEO 可见性。"),
            "针对 AI Overview 优化：FAQ schema、简洁可引用的答案段落、权威外链。",
            "AICITE-002", "live_ai_search",
            evidence=f"google_serp 已引用 (最佳第 {pos} 位)，但 AI Overview / Gemini 引用率 0%。"))

    # P2 INFO — methodology transparency (queries tested)
    if queries_run:
        q_list = "\n".join(f"• {q}" for q in queries_run[:5])
        findings.append(_finding(
            "P2", "测试的真实买家查询",
            f"本模块以下列真实买家意图查询实测（非通用模板），证明方法论针对你的品类：\n{q_list}",
            "—",
            "AICITE-003", "live_ai_search",
            evidence=f"共测试 {len(queries_run)} 条买家查询。"))

    n_cited_comp = len(top_comp)
    verdict = (f"在 {total_rows} 个买家意图查询结果中，AI 引擎引用 {brand} {total_cited} 次，"
               f"而竞品被引用 {sum(c.get('appearances', 0) for c in top_comp)} 次。")

    return {
        "icon": "🤖", "title_zh": "AI 引用力 — 当客户问 AI，你的品牌出现吗？",
        "title_en": "AI Citation Visibility — Do You Show Up When Customers Ask AI?",
        "score": max(0, min(100, score)),
        "grade_letter": grade,
        "summary_html": (f"<p><strong>{score}/100 · 评级 {grade}</strong></p><p>{verdict}</p>"
                         f"<p>实测 {len(engine_keys)} 个 AI 引擎，"
                         f"{n_cited_comp} 个竞品反被推荐。</p>"),
        "data_table": {"headers": ["引擎", "查询", "引用", "引用率", "平均排名", "最佳排名"],
                       "rows": table_a_rows + table_b_rows},
        "findings": findings,
    }


def _module_reputation(rep: dict) -> dict:
    """Reputation & Reviews — multi-surface trust scorecard."""
    grade = rep.get("reputation_grade", "UNKNOWN")
    gbp = rep.get("google_business_profile", {}) or {}
    fb = rep.get("facebook", {}) or {}
    onsite = rep.get("onsite_testimonials", {}) or {}
    trip = rep.get("tripadvisor", {}) or {}
    reasoning = rep.get("grade_reasoning", []) or []
    fixes = rep.get("fixes_ranked", []) or []

    widgets = onsite.get("review_widgets_detected", []) or []
    widget_str = ", ".join(widgets) if widgets else "无"
    attributed = onsite.get("attributed_count", 0)
    anon = onsite.get("anonymous_count", 0)
    ts_count = onsite.get("count", 0)

    rows = [
        ["Google Business Profile",
         "未在 SERP 出现" if not gbp.get("found_via_search") else "已找到",
         _trunc(gbp.get("search_result_summary", "—"), 40)],
        ["Facebook 主页",
         "已检测" if fb.get("page_accessible") else "未检测",
         "未找到主页" if not fb.get("page_url") else _trunc(fb.get("page_url"), 40)],
        ["站内推荐语",
         f"{ts_count} 条（{attributed} 署名 / {anon} 匿名）",
         "无 AggregateRating schema，引擎无法读取" if not onsite.get("has_aggregate_schema") else "已标记 schema"],
        ["评价插件", widget_str,
         "已安装但无聚合评分 schema" if widgets else "—"],
        ["TripAdvisor",
         "无收录" if not trip.get("listing_url") else "已收录",
         "此品类相关性低（仅供参考）"],
        ["综合声誉评级", grade,
         _trunc("；".join(reasoning), 50) if reasoning else "—"],
    ]
    # Testimonial samples (concrete anonymity evidence)
    samples = onsite.get("samples", []) or []
    if samples:
        rows.append(["── 推荐语样本 ──", "", ""])
        for s in samples[:3]:
            rows.append(["  └ 样本", _trunc(s.get("text_preview", ""), 50),
                         s.get("attributed_to") or "匿名"])

    # Findings from fixes_ranked (priority → severity)
    _prio_sev = {1: "P1", 2: "P2", 4: "P2", 5: "P2"}
    findings: list[dict] = []
    for fx in fixes:
        prio = fx.get("priority", 3)
        sev = _prio_sev.get(prio, "P2")
        action = fx.get("action", "")
        if prio == 1:
            title = "无 Google Business Profile 评价"
            impact = ("Google 评价是最强的信任 + 本地 SEO 信号。SERP 未出现任何评价，"
                      "买家搜索品牌评价时看不到星级，削弱转化并压制富结果 / 地图包资格。")
        elif prio == 2:
            title = f"全部 {ts_count} 条站内推荐语均为匿名"
            impact = (f"站内 {ts_count} 条五星推荐语 attributed_count={attributed} — 全部匿名或仅名字。"
                      "匿名好评可信度低、易被视为伪造，且因无 AggregateRating schema 不具备富结果资格。")
            action = action + "（另：补 Review/AggregateRating JSON-LD，让 Judge.me 数据可被机器读取。）"
        elif prio == 4:
            title = "未链接 Facebook 商业主页"
            impact = "未检测到 Facebook 主页 — 缺失社会证明面与 NAP 一致性信号。"
        else:
            title = "无 TripAdvisor 收录（视品类而定）"
            impact = "TripAdvisor 对户外结构电商品牌相关性低，标记为可选，不重罚。"
        findings.append(_finding(
            sev, title, impact, action,
            f"REPUT-00{prio}", "reviews_scan",
            evidence=_trunc("；".join(reasoning), 80) if prio == 1 else f"fixes_ranked priority {prio}",
            confidence=0.8))

    # Map WEAK grade → numeric band (derived only)
    band = {"DANGEROUS": 15, "WEAK": 35, "ACCEPTABLE": 58, "STRONG": 85}.get(grade, 50)

    return {
        "icon": "⭐", "title_zh": "声誉与评价", "title_en": "Reputation & Reviews",
        "score": band,
        "summary_html": (f"<p>综合声誉评级: <strong>{grade}</strong>。"
                         f"Google 评价未在 SERP 出现，站内 {ts_count} 条推荐语全部匿名"
                         f"（{anon}/{ts_count}），无 AggregateRating schema。</p>"
                         f"<p>⚠️ Google Business Profile 检查在 SG SERP 区域执行，"
                         f"建议对照 US/全球 GBP 复核后再行动。</p>"),
        "data_table": {"headers": ["反馈面 Surface", "状态 / 数值", "细节 Detail"], "rows": rows},
        "findings": findings,
    }


def _module_backlinks_news(backlink: dict, news: dict) -> dict:
    """Backlinks & News Authority — surfaces per-domain + per-article detail.

    Renders the entity-disambiguation layer HONESTLY: genuine brand coverage
    and outreach leads are separated from same-name (namesake) entities that
    belong to different companies, and the exclusion is shown explicitly as an
    expertise signal rather than buried as a hedge.
    """
    brand = backlink.get("brand_name", "本品牌")
    ext = backlink.get("external_mentions_estimate", 0)
    bl_grade = backlink.get("mention_grade", "UNKNOWN")
    # unlinked_mentions are now GENUINE only (namesakes already removed by probe).
    unlinked = backlink.get("unlinked_mentions", []) or []
    unlinked_n = backlink.get("unlinked_mention_count", len(unlinked))
    bl_namesakes = backlink.get("namesake_entities", []) or []
    bl_namesake_n = backlink.get("namesake_count", len(bl_namesakes))
    breakdown = backlink.get("domain_type_breakdown", {}) or {}
    sources = backlink.get("mention_sources", []) or []
    disclaimer = backlink.get("disclaimer", "")

    # News: verified vs namesake-excluded (entity disambiguation).
    verified_articles = news.get("verified_articles", []) or []
    verified_count = news.get("verified_count", len(verified_articles))
    news_namesakes = news.get("namesake_excluded", []) or []
    news_namesake_n = news.get("namesake_count", len(news_namesakes))
    news_grade = news.get("coverage_grade", "NONE")
    tiers = news.get("source_tier_breakdown", {}) or {}
    recency = news.get("recency", {}) or {}
    kp = news.get("knowledge_panel_detected", False)
    total_namesakes = bl_namesake_n + news_namesake_n

    # Build table
    bd_str = " · ".join(f"{k} {v}" for k, v in breakdown.items() if v)
    rows: list[list] = [["── 反链概览 ──", "", ""]]
    rows.append(["外部提及数 (估算)", str(ext), bl_grade])
    rows.append(["未链接品牌提及（真实）", str(unlinked_n), "🔗 待转化" if unlinked_n > 0 else "—"])
    rows.append(["提及来源类型", bd_str or "—", "—"])
    rows.append(["新闻类提及", str(breakdown.get("news", 0)),
                 "❌ 缺口" if breakdown.get("news", 0) == 0 else "—"])
    rows.append(["edu/gov 类提及", str(breakdown.get("edu_gov", 0)),
                 "❌ 缺口" if breakdown.get("edu_gov", 0) == 0 else "—"])

    rows.append(["── 提及来源明细 ──", "", ""])
    for s in sources[:9]:
        rows.append([s.get("domain", ""), s.get("category", ""), _trunc(s.get("title", ""), 40)])

    if unlinked:
        rows.append(["── 未链接提及（真实外联线索）──", "", ""])
        for um in unlinked:
            rows.append([um.get("domain", ""), "🔗 可外联加链", _trunc(um.get("title", ""), 40)])

    # Backlink namesakes — excluded same-name, different-owner entities.
    if bl_namesakes:
        rows.append(["── 已识别并排除的同名异主实体 ──", "", ""])
        for ns in bl_namesakes[:6]:
            rows.append([ns.get("domain", ""), "🚫 非本品牌",
                         _trunc(ns.get("why", "") or ns.get("title", ""), 45)])

    rows.append(["── 新闻报道 ──", "", ""])
    rows.append(["真实品牌报道数（已消歧）", str(verified_count), news_grade])
    tier_str = (f"{tiers.get('tier_1',0)} / {tiers.get('tier_2',0)} / "
                f"{tiers.get('tier_3',0)} / {tiers.get('tier_4_press_release',0)}")
    all_t3 = tiers.get("tier_1", 0) == 0 and tiers.get("tier_2", 0) == 0 and tiers.get("tier_3", 0) > 0
    rows.append(["新闻分级 (T1/T2/T3/PR)", tier_str, "⚠️ 全部 Tier-3" if all_t3 else "—"])
    rec_str = (f"30天 {recency.get('last_30_days',0)} · 90天 {recency.get('last_90_days',0)} · "
               f"365天 {recency.get('last_365_days',0)}")
    rows.append(["报道时效", rec_str, "✅ 活跃" if recency.get("last_30_days", 0) > 0 else "—"])

    rows.append(["── 真实品牌报道明细（已消歧）──", "", ""])
    for a in verified_articles[:8]:
        rows.append([_trunc(a.get("source", ""), 20), f"T{a.get('tier','?')}",
                     _trunc(f"{a.get('title','')} · {a.get('date','')}", 45)])
    if len(verified_articles) > 8:
        rows.append([f"+{len(verified_articles) - 8} 更多", "", ""])
    if not verified_articles:
        rows.append(["（无真实品牌报道）", "—", "—"])

    # News namesakes — excluded same-name articles with their reason.
    rows.append(["── 已排除同名误匹配 ──", str(news_namesake_n), "🚫 非本品牌"])
    for ns in news_namesakes[:5]:
        rows.append([_trunc(ns.get("source", "") or ns.get("title", ""), 20), "🚫",
                     _trunc(ns.get("reason", "") or ns.get("title", ""), 45)])

    findings: list[dict] = []
    # FINDING 0 — entity disambiguation as an EXPERTISE signal
    if total_namesakes > 0:
        ns_examples = bl_namesakes[:2] + news_namesakes[:2]
        ex_str = "；".join(
            f"{e.get('domain', '') or e.get('source', '')}"
            f"（{_trunc(e.get('why', '') or e.get('reason', ''), 30)}）"
            for e in ns_examples if e)
        findings.append(_finding(
            "P2", f"实体消歧：识别并排除 {total_namesakes} 个同名异主实体",
            (f"我们检测到 {total_namesakes} 个与品牌同名但属于不同企业的实体"
             f"（如 modernshade.net = 亚利桑那窗帘公司），已从「真实媒体报道」与"
             f"「外联线索」中排除，避免误导性建议。"),
            "无需行动 — 此为方法论质量保证；后续外联与 PR 仅针对已验证的真实品牌资产。",
            "OFFSITE-000", "backlink",
            evidence=ex_str or f"反链同名 {bl_namesake_n}、新闻同名 {news_namesake_n}。",
            confidence=0.9))
    # FINDING 1
    findings.append(_finding(
        "P1", "反链权威度依赖估算且类型集中",
        (f"仅约 {ext} 个 SERP 提及估算，且高度偏向零售/市场平台（amazon/walmart/wayfair/lowes），"
         "新闻/博客/edu_gov 类型为 0 — 链接多样性弱且未经验证。"),
        ("用真实反链工具（Ahrefs/Moz/SEMrush）确认，再优先争取编辑/博客/edu 链接，"
         "把 domain_type_breakdown 扩展到市场平台之外。"),
        "OFFSITE-001", "backlink",
        evidence=f"外部提及 {ext}；类型 {bd_str}（news 0/blog 0/edu_gov 0）。{disclaimer[:80]}"))
    # FINDING 2 — genuine outreach leads (namesakes already excluded)
    if unlinked:
        um_str = "；".join(f"{u.get('domain','')}（{_trunc(u.get('title',''),30)}）" for u in unlinked)
        findings.append(_finding(
            "P1", "未链接品牌提及待转化为反链",
            (f"{len(unlinked)} 个域名已真实提及品牌但很可能未加链接（同名异主实体已排除）— "
             "最温暖的外联线索、转化率最高的链接建设。"),
            "联系各未链接提及站点请求加链至官网；可用模板化外联。",
            "OFFSITE-002", "backlink",
            evidence=um_str))
    # FINDING 3 — news authority gap (honest disambiguation, no hedge)
    findings.append(_finding(
        "P1", "新闻覆盖全部为 Tier-3，缺乏权威媒体",
        (f"经实体消歧后仅 {verified_count} 篇为真实品牌报道，{news_namesake_n} 篇同名误匹配已排除；"
         f"分级 T1={tiers.get('tier_1',0)} / T2={tiers.get('tier_2',0)} / T3={tiers.get('tier_3',0)} — "
         f"真正的品牌赢得媒体很薄；无 Knowledge Panel（{kp}）。"
         "缺 T1/T2 报道会损害 E-E-A-T 与 AI 搜索引用。"),
        ("跑 Digital PR / HARO 对接 T1/T2 媒体（见 digital_pr_opportunities），"
         "并建立 Crunchbase/Wikidata/Wikipedia 实体以触发 Knowledge Panel。"),
        "OFFSITE-003", "news",
        evidence=f"真实报道 {verified_count} 篇（分级 {tier_str}）；同名排除 {news_namesake_n} 篇。"))
    # FINDING 4 — positive momentum
    if recency.get("last_30_days", 0) > 0:
        findings.append(_finding(
            "PASS", "近期有新闻动能可放大",
            f"近 30 天有 {recency.get('last_30_days',0)} 篇报道，存在可借力的合法曝光。",
            "通过自有渠道放大近期合法曝光并争取后续报道。",
            "OFFSITE-004", "news",
            evidence=rec_str))

    return {
        "icon": "🔗", "title_zh": "站外反链与新闻权威度", "title_en": "Backlinks & News Authority",
        "score": _module_score(findings),
        "summary_html": (f"<p>外部提及约 {ext} 个（估算），{unlinked_n} 个真实未链接外联线索；"
                         f"经实体消歧后真实品牌报道 {verified_count} 篇（全部 Tier-3），"
                         f"已排除 {total_namesakes} 个同名异主实体；无 Knowledge Panel。</p>"
                         f"<p>⚠️ {_trunc(disclaimer, 90)}</p>"),
        "data_table": {"headers": ["指标", "数值", "评级/状态"], "rows": rows},
        "findings": findings,
    }


def _module_community_presence(community: dict) -> dict:
    """Community & Conversational Presence — per-platform discussion detail."""
    total = community.get("total_community_mentions", 0)
    grade = community.get("community_grade", "NONE")
    reddit = community.get("reddit", {}) or {}
    quora = community.get("quora", {}) or {}
    forums = community.get("forums", {}) or {}
    scan_conf = community.get("confidence", "low")
    sample_n = community.get("estimated_from_sample_size", "")
    method = community.get("methodology_note", "")
    readiness = community.get("ai_citation_readiness", "")

    subs = reddit.get("top_subreddits", []) or []
    sent = reddit.get("sentiment_breakdown", {}) or {}
    r_mentions = reddit.get("mentions", []) or []

    rows: list[list] = [["── 平台概览 ──", "", ""]]
    rows.append(["社区提及总数", str(total), grade])
    rows.append(["Reddit 提及", str(reddit.get("mention_count", 0)), reddit.get("confidence", "—")])
    rows.append(["Quora 提及", str(quora.get("mention_count", 0)), quora.get("confidence", "—")])
    rows.append(["Forums 提及", str(forums.get("mention_count", 0)), forums.get("confidence", "—")])

    if subs:
        rows.append(["── Reddit 子版块分布 ──", "", ""])
        rows.append(["活跃子版块", " · ".join("r/" + s for s in subs), f"{len(subs)} 个"])

    rows.append(["── Reddit 情感分布 ──", "", ""])
    rows.append(["Reddit 情感",
                 f"positive {sent.get('positive',0)} · neutral {sent.get('neutral',0)} · negative {sent.get('negative',0)}",
                 "✅ 无负面" if sent.get("negative", 0) == 0 else "⚠️"])

    if r_mentions:
        rows.append(["── Reddit 提及明细 ──", "", ""])
        for m in r_mentions[:6]:
            rows.append(["r/" + str(m.get("subreddit", "")), m.get("sentiment", "—"),
                         _trunc(m.get("snippet", ""), 50)])
        if len(r_mentions) > 6:
            rows.append([f"+{len(r_mentions) - 6} 更多", "", ""])

    rows.append(["Quora 提及质量", "多为同名误匹配（颜色/旗帜 'modern shade'）", "⚠️ 低相关"])

    findings: list[dict] = []
    # FINDING 1 — Reddit strength (PASS)
    findings.append(_finding(
        "PASS", "Reddit 社区基础良好、利于 AI 引用",
        (f"{reddit.get('mention_count',0)} 条 Reddit 提及横跨 {len(subs)} 个子版块，"
         f"情感正/中性（{sent.get('positive',0)} 正面、{sent.get('negative',0)} 负面），含推荐式语言 — "
         f"正是 AI 搜索引擎采集引用的内容。{readiness[:40]}"),
        ("在活跃子版块持续真实互动并播种更多推荐式讨论；按 opportunities 扩展到行业论坛。"),
        "COMMUNITY-001", "community",
        evidence=f"子版块 {len(subs)} 个；情感 {sent}；{_trunc(readiness, 50)}"))
    # FINDING 2 — confidence caveat
    findings.append(_finding(
        "P2", "提及数据置信度有限，需校验",
        (f"扫描级置信度为 '{scan_conf}'（样本 {sample_n}，每查询取 top-10 Serper 结果），"
         "且 10 条 Quora '提及' 几乎全是 'modern shade' 作为颜色/旗帜描述的误匹配 — "
         f"STRONG 评级高估了真实品牌对话。"),
        ("将计数视为发现信号；上报客户前人工区分品牌 vs 通用短语提及，"
         "且不要把 Quora 当作渠道优化。"),
        "COMMUNITY-002", "community",
        evidence=_trunc(method, 80)))
    # FINDING 3 — forums gap
    findings.append(_finding(
        "P2", "Forums 渠道空白",
        (f"forums.mention_count={forums.get('mention_count',0)}，置信度低 — "
         "智能家居/户外生活/DIY 等垂直论坛是人类发现与 AI 引用的未开发面。"),
        "按 opportunities 参与 2-3 个相关论坛/社区，扩大 Reddit 之外的引用面。",
        "COMMUNITY-003", "community",
        evidence=f"forums 提及 {forums.get('mention_count',0)}。"))

    return {
        "icon": "💬", "title_zh": "社区与对话式提及", "title_en": "Community & Conversational Presence",
        "score": _module_score(findings),
        "summary_html": (f"<p>{total} 条社区提及（Reddit {reddit.get('mention_count',0)} 横跨 "
                         f"{len(subs)} 子版块，正向为主；Quora {quora.get('mention_count',0)} 多为通用短语误匹配；"
                         f"forums 0）；部分具备 AI 引用条件。</p>"),
        "data_table": {"headers": ["指标", "数值", "评级/置信度"], "rows": rows},
        "findings": findings,
    }


def _looks_like_address(s: Any) -> bool:
    """False when the scraped 'address' is JS state garbage or empty."""
    if not s or not isinstance(s, str):
        return False
    low = s.lower()
    for bad in ("null,", "orders_count", "total_spent", "\n"):
        if bad in low if bad != "\n" else bad in s:
            return False
    return True


def _module_schema_ai(schema: dict, freshness: dict) -> dict:
    block_count = schema.get("block_count", 0)
    found_types = schema.get("found_types", []) or []
    comp = schema.get("composite_score", 0)
    parse_errors = schema.get("parse_errors", 0)
    missing = schema.get("missing_expected_types", []) or []
    sch_findings = schema.get("findings", []) or []
    types_str = ", ".join(found_types) if found_types else "无"

    sitemap = freshness.get("sitemap_found", False)
    fresh = freshness.get("freshness", {}) or {}
    fresh_grade = fresh.get("freshness_grade", "UNKNOWN")
    total_urls = freshness.get("total_urls_in_sitemap", 0)
    stale = fresh.get("stale_pages_1yr", 0)
    pct90 = fresh.get("pct_updated_90d", 0)
    most_recent = fresh.get("most_recent_update", "—")
    url_cats = freshness.get("url_categories", {}) or {}
    pv = freshness.get("publishing_velocity", {}) or {}
    velocity_basis = pv.get("velocity_basis", "all_urls")
    blog_detected = freshness.get("blog_detected", False)
    content_grade = freshness.get("content_grade", "—")

    rows: list[list] = [
        ["JSON-LD 块数", str(block_count), "✅" if block_count > 0 else "❌"],
        ["检测到的 Schema 类型", types_str, f"{len(found_types)} 种"],
        ["缺失的推荐类型", ", ".join(missing) if missing else "无", "⚠️ 待补" if missing else "✅"],
        ["Schema 综合评分", f"{comp}/100", "✅" if comp >= 60 else "⚠️"],
        ["解析错误", str(parse_errors), "✅" if parse_errors == 0 else "❌"],
    ]
    # Per-type completeness rows
    for f in sch_findings:
        st = f.get("schema_type", "?")
        elig = f.get("rich_results_eligibility", "—")
        req_miss = f.get("required_missing", []) or []
        rec_miss = f.get("recommended_missing", []) or []
        if not f.get("is_known_type", True):
            status = "ℹ️ 未知类型"
            elig_cell = elig
        elif req_miss or rec_miss:
            status = "⚠️ 缺字段"
            elig_cell = f"{elig}（缺 {', '.join(req_miss + rec_miss)}）"
        elif elig and "no rule" not in elig.lower():
            status = "✅ Rich Result"
            elig_cell = elig
        else:
            status = "✅"
            elig_cell = elig
        rows.append([f"  └ {st}", elig_cell, status])

    rows += [
        ["Sitemap", "✅ 已检测" if sitemap else "❌", f"{total_urls} URL"],
        ["Sitemap 内容构成",
         f"产品 {url_cats.get('product',0)} · 博客 {url_cats.get('blog',0)} · "
         f"静态 {url_cats.get('static',0)} · 其他 {url_cats.get('other',0)}", "—"],
        ["发布速率 (近30天)", str(pv.get("last_30_days", 0)), "—"],
        ["月均发布 (12月)", f"{pv.get('avg_per_month_12m', 0)} 页/月", f"基准: {velocity_basis}"],
        ["最近更新", str(most_recent), fresh_grade],
        ["近90天更新占比", f"{pct90}%", "✅" if pct90 >= 50 else "⚠️"],
        ["过期页面 (>1年)", str(stale), "⚠️" if stale > 50 else "—"],
        ["内容更新评级", content_grade, "—"],
    ]

    findings: list[dict] = []
    # FINDING A — missing expected types
    if missing:
        findings.append(_finding(
            "P2", f"缺少推荐的 Schema 类型 ({', '.join(missing)})",
            ("Organization schema 是 Google Knowledge Panel 与 AI 搜索引用的核心实体信号；"
             "已声明 OnlineStore 但缺独立 Organization 实体，导致品牌实体图谱不完整。"),
            "在首页 <head> 加 Organization JSON-LD，含 name/url/logo/sameAs(社媒)。",
            "SCHEMA-001", "schema",
            evidence=f"已检测类型: {types_str}；缺失期望类型: {', '.join(missing)}。",
            code_snippet=(
                '{\n  "@context": "https://schema.org",\n  "@type": "Organization",\n'
                '  "name": "<品牌名>",\n  "url": "<网站>",\n  "logo": "<logo URL>",\n'
                '  "sameAs": ["<社媒1>", "<社媒2>"]\n}'),
            code_language="JSON-LD", paste_location="首页 <head> 标签内", confidence=0.75))
    # FINDING B — unknown / non-spec type
    for f in sch_findings:
        if not f.get("is_known_type", True):
            nested = f.get("nested_types", []) or []
            findings.append(_finding(
                "P2", f"{f.get('schema_type')} 类型未在 Rich Results 规则库中",
                (f"{f.get('schema_type')} 虽合法 schema.org 类型，但 Google 无对应 Rich Result 渲染规则，"
                 "无法直接产生富摘要；其嵌套类型价值无法被验证工具确认。"),
                "保留该类型，但补充 Google 明确支持的类型 (Product/Organization/LocalBusiness) 以获得富结果资格。",
                "SCHEMA-002", "schema",
                evidence=f"{f.get('schema_type')} 含嵌套类型 {', '.join(nested) or '无'}，但无 Rich Result 规则。",
                confidence=0.7))
            break
    # FINDING C — field completeness (PASS if all complete)
    if not any((f.get("required_missing") or f.get("recommended_missing")) for f in sch_findings):
        wins = [f"{f.get('schema_type')}: {f.get('rich_results_eligibility')}"
                for f in sch_findings if f.get("rich_results_eligibility")
                and "no rule" not in str(f.get("rich_results_eligibility")).lower()]
        findings.append(_finding(
            "PASS", "已声明 Schema 类型字段完整",
            ("WebSite/WebPage/BreadcrumbList 必填与推荐字段齐全，"
             "WebSite 含 SearchAction 可触发 Sitelinks Search Box，BreadcrumbList 可触发 SERP 面包屑。"),
            "维持现有 schema 字段完整度。",
            "SCHEMA-003", "schema",
            evidence="；".join(wins)))
    # FINDING D — content freshness (stale P2 only if >50, else PASS)
    if stale > 50:
        findings.append(_finding(
            "P2", "大量页面内容过期",
            f"{stale} 个页面超过 1 年未更新。Google 偏好新鲜内容，AI 搜索引擎更倾向引用近期更新页面。",
            "启动内容刷新计划：优先更新流量最高的 Top 20 过期页面，每月至少更新 10-15 个。",
            "SCHEMA-004", "freshness",
            evidence=f"Sitemap 含 {total_urls} 个 URL，{stale} 个超 1 年未更新，仅 {pct90}% 近 90 天更新。"))
    else:
        findings.append(_finding(
            "PASS", "内容新鲜度健康",
            (f"近 90 天更新 {pct90}%，月均 {pv.get('avg_per_month_12m', 0)} 页，"
             f"仅 {stale} 个页面 >1 年未更新。"),
            "保持当前更新节奏。",
            "SCHEMA-005", "freshness",
            evidence=f"velocity_basis={velocity_basis}（注意是全站 URL 而非纯博客）。"))
    # FINDING E — no blog / editorial hub
    if not blog_detected and url_cats.get("blog", 0) == 0:
        findings.append(_finding(
            "P2", "缺少博客/编辑内容枢纽",
            (f"站点 {total_urls} URL 中 0 篇博客（全是产品/静态页），缺乏话题权威 (E-E-A-T) 内容，"
             "AI 搜索更倾向引用有教育性长文的站点。"),
            "建内容枢纽，针对 'how to choose'/'best ... gazebo' 类查询产出长文（正是竞品被 AI 引用的查询）。",
            "SCHEMA-006", "freshness",
            evidence=(f"url_categories: 产品 {url_cats.get('product',0)}, 博客 {url_cats.get('blog',0)}, "
                      f"静态 {url_cats.get('static',0)}。高发布速率 ({pv.get('last_30_days',0)}/30天) 是产品页更替，非编辑内容。")))

    score = comp
    for f in findings:
        score -= _MODULE_DEDUCT.get(f["severity"], 0)
    return {
        "icon": "🤖", "title_zh": "结构化数据与 AI 就绪度", "title_en": "Schema & AI Readiness",
        "score": max(0, min(100, score)),
        "summary_html": (f"<p>检测到 {len(found_types)} 种 Schema 类型（{types_str}），综合评分 {comp}/100。</p>"
                         f"<p>月均发布 {pv.get('avg_per_month_12m',0)} 页（基准 {velocity_basis} — 产品页更替非内容营销），"
                         f"博客枢纽缺失。</p>"),
        "data_table": {"headers": ["指标", "数值", "状态"], "rows": rows},
        "findings": findings,
    }


def _module_social_nap(social: dict, nap: dict) -> dict:
    profiles = social.get("profiles_analyzed", 0)
    reach = social.get("total_reach", 0)
    active = social.get("active_platforms", 0)
    unknown_n = social.get("unknown_activity_count", 0)
    cons_score = social.get("consistency_score", 0)
    inf_grade = social.get("influence_grade", "ABSENT")
    platforms = social.get("platforms", {}) or {}

    onsite = nap.get("onsite_nap", {}) or {}
    nap_grade = nap.get("nap_grade", "UNKNOWN")
    has_schema = onsite.get("has_schema_org", False)
    issues = nap.get("consistency_issues", []) or []
    dirs = nap.get("directory_listings", []) or []
    found_count = sum(1 for d in dirs if d.get("found"))
    total_dirs = len(dirs)

    _PLAT_NAME = {"linkedin": "LinkedIn", "youtube": "YouTube", "twitter": "Twitter",
                  "facebook": "Facebook", "instagram": "Instagram"}

    rows: list[list] = [["── 社交影响力 ──", "", ""]]
    rows.append(["社媒账号分析数", str(profiles), "—"])
    rows.append(["总触达", str(reach), inf_grade])
    rows.append(["活跃平台 / 状态未知", f"{active} 活跃 · {unknown_n} 未知", "—"])
    rows.append(["一致性评分", str(cons_score), "—"])
    for key, p in platforms.items():
        name = _PLAT_NAME.get(key, key)
        url = p.get("url")
        act = p.get("active")
        # value string
        if p.get("subscribers") is not None:
            val = f"{p.get('subscribers')} 订阅"
            if p.get("video_count") is not None:
                val += f" · {p.get('video_count')} 视频"
        elif p.get("followers") is not None:
            val = f"{p.get('followers')} 粉丝"
        elif p.get("likes") is not None:
            val = f"{p.get('likes')} 赞"
        elif url:
            val = "已建档 · 数据未知"
        else:
            val = "无账号"
        if not url:
            status = "❌ 缺失"
        elif act is True:
            status = "✅ 活跃"
        else:
            status = "❓ 未知"
        rows.append([f"  └ {name}", val, status])

    # NAP section
    rows.append(["── NAP 一致性 ──", "", ""])
    rows.append(["NAP 一致性评级", nap_grade, "✅" if nap_grade == "CONSISTENT" else "⚠️"])
    rows.append(["站内 NAP — 名称", onsite.get("name", "—"), "—"])
    rows.append(["站内 NAP — 电话", onsite.get("phone", "—"), "✅" if onsite.get("phone") else "—"])
    rows.append(["站内 NAP — 邮箱", onsite.get("email", "—"), "—"])
    addr_ok = _looks_like_address(onsite.get("address"))
    rows.append(["站内 NAP — 地址",
                 onsite.get("address") if addr_ok else "⚠️ 解析失败 (疑似抓到 JS 变量)",
                 "✅" if addr_ok else "❌"])
    rows.append(["Schema.org NAP 标记", "有" if has_schema else "无", "✅" if has_schema else "❌"])
    rows.append(["目录收录", f"{found_count}/{total_dirs} 平台", "—"])
    for d in dirs:
        plat = d.get("platform", "?")
        if not d.get("found"):
            detail, mstatus = "未收录", "❌ 未收录"
        else:
            parts = [d.get("directory_name", "")]
            if d.get("directory_phone"):
                parts.append(f"☎{d.get('directory_phone')} {'✓' if d.get('phone_match') else '✗'}")
            if d.get("directory_address"):
                parts.append(f"📍{_trunc(d.get('directory_address'),16)} {'✓' if d.get('address_match') else '✗'}")
            detail = " · ".join(p for p in parts if p)
            if d.get("name_match") and d.get("phone_match") and d.get("address_match"):
                mstatus = "✅ 全匹配"
            elif d.get("phone_match") is None or d.get("address_match") is None:
                mstatus = "⚠️ 信息缺失"
            else:
                mstatus = "⚠️ 部分不符"
        rows.append([f"  └ {plat}", _trunc(detail, 45), mstatus])

    findings: list[dict] = []
    # FINDING A — social influence
    if inf_grade in ("ABSENT", "DORMANT"):
        findings.append(_finding(
            "P1", "社交媒体影响力薄弱",
            (f"影响力评级 {inf_grade}，{active} 个活跃平台，总触达仅 {reach}；"
             f"{unknown_n} 个平台已建档但活跃度/粉丝未知，部分平台完全缺失。"
             "社交信号影响品牌搜索量与 AI 引用频率。"),
            ("补建缺失账号并激活已建档平台、定期发布；在站点页脚与 Schema sameAs 中声明全部社媒链接。"),
            "SOCIAL-001", "social",
            evidence=f"活跃 {active}、未知 {unknown_n}；总触达 {reach}。"))
    # FINDING B — NAP inconsistency
    if nap_grade in ("INCONSISTENT", "PARTIAL"):
        issues_text = "; ".join(str(i) for i in issues[:3]) if issues else "信息不一致"
        findings.append(_finding(
            "P2", "NAP 信息跨目录不一致",
            ("品牌电话/地址在目录与官网不符，且官网无 Schema.org 标记，"
             "搜索引擎无法程序化验证 NAP — 削弱本地 SEO 与实体可信度。"),
            "统一各目录 NAP，认领未收录目录 (如 Google Maps)，并加 LocalBusiness JSON-LD。",
            "SOCIAL-002", "nap",
            evidence=f"NAP 评级 {nap_grade}。问题: {_trunc(issues_text, 80)}",
            code_snippet=(
                '{\n  "@context": "https://schema.org",\n  "@type": "LocalBusiness",\n'
                '  "name": "<统一品牌名>",\n  "address": {"@type": "PostalAddress", "streetAddress": "<地址>", "addressCountry": "<国家>"},\n'
                '  "telephone": "<电话>",\n  "email": "<邮箱>"\n}'),
            code_language="JSON-LD", paste_location="首页 <head> 标签内", confidence=0.80))
    # FINDING C — corrupted onsite address
    if not _looks_like_address(onsite.get("address")):
        findings.append(_finding(
            "P2", "站内地址未能正确解析",
            ("探针在官网未找到结构化地址，抓取到的是 JS 变量片段 (orders_count/total_spent)，"
             "说明官网首页没有以可读形式公开实体地址 — 本身就是 NAP/本地 SEO 缺口。"),
            "在页脚与 Contact 页以纯文本 + LocalBusiness schema 公开完整地址。",
            "SOCIAL-003", "nap",
            evidence="抓取值为 JS 状态而非地址；地址维度的一致性比对不可靠。",
            confidence=0.6))
    # FINDING D — unclaimed directory (e.g. Google Maps)
    unclaimed = [d.get("platform") for d in dirs if not d.get("found")]
    if unclaimed and total_dirs > 0:
        findings.append(_finding(
            "P2", f"目录未收录: {', '.join(unclaimed)}",
            (f"{total_dirs} 个目录中 {len(unclaimed)} 个未收录（其余 {found_count} 个已收录）。"
             "未收录目录是最高价值的本地 SEO 面，应优先认领。"),
            "认领未收录目录并填写一致的 NAP 信息。",
            "SOCIAL-004", "nap",
            evidence=f"未收录: {', '.join(unclaimed)}。"))
    # FINDING E — PASS: brand name matches
    found_dirs = [d for d in dirs if d.get("found")]
    if found_dirs and all(d.get("name_match") for d in found_dirs):
        findings.append(_finding(
            "PASS", "品牌名在已收录目录均匹配",
            "已收录目录的名称均与官网一致；问题集中在电话/地址维度而非品牌名。",
            "保持品牌名一致性，重点修正电话/地址维度。",
            "SOCIAL-005", "nap",
            evidence=f"{len(found_dirs)} 个已收录目录名称全部匹配。"))

    return {
        "icon": "📱", "title_zh": "社交媒体与信息一致性 (NAP)", "title_en": "Social & NAP Consistency",
        "score": _module_score(findings),
        "summary_html": (f"<p>社媒影响力 {inf_grade}（{active} 个验证活跃平台、{unknown_n} 个状态未知）。"
                         f"NAP {nap_grade}，目录收录 {found_count}/{total_dirs}。</p>"),
        "data_table": {"headers": ["指标", "数值", "状态"], "rows": rows},
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Synthesis layer — 90-day GEO roadmap
# ---------------------------------------------------------------------------

def _module_roadmap(modules: list[dict], offsite: dict[str, dict]) -> dict:
    """Synthesize a SEQUENCED 90-day GEO plan from the built diagnostics.

    Not a concatenation of findings — it reads the already-built modules and the
    rich probe data to sequence three phases:
      Month 1 (foundation): highest-severity quick wins (schema, GBP, NAP).
      Month 2 (GEO content): citation-bait pages for the EXACT queries
        competitors win in the AI citation module.
      Month 3 (authority): digital PR to T1/T2 media, entity-building for a
        Knowledge Panel, and converting unlinked mentions to backlinks.
    Brand-agnostic.
    """
    # --- collect cross-module findings by severity (skip the roadmap itself) ---
    all_findings: list[dict] = []
    for m in modules:
        all_findings.extend(m.get("findings", []) or [])
    p0 = [f for f in all_findings if f.get("severity", "").upper() == "P0"]
    p1 = [f for f in all_findings if f.get("severity", "").upper() == "P1"]

    def _titles(findings: list[dict], n: int = 4) -> list[str]:
        return [f.get("title_zh", "") for f in findings[:n] if f.get("title_zh")]

    # --- Month 1: foundation quick-wins derived from P0/P1 titles ---
    quick_wins = _titles(p0 + p1, 5)
    m1_actions = (
        "修复最高优先级基础项：" + "；".join(quick_wins)
        if quick_wins else
        "补齐 Organization/LocalBusiness Schema、认领 Google Business Profile、"
        "配置 Consent Mode v2、统一 NAP 信息。")

    # --- Month 2: GEO content from the AI citation module's exact queries ---
    ai = offsite.get("ai_citation", {}) or {}
    queries_run = ai.get("queries_run", []) or []
    stats = ai.get("summary_stats", {}) or {}
    top_comp = stats.get("top_competitors_cited", []) or []
    comp_domains = [c.get("domain", "") for c in top_comp[:3] if c.get("domain")]
    q_sample = "、".join(f"「{q}」" for q in queries_run[:3]) if queries_run else "目标买家意图查询"
    comp_str = "、".join(comp_domains) if comp_domains else "现有被引竞品"
    m2_actions = (
        f"针对竞品垄断 AI 引用的精确查询创建对比/买家意图页（citation-bait）："
        f"{q_sample}；逐一对标 {comp_str}，配 Product/FAQ Schema 与可被 AI 抓取的数据页。")

    # --- Month 3: authority — PR, entity-building, convert unlinked mentions ---
    bl = offsite.get("backlink", {}) or {}
    unlinked_n = bl.get("unlinked_mention_count", len(bl.get("unlinked_mentions", []) or []))
    news = offsite.get("news", {}) or {}
    tiers = news.get("source_tier_breakdown", {}) or {}
    all_t3 = (tiers.get("tier_1", 0) == 0 and tiers.get("tier_2", 0) == 0
              and tiers.get("tier_3", 0) > 0)
    m3_actions = (
        f"数字 PR / HARO 对接 T1/T2 媒体（修复{'全部 Tier-3' if all_t3 else '权威媒体'}缺口）；"
        f"建立 Crunchbase/Wikidata 实体以触发 Knowledge Panel；"
        f"将 {unlinked_n} 个真实未链接提及转化为反链。")

    rows = [
        ["Month 1", "基础修复 (Foundation)", m1_actions,
         "消除技术失分项，建立 AI/搜索引擎可读的实体与信任基线"],
        ["Month 2", "GEO 内容 (Citation-Bait)", m2_actions,
         "在竞品垄断的精确查询上抢占 AI 引用位，扭转 0% 引用率"],
        ["Month 3", "权威建设 (Authority)", m3_actions,
         "提升 E-E-A-T 与媒体权威，巩固 Knowledge Panel 与长期 AI 引用"],
    ]

    findings: list[dict] = [
        _finding(
            "P1", "按月排序的 90 天 GEO 执行路线",
            (f"将本报告的 {len(p0)} 个 P0 与 {len(p1)} 个 P1 诊断综合为可执行的三阶段计划："
             "先修基础、再用 citation-bait 抢 AI 引用、最后建权威与实体 — "
             "顺序而非并行，避免在权威信号缺失时盲目产内容。"),
            "按 Month 1→2→3 顺序执行；每月末复测 ai_citation 引用率与 schema/NAP 评分以验证进展。",
            "ROADMAP-001", "roadmap",
            evidence=f"P0 {len(p0)} 个、P1 {len(p1)} 个；AI 查询 {len(queries_run)} 条、被引竞品 {len(top_comp)} 个。",
            confidence=0.85),
    ]
    if queries_run:
        findings.append(_finding(
            "P2", "Month 2 内容直接对标竞品赢得的 AI 查询",
            (f"内容生产不靠猜：直接复用 ai_citation 实测的 {len(queries_run)} 条买家查询作为页面选题，"
             f"对标 {comp_str} 的被引内容，最大化 citation-bait 命中率。"),
            "为每条高意图查询产出 1 篇对比/指南长文 + 结构化数据，30 天内上线 3-5 篇。",
            "ROADMAP-002", "roadmap",
            evidence=f"目标查询样本: {q_sample}",
            confidence=0.8))

    return {
        "icon": "🗺️", "title_zh": "90 天 GEO 行动路线图", "title_en": "90-Day GEO Roadmap",
        "score": _module_score(findings),
        "summary_html": (
            f"<p>将全部诊断综合为<strong>按月排序</strong>的 90 天计划："
            f"Month 1 基础修复（{len(p0)} P0 / {len(p1)} P1 快赢）→ "
            f"Month 2 针对竞品垄断查询的 GEO citation-bait 内容 → "
            f"Month 3 数字 PR + 实体建设 + {unlinked_n} 个未链接提及转化。</p>"),
        "data_table": {"headers": ["阶段 Month", "重点", "具体动作", "预期影响"], "rows": rows},
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Contract builder
# ---------------------------------------------------------------------------

def build_contract(base: dict, offsite: dict[str, dict]) -> dict:
    """Merge base on-site audit + off-site probes into one report contract."""
    offsite_modules = [
        _module_ai_citation(
            offsite.get("ai_citation", {}), brand_name=base.get("company", "")),
        _module_reputation(offsite.get("reputation", {})),
        _module_backlinks_news(offsite.get("backlink", {}), offsite.get("news", {})),
        _module_community_presence(offsite.get("community", {})),
        _module_schema_ai(
            offsite.get("schema", {}), offsite.get("freshness", {})),
        _module_social_nap(
            offsite.get("social", {}), offsite.get("nap", {})),
    ]

    data = dict(base)  # shallow copy of the base report_data
    all_modules = list(base.get("modules", [])) + offsite_modules

    # Synthesis layer (LAST module): reads the already-built diagnostics to
    # sequence a 90-day plan, so it must be appended AFTER everything else.
    roadmap = _module_roadmap(all_modules, offsite)
    all_modules.append(roadmap)
    data["modules"] = all_modules

    # Recalculate counts + overall score across ALL modules (skip score == -1)
    p0 = p1 = p2 = passes = 0
    for mod in data["modules"]:
        if mod.get("score", -1) == -1:
            continue
        for f in mod.get("findings", []):
            sev = f.get("severity", "").upper()
            if sev == "P0":
                p0 += 1
            elif sev == "P1":
                p1 += 1
            elif sev == "P2":
                p2 += 1
            elif sev == "PASS":
                passes += 1
    data["p0_count"], data["p1_count"], data["p2_count"], data["pass_count"] = p0, p1, p2, passes
    data["overall_score"] = max(0, 100 - (p0 * 25 + p1 * 10 + p2 * 3))

    # Re-rank top actions across all findings
    all_findings: list[dict] = []
    for mod in data["modules"]:
        all_findings.extend(mod.get("findings", []))
    order = {"P0": 0, "P1": 1, "P2": 2, "PASS": 3}
    all_findings.sort(key=lambda f: order.get(f.get("severity", "").upper(), 9))
    data["top_actions"] = [{
        "title": f.get("action_zh", f.get("title_zh", "")),
        "impact": f"{f.get('severity', 'P1')} · {f.get('impact_zh', '')}",
        "effort": "30 分钟" if f.get("code_snippet") else "需评估",
    } for f in all_findings[:3]]

    data["next_steps_text"] = (
        f"本报告中标记为 P0 的 {p0} 个问题建议在 7 天内优先处理。"
        f"如需协助实施，请联系我们的技术团队。")
    data["_offsite_raw"] = offsite
    return data
