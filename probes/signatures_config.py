"""Detection-signature registry loader.

Deterministic vendor signatures (CDN headers, tracking-tag URLs, social-platform
domains, SERP exclusions) live in an external JSON registry so non-Western /
modern vendors can be added WITHOUT code edits. If the JSON file is missing or
malformed, the BUNDLED defaults below are used, so behavior is identical to the
pre-config code path. No network, no LLM — pure config.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).with_name("detection_signatures.json")

# Bundled defaults == the JSON above. Kept in-code so the loader is robust even
# if the JSON file is deleted. (Mirror of detection_signatures.json.)
_DEFAULTS: dict[str, Any] = {
    "cdn_header_rules": [
        ["cf-ray", r".+", "cloudflare"],
        ["server", r"(?i)cloudflare", "cloudflare"],
        ["x-amz-cf-id", r".+", "cloudfront"],
        ["x-amz-cf-pop", r".+", "cloudfront"],
        ["via", r"(?i)cloudfront", "cloudfront"],
        ["x-akamai-request-id", r".+", "akamai"],
        ["x-akamai-transformed", r".+", "akamai"],
        ["server", r"(?i)akamai", "akamai"],
        ["x-cdn", r"(?i)incapsula|imperva", "imperva"],
        ["server", r"(?i)keycdn", "keycdn"],
        ["x-served-by", r"(?i)cache-", "fastly"],
        ["server", r"(?i)\bvercel\b", "vercel"],
        ["x-vercel-id", r".+", "vercel"],
        ["x-vercel-cache", r".+", "vercel"],
        ["server", r"(?i)netlify", "netlify"],
        ["x-nf-request-id", r".+", "netlify"],
        ["server", r"(?i)bunnycdn|bunny", "bunny"],
        ["server", r"(?i)tengine", "alibaba"],
        ["eagleid", r".+", "alibaba"],
        ["via", r"(?i)\.cdngslb\.com|alicdn|aliyun", "alibaba"],
        ["x-swift-savetime", r".+", "alibaba"],
        ["server", r"(?i)\bgws\b|tencent|qcloud", "tencent"],
        ["x-nws-log-uuid", r".+", "tencent"],
        ["x-daa-tunnel", r".+", "tencent"],
        ["server", r"(?i)gcore|g-core", "gcore"],
        ["server", r"(?i)cdn77", "cdn77"],
        ["x-cdn77-request-id", r".+", "cdn77"],
        ["x-sucuri-id", r".+", "sucuri"],
        ["x-sucuri-cache", r".+", "sucuri"],
        ["x-cache", r"(?i)hit|miss", "generic_cdn"],
    ],
    "tag_detectors": [
        ["ga4", "Google Analytics 4", r"googletagmanager\.com/gtag/js\?id=G-[A-Z0-9]{8,12}\b"],
        ["gtm", "Google Tag Manager", r"googletagmanager\.com/gtm\.js\?id=GTM-[A-Z0-9]{5,9}\b"],
        ["google_ads", "Google Ads Tag", r"googletagmanager\.com/gtag/js\?id=AW-[0-9]{8,12}\b"],
        ["meta_pixel", "Meta Pixel", r"connect\.facebook\.net/[a-zA-Z_]+/fbevents\.js"],
        ["tiktok_pixel", "TikTok Pixel", r"analytics\.tiktok\.com/i18n/pixel|ttq\.load\s*\("],
        ["pinterest_tag", "Pinterest Tag", r"s\.pinimg\.com/ct/core\.js|pintrk\s*\("],
        ["linkedin_insight", "LinkedIn Insight Tag", r"snap\.licdn\.com/li\.lms-analytics/insight\.min\.js|_linkedin_partner_id"],
        ["twitter_pixel", "Twitter/X Pixel", r"static\.ads-twitter\.com/uwt\.js|twq\s*\("],
        ["snap_pixel", "Snap Pixel", r"sc-static\.net/scevent\.min\.js|snaptr\s*\("],
        ["reddit_pixel", "Reddit Pixel", r"redditstatic\.com/ads/pixel\.js|rdt\s*\("],
        ["bing_uet", "Microsoft/Bing UET", r"bat\.bing\.com/bat\.js|window\.uetq"],
        ["baidu_tongji", "Baidu Tongji", r"hm\.baidu\.com/hm\.js"],
        ["plausible", "Plausible Analytics", r"plausible\.io/js/script(?:\.[a-z.]+)?\.js"],
        ["umami", "Umami Analytics", r"data-website-id[^>]*umami|/umami(?:\.[a-z]+)?\.js"],
        ["matomo", "Matomo/Piwik", r"matomo\.js|piwik\.js|_paq\.push"],
        ["clarity", "Microsoft Clarity", r"clarity\.ms/tag/|clarity\(\s*[\"']set"],
        ["hotjar", "Hotjar", r"static\.hotjar\.com/c/hotjar-|hj\s*\(\s*[\"']"],
    ],
    "feature_detectors": [
        ["consent_mode_v2", "Consent Mode v2", r"""gtag\s*\(\s*['"]consent['"]\s*,\s*['"]default['"]"""],
        ["enhanced_conversions", "Enhanced Conversions", r"""gtag\s*\(\s*['"]set['"]\s*,\s*['"]user_data['"]"""],
        ["conversion_event", "Conversion Event", r"""gtag\s*\(\s*['"]event['"]\s*,\s*['"]conversion['"]"""],
        ["purchase_event", "Purchase Event", r"""gtag\s*\(\s*['"]event['"]\s*,\s*['"]purchase['"]"""],
        ["recaptcha_v2", "reCAPTCHA v2", r"google\.com/recaptcha/api\.js|g-recaptcha"],
        ["recaptcha_v3", "reCAPTCHA v3", r"google\.com/recaptcha/api\.js\?render="],
        ["recaptcha_enterprise", "reCAPTCHA Enterprise", r"google\.com/recaptcha/enterprise\.js"],
        ["turnstile", "Cloudflare Turnstile", r"challenges\.cloudflare\.com/turnstile/"],
    ],
    "social_platforms": {
        "linkedin":   {"url_regex": r'https?://(?:www\.)?linkedin\.com/company/[A-Za-z0-9._-]+', "site_query": "site:linkedin.com/company", "generic_handles": ["share", "shareArticle", "sharing", "feed", "jobs"]},
        "youtube":    {"url_regex": r'https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)[A-Za-z0-9._-]+', "site_query": "site:youtube.com", "generic_handles": ["embed", "watch", "results", "feed", "playlist"]},
        "twitter":    {"url_regex": r'https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+', "site_query": "site:x.com OR site:twitter.com", "generic_handles": ["intent", "share", "home", "i", "search", "explore", "settings"]},
        "facebook":   {"url_regex": r'https?://(?:www\.)?facebook\.com/[A-Za-z0-9._-]+', "site_query": "site:facebook.com", "generic_handles": ["sharer", "sharer.php", "share.php", "dialog", "plugins", "tr", "groups"]},
        "instagram":  {"url_regex": r'https?://(?:www\.)?instagram\.com/[A-Za-z0-9._-]+', "site_query": "site:instagram.com", "generic_handles": ["p", "reel", "tv", "explore", "accounts"]},
        "tiktok":     {"url_regex": r'https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9._-]+', "site_query": "site:tiktok.com", "generic_handles": ["explore", "foryou", "tag", "music", "discover"]},
        "pinterest":  {"url_regex": r'https?://(?:www\.)?pinterest\.(?:com|[a-z.]+)/[A-Za-z0-9._-]+', "site_query": "site:pinterest.com", "generic_handles": ["pin", "search", "ideas", "today"]},
        "threads":    {"url_regex": r'https?://(?:www\.)?threads\.net/@[A-Za-z0-9._-]+', "site_query": "site:threads.net", "generic_handles": ["search", "explore"]},
        "xiaohongshu":{"url_regex": r'https?://(?:www\.)?xiaohongshu\.com/user/profile/[A-Za-z0-9]+', "site_query": "site:xiaohongshu.com", "generic_handles": ["explore", "search_result"]},
        "weibo":      {"url_regex": r'https?://(?:www\.)?weibo\.com/[A-Za-z0-9._-]+', "site_query": "site:weibo.com", "generic_handles": ["search", "login", "signup"]},
        "bilibili":   {"url_regex": r'https?://(?:www\.|space\.)?bilibili\.com/[0-9]+', "site_query": "site:bilibili.com", "generic_handles": ["search", "video", "v"]},
        "douyin":     {"url_regex": r'https?://(?:www\.)?douyin\.com/user/[A-Za-z0-9_-]+', "site_query": "site:douyin.com", "generic_handles": ["search", "discover"]},
    },
    "social_block_substrings": [
        "/sharer", "/share?", "/share/", "shareArticle", "intent/tweet",
        "intent/follow", "/dialog", "/pin/create", "/watch?", "/embed/",
        "/results", "/p/", "/reel/", "/tv/", "/explore", "/tr?",
        "/plugins/", "/hashtag/", "/search",
    ],
    "serp_generic_platforms": [
        "facebook.com", "instagram.com", "youtube.com", "linkedin.com",
        "twitter.com", "x.com", "wikipedia.org", "reddit.com", "medium.com",
        "quora.com", "yelp.com", "yelp.com.sg", "tripadvisor.com", "google.com",
        "pinterest.com", "tiktok.com", "threads.net",
        "weibo.com", "xiaohongshu.com", "douyin.com", "bilibili.com",
        "zhihu.com", "baidu.com", "baike.baidu.com", "lazada.com", "shopee.com",
    ],
    "competitor_exclude_domains": [
        "_serp_generic_platforms",
        "cnet.com", "zdnet.com", "techradar.com", "pcmag.com", "tomshardware.com",
        "theverge.com", "engadget.com", "wired.com", "forbes.com", "gartner.com",
        "g2.com", "capterra.com", "getapp.com", "softwareadvice.com", "trustpilot.com",
        "trustradius.com", "sourceforge.net", "producthunt.com",
        "dictionary.com", "merriam-webster.com", "britannica.com", "investopedia.com",
        "indeed.com", "glassdoor.com", "ziprecruiter.com", "crunchbase.com",
        "bing.com", "duckduckgo.com", "yahoo.com", "msn.com", "amazon.com", "ebay.com",
    ],
}

