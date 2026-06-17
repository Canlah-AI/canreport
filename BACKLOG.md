# canreport — backlog

## Principle: no cross-contamination ("串"), but match-triggered enrichment is OK
The audit tool must generalize to ANY brand/industry/region. Hardcoded LOOSE-match
tables (e.g. "outdoor" substring → gazebo keywords) cross-contaminate the wrong
client and are removed. Industry- or region-SPECIFIC logic is allowed ONLY as an
enrichment layer that fires on a **confident** match and falls back to the generic
LLM-dynamic path otherwise — never a loose partial-keyword bleed.

## Match-triggered enrichment layer (to build)
A registry of optional enrichments keyed by a CONFIDENT signal, each adding
precision when matched and doing nothing when not:
- **Industry-specific schema expectations** — e.g. a verified ecommerce brand →
  expect Product/Offer/AggregateRating; a LocalBusiness → expect address/hours.
  (Replaces the removed `INDUSTRY_EXPECTED_TYPES` 6-industry table in
  `site-audit/probes/schema_validator.py`; currently still hardcoded —
  thread the detected business type + LLM-derive expected types, generic
  [Organization, WebSite] default.)
- **Region/language-specific media priors** — e.g. a verified `.de` brand →
  weight German national outlets; a CN brand → CN media. The news tier classifier
  is already LLM-dynamic (no hardcoded list); priors would only add confidence.
- **Industry-specific buyer-query phrasing** — confident category → richer intent
  templates. The base queries already come from the LLM business-understanding
  categories.

Match must be CONFIDENT (e.g. the business-understanding pass's
`primary_category` / detected `business_type`, or a domain TLD), never a loose
substring over raw hints.

## Remaining de-hardcoding (each codex-gated)
- **i18n** — report output is Simplified-Chinese-only (≈244 strings in
  `contract.py` + templates). Non-Chinese clients see Chinese labels. Needs a
  language bundle + locale detection. Large; a product decision (which output
  languages) gates it.
- **schema_validator `INDUSTRY_EXPECTED_TYPES`** — see enrichment layer above.
- **Off-category namesake patterns** (`news_coverage_scan.OFF_CATEGORY_PATTERNS`,
  5 hardcoded industries) → LLM "is this article off-category for the brand's
  industry?".
- **Geography defaults** — `gl=us` for the BD AI-Overview call is a documented
  Google constraint (AIO is US-gated), not hardcoding; confirm no other stray
  `sg`/`us` default beyond the market arg.

## Done (de-hardcoded, LLM-dynamic / detected-context, codex-gated)
- News outlet tier classification (was 3 hardcoded Western/CN domain sets) →
  batched LLM, neutral tier_3 fallback. spiegel.de/nikkei.com now tier-1.
- News sentiment + topics (was English-only regex) → batched LLM, any language.
- `_PRODUCT_NOUN_MAP` + `_INDUSTRY_KEYWORD_MAP` (6-industry loose tables) →
  detected english_product_hint + canonical categories.
- Singapore `_STALE_GARBAGE_DOMAINS` blacklist → removed (vestigial).
- News sentiment + topics + off-category namesake verdicts → batched LLM.
- `schema_validator.INDUSTRY_EXPECTED_TYPES` (6-industry table) → LLM-derived
  expected types, neutral [Organization, WebSite] default.
- **CDN / tracking / social / SERP detection lists** (`eac_speed_probe`,
  `eac_tracking_probe`, `social_influence_scan`, `serper_discovery`) → external
  `probes/detection_signatures.json` + validated `signatures_config.py` loader
  (per-section fallback to bundled `_DEFAULTS`, never raises). Adds modern /
  non-Western vendors (Vercel/Netlify/Alibaba/Tencent CDN; TikTok/Pinterest/
  LinkedIn/Reddit/Snap pixels, Bing UET, Baidu Tongji, Plausible/Umami/Matomo;
  weibo/xiaohongshu/douyin/bilibili + CN/SEA SERP aggregators). These are
  deterministic signatures (false-negatives, never "串"); new vendors can be
  added by editing JSON, no code change.
