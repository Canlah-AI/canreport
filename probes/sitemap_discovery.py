#!/usr/bin/env python3
"""Sitemap-driven URL discovery — the single source of truth for "what real
URLs does this site actually have?"

Three probes used to each guess paths independently and disagree:
  - eac_trust_probe HEAD-checked a hardcoded list (/blog, /news, ...) and
    declared has_blog=False for Shopify stores whose blog lives at
    /blogs/<handle> (e.g. /blogs/news) -> false negative.
  - content_freshness_scan already parsed sitemaps correctly but kept the logic
    private and discarded the URL lists (only counts survived).
  - news_coverage_scan never looked at the site's own /press or /news.

This module owns the canonical robots.txt + sitemap-index + child-sitemap
discovery logic and returns REAL, CATEGORIZED URLs so callers stop guessing.

Stdlib only. Importable as `from probes.sitemap_discovery import discover_urls`.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 15
MAX_URLS = 5000
MAX_CHILD_SITEMAPS = 25

# ── Path classification ────────────────────────────────────────────────────
# IMPORTANT: BLOG_RE must match Shopify's /blogs/<handle> pattern. The original
# bug was that the trust probe only knew about /blog (singular), so a Shopify
# store blogging at /blogs/news or /blogs/gazebo-tips was reported as "no blog".
BLOG_RE = re.compile(
    r"/blogs?(?:/|$)"          # /blog, /blogs, /blogs/<handle>  (Shopify)
    r"|/news(?:/|$)"
    r"|/articles?(?:/|$)"
    r"|/posts?(?:/|$)"
    r"|/insights?(?:/|$)"
    r"|/stories(?:/|$)"
    r"|/journal(?:/|$)"
    r"|/magazine(?:/|$)"
    r"|/resources?(?:/|$)"
    r"|/press(?:/|$)",
    re.IGNORECASE,
)
# Shopify ships a dedicated sitemap_blogs_N.xml child sitemap — if we ever see
# one, every loc inside it is editorial regardless of its path shape.
SHOPIFY_BLOG_SITEMAP_RE = re.compile(r"sitemap_blogs?[_-]?\d*\.xml", re.IGNORECASE)
PRODUCT_RE = re.compile(r"/products?(?:/|$)|/p/|/item(?:/|$)|/sku/", re.IGNORECASE)
COLLECTION_RE = re.compile(r"/collections?(?:/|$)|/category(?:/|$)|/shop(?:/|$)",
                           re.IGNORECASE)


@dataclass
class SitemapResult:
    """Categorized real-URL payload from sitemap + robots discovery."""
    target_url: str                       # normalized base, no trailing slash
    sitemap_found: bool
    sitemap_urls: list[str] = field(default_factory=list)   # sitemaps fetched
    robots_sitemap_directives: list[str] = field(default_factory=list)
    urls: dict[str, list[str]] = field(default_factory=dict)  # categorized payload
    lastmods: dict[str, str] = field(default_factory=dict)    # url -> lastmod
    child_sitemaps_total: int = 0
    child_sitemaps_fetched: int = 0
    truncated: bool = False

    # ── Convenience accessors callers want ─────────────────────────────────
    @property
    def blog_urls(self) -> list[str]:
        return self.urls.get("blog", [])

    @property
    def product_urls(self) -> list[str]:
        return self.urls.get("product", [])

    @property
    def has_blog(self) -> bool:
        return bool(self.urls.get("blog"))

    @property
    def total_urls(self) -> int:
        return sum(len(v) for v in self.urls.values())

    def category_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.urls.items()}

    def first_blog_url(self) -> str | None:
        blogs = self.urls.get("blog")
        return blogs[0] if blogs else None


# ── HTTP ────────────────────────────────────────────────────────────────────

def _fetch(url: str, timeout: int = TIMEOUT) -> str | None:
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, OSError) as exc:
        logger.debug("fetch failed %s: %s", url, exc)
        return None


def _norm(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


# ── Sitemap parsing ──────────────────────────────────────────────────────────

def _parse_sitemap(xml_text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return ([(loc, lastmod), ...], [child_sitemap_urls])."""
    entries: list[tuple[str, str | None]] = []
    children: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries, children
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    for sm in root.findall(f"{ns}sitemap"):
        loc = sm.find(f"{ns}loc")
        if loc is not None and loc.text:
            children.append(loc.text.strip())
    for u in root.findall(f"{ns}url"):
        loc = u.find(f"{ns}loc")
        if loc is None or not loc.text:
            continue
        lm = u.find(f"{ns}lastmod")
        entries.append((loc.text.strip(),
                        lm.text.strip() if lm is not None and lm.text else None))
    return entries, children