_cache: dict[str, Any] | None = None


def _valid_rows(rows: Any, regex_idx: int) -> bool:
    """A detector section must be a list of 3-tuples whose regex column compiles.

    Guards the downstream tuple-unpack (`for a, b, c in ROWS`) and `re.search`
    so a malformed external row can never raise ValueError / re.error at use.
    """
    if not isinstance(rows, list) or not rows:
        return False
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) != 3:
            return False
        # Every column must be a string: the regex column is fed to re.search,
        # the others become dict keys / labels. A non-str sneaking through would
        # raise at probe RUNTIME (re.search(123, ...) / unhashable key), not here.
        if not all(isinstance(x, str) for x in r):
            return False
        try:
            re.compile(r[regex_idx])
        except re.error:
            return False
    return True


def _valid_social(d: Any) -> bool:
    if not isinstance(d, dict) or not d:
        return False
    for cfg in d.values():
        if not isinstance(cfg, dict) or not isinstance(cfg.get("url_regex"), str):
            return False
        try:
            re.compile(cfg["url_regex"], re.I)
        except re.error:
            return False
        if "site_query" in cfg and not isinstance(cfg["site_query"], str):
            return False
        # generic_handles items become set members — must be hashable strings,
        # else set(...) raises TypeError inside get_social_generic().
        gh = cfg.get("generic_handles")
        if gh is not None and (
            not isinstance(gh, (list, tuple)) or not all(isinstance(x, str) for x in gh)
        ):
            return False
    return True


