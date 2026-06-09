"""Deep business-understanding pass.

Runs BEFORE any audit. Reads multiple key pages of the target site (homepage +
about/products/solutions + product links found on the homepage), then asks the
LLM to synthesize ONE structured profile of what the company actually does —
capturing ALL distinct business lines, not just the most prominent one a single
homepage snippet would surface.

Why it exists: the legacy classifier fed only ~600 chars of homepage body to the
LLM, so multi-business manufacturers got mis-/under-classified (e.g. a rugged-
computing + VR/AR group reduced to "rugged industrial computers"). That shallow
read poisoned the AI-citation buyer queries (wrong/narrow category) and the
report never clearly stated what the brand does. This module fixes the root.

Output feeds: (1) engine.build_buyer_queries (clean canonical category), and
(2) a "what you do" section in the report contract.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests

from .eac_trust_probe import (
    _BAIDU_BASE,
    _BAIDU_KEY,
    _CLASSIFY_MODEL,
    _CLASSIFY_TIMEOUT,
    _fetch,
    _strip_body_text,
)

logger = logging.getLogger("understand_business")

# Path/anchor hints that usually lead to business-describing pages.
_PAGE_HINTS = (
    "about", "about-us", "aboutus", "company", "profile", "who-we-are",
    "products", "product", "solutions", "solution", "industries", "industry",
    "applications", "application", "technology", "services",
)

_MAX_PAGES = 8
_PER_PAGE_CHARS = 1200
_HOME_CHARS = 2400
_BLOB_CAP = 14000

_SYNTH_PROMPT = """You are a B2B / industrial market analyst. Below is text scraped from MULTIPLE pages of ONE company's website. Produce a STRICT JSON business profile.

CRITICAL: capture EVERY distinct business line / segment, not just the most prominent. Multi-business manufacturers often run several segments (e.g. rugged computing AND VR/AR AND consumer electronics) that a single page would miss. If the text shows multiple segments, list them all.

GROUNDING (equally critical): every segment, product line and proof point MUST be backed by an actual PRODUCT or SOLUTION the company presents — shown in product/solution sections, navigation, or page headings with real offerings beneath them. Do NOT infer, generalize, or invent a business the company is not visibly in.

Ignore SEO / meta-tag artifacts: a word that appears only as an isolated keyword (in a meta description, title tag, or keyword list) WITHOUT a corresponding product/solution section is NOT a business segment — skip it. For example, a hardware manufacturer whose meta description happens to contain "software agent" / "软件代理" / "consulting" but whose actual products are all physical devices is NOT a "Software Agency"; do not list such a segment. When a candidate segment has no concrete product/solution behind it in the text, omit it. Prefer fewer, well-evidenced segments over a longer speculative list.

Return ONLY this JSON (no prose, no code fence):
{{
  "summary_en": "<2-3 sentences, plain English: what this company does>",
  "summary_zh": "<中文 2-3 句：这家公司到底做什么>",
  "product_lines": ["<distinct product line>", "..."],
  "segments": ["<distinct business segment / category>", "..."],
  "target_buyers": "<who buys: B2B / OEM / ODM, end markets, use cases>",
  "positioning": "<differentiators / positioning claims, if any>",
  "proof_points": ["<year founded, factories, certifications, named clients>", "..."],
  "canonical_categories": ["<2-4 clean search-category nouns a real AI buyer would type, e.g. 'rugged tablets', 'industrial handheld computers', 'AR smart glasses'>"],
  "primary_category": "<the single most representative search-category noun>"
}}

WEBSITE TEXT (page tag in brackets):
{pages}
"""


def _discover_pages(base_url: str, home_html: str) -> list[str]:
    """Same-host links whose href hints at a business-describing page."""
    host = urlparse(base_url).netloc
    found: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', home_html or "", re.I):
        href = m.group(1).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if not any(h in href.lower() for h in _PAGE_HINTS):
            continue
        full = urljoin(base_url, href).split("#")[0].rstrip("/")
        if urlparse(full).netloc != host or full in seen:
            continue
        seen.add(full)
        found.append(full)
    # Always probe a few canonical paths even if not linked with a hint anchor.
    for p in ("/about", "/about-us", "/products", "/solutions"):
        full = urljoin(base_url, p).rstrip("/")
        if full not in seen:
            seen.add(full)
            found.append(full)
    return found[:_MAX_PAGES]


def _call_llm(prompt: str) -> dict | None:
    if not _BAIDU_KEY:
        logger.warning("BAIDU_API_KEY not set; skipping business understanding")
        return None
    endpoint = f"{_BAIDU_BASE}/models/{_CLASSIFY_MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    try:
        resp = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {_BAIDU_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=_CLASSIFY_TIMEOUT * 2,
        )
        resp.raise_for_status()
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
        return json.loads(raw)
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        logger.warning("business synthesis failed: %s", e)
        return None


def understand_business(url: str, home_html: str | None = None) -> dict | None:
    """Read several key pages, synthesize what the company does. None on failure."""
    if home_html is None:
        home_html, _ = _fetch(url)
    if not home_html:
        logger.warning("no homepage HTML for %s", url)
        return None

    pages: list[str] = [f"[HOMEPAGE] {_strip_body_text(home_html, _HOME_CHARS)}"]
    read_urls: list[str] = [url]
    for purl in _discover_pages(url, home_html):
        html, status = _fetch(purl)
        if status == 200 and html:
            txt = _strip_body_text(html, _PER_PAGE_CHARS)
            if txt:
                pages.append(f"[{urlparse(purl).path or '/'}] {txt}")
                read_urls.append(purl)

    blob = "\n\n".join(pages)[:_BLOB_CAP]
    profile = _call_llm(_SYNTH_PROMPT.format(pages=blob))
    if not profile:
        return None
    profile["_pages_read"] = len(pages)
    profile["_read_urls"] = read_urls
    return profile