def _categorise(url: str, parent_sitemap: str | None = None) -> str:
    """Classify a URL into blog | product | collection | page | other.

    parent_sitemap lets a Shopify sitemap_blogs_N.xml force every child loc to
    "blog" even when the path shape is ambiguous.
    """
    if parent_sitemap and SHOPIFY_BLOG_SITEMAP_RE.search(parent_sitemap):
        return "blog"
    path = urlparse(url).path.lower()
    if BLOG_RE.search(path):
        return "blog"
    if PRODUCT_RE.search(path):
        return "product"
    if COLLECTION_RE.search(path):
        return "collection"
    # Top-level (zero or one path segment) -> a "page" (home, /about, /contact)
    if len([s for s in path.split("/") if s]) <= 1:
        return "page"
    return "other"


# ── Public API ────────────────────────────────────────────────────────────────

def discover_urls(url: str, max_urls: int = MAX_URLS,
                  max_child_sitemaps: int = MAX_CHILD_SITEMAPS) -> SitemapResult:
    """Discover real, categorized URLs for a site via robots.txt + sitemaps.

    Resolution order:
      1. robots.txt `Sitemap:` directives
      2. /sitemap.xml, /sitemap_index.xml fallbacks
      3. recurse one level into child sitemaps (capped)

    Returns a SitemapResult whose `.urls` is {blog, product, collection,
    page, other} -> [real urls]. Never raises on network errors — an
    unreachable site yields sitemap_found=False and empty url lists, which
    callers treat as "fall back to path guessing".
    """
    base = _norm(url)
    result = SitemapResult(target_url=base, sitemap_found=False)

    candidates: list[str] = []
    robots = _fetch(f"{base}/robots.txt")
    if robots:
        for line in robots.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    result.robots_sitemap_directives.append(val)
                    candidates.append(val)
    for p in ("/sitemap.xml", "/sitemap_index.xml"):
        full = f"{base}{p}"
        if full not in candidates:
            candidates.append(full)

    seen_urls: set[str] = set()
    urls: dict[str, list[str]] = {
        "blog": [], "product": [], "collection": [], "page": [], "other": [],
    }

    def _add(loc: str, lastmod: str | None, parent_sitemap: str | None) -> None:
        if loc in seen_urls:
            return
        seen_urls.add(loc)
        cat = _categorise(loc, parent_sitemap)
        urls[cat].append(loc)
        if lastmod:
            result.lastmods[loc] = lastmod

    for sm_url in candidates:
        if len(seen_urls) >= max_urls:
            result.truncated = True
            break
        xml = _fetch(sm_url)
        if not xml:
            continue
        entries, children = _parse_sitemap(xml)
        if entries or children:
            result.sitemap_found = True
            result.sitemap_urls.append(sm_url)
        for loc, lm in entries:
            _add(loc, lm, sm_url)
        result.child_sitemaps_total += len(children)
        for child in children:
            if result.child_sitemaps_fetched >= max_child_sitemaps:
                result.truncated = True
                break
            if len(seen_urls) >= max_urls:
                result.truncated = True
                break
            result.child_sitemaps_fetched += 1
            cxml = _fetch(child)
            if not cxml:
                continue
            result.sitemap_urls.append(child)
            ce, _grandchildren = _parse_sitemap(cxml)
            for loc, lm in ce:
                _add(loc, lm, child)

    if result.child_sitemaps_total > result.child_sitemaps_fetched:
        result.truncated = True
    result.urls = urls
    return result


# ── CLI (debugging) ────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Sitemap-driven URL discovery.")
    ap.add_argument("url")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    r = discover_urls(args.url)
    out = {
        "target_url": r.target_url,
        "sitemap_found": r.sitemap_found,
        "sitemap_urls": r.sitemap_urls,
        "robots_sitemap_directives": r.robots_sitemap_directives,
        "category_counts": r.category_counts(),
        "has_blog": r.has_blog,
        "first_blog_url": r.first_blog_url(),
        "blog_urls_sample": r.blog_urls[:10],
        "child_sitemaps_total": r.child_sitemaps_total,
        "child_sitemaps_fetched": r.child_sitemaps_fetched,
        "truncated": r.truncated,
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