def _valid_str_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) for x in v)


_VALIDATORS = {
    "competitor_exclude_domains": _valid_str_list,
    "cdn_header_rules": lambda v: _valid_rows(v, 1),      # (header, value_regex, label)
    "tag_detectors": lambda v: _valid_rows(v, 2),         # (key, label, regex)
    "feature_detectors": lambda v: _valid_rows(v, 2),     # (key, label, regex)
    "social_platforms": _valid_social,
    "social_block_substrings": _valid_str_list,
    "serp_generic_platforms": _valid_str_list,
}


def _load_registry() -> dict[str, Any]:
    """Load the JSON registry; fall back to bundled defaults per-key on any error.

    A present-but-partial JSON only overrides the keys it defines AND validates;
    missing or malformed keys keep their bundled defaults. The loader and every
    getter are guaranteed never to raise — worst case is identical-to-bundled.
    """
    global _cache
    if _cache is not None:
        return _cache
    registry: dict[str, Any] = {k: v for k, v in _DEFAULTS.items()}
    if _REGISTRY_PATH.exists():
        try:
            with _REGISTRY_PATH.open("r", encoding="utf-8") as f:
                external = json.load(f)
            if isinstance(external, dict):
                for key in _DEFAULTS:
                    if key not in external or not external[key]:
                        continue
                    validator = _VALIDATORS.get(key)
                    try:
                        ok = validator(external[key]) if validator else True
                    except Exception:  # validator itself must never break loading
                        ok = False
                    if ok:
                        registry[key] = external[key]
                    else:
                        logger.warning(
                            "detection_signatures.json[%s] malformed; keeping bundled defaults", key
                        )
            else:
                logger.warning("detection_signatures.json not a dict; using defaults")
        except Exception as e:  # JSON decode, OS, UnicodeDecode — never propagate
            logger.warning("detection_signatures.json unreadable (%s); using defaults", e)
    _cache = registry
    return registry


def _as_tuples(rows: list) -> list[tuple]:
    """Normalize JSON list-of-lists into list-of-tuples (matches inline dicts)."""
    return [tuple(r) for r in rows]


def get_cdn_header_rules() -> list[tuple[str, str, str]]:
    return _as_tuples(_load_registry()["cdn_header_rules"])


def get_tag_detectors() -> list[tuple[str, str, str]]:
    return _as_tuples(_load_registry()["tag_detectors"])


def get_feature_detectors() -> list[tuple[str, str, str]]:
    return _as_tuples(_load_registry()["feature_detectors"])


def get_social_url_re() -> dict[str, re.Pattern[str]]:
    return {
        plat: re.compile(cfg["url_regex"], re.I)
        for plat, cfg in _load_registry()["social_platforms"].items()
    }


def get_social_generic() -> dict[str, set[str]]:
    return {
        plat: set(cfg.get("generic_handles", []))
        for plat, cfg in _load_registry()["social_platforms"].items()
    }


def get_social_site_map() -> dict[str, str]:
    return {
        plat: cfg["site_query"]
        for plat, cfg in _load_registry()["social_platforms"].items()
        if cfg.get("site_query")
    }


def get_social_block_substrings() -> tuple[str, ...]:
    return tuple(_load_registry()["social_block_substrings"])


def get_serp_generic_platforms() -> set[str]:
    return set(_load_registry()["serp_generic_platforms"])


def get_competitor_exclude_domains() -> set[str]:
    """Domains that are never a brand's competitor (social + media/review/
    reference/job/search aggregators). Used to keep AI-citation competitor
    tallies clean. The token '_serp_generic_platforms' expands to that list so
    the two stay in sync without duplication."""
    reg = _load_registry()
    out: set[str] = set()
    for d in reg.get("competitor_exclude_domains", []):
        if d == "_serp_generic_platforms":
            out |= set(reg["serp_generic_platforms"])
        else:
            out.add(d)
    return out
