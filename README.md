# Audit Report Generator (presentation layer)

One command turns a URL into a branded HTML + PDF audit report. This repo is the
**presentation layer**: it runs the on-site infra audit, pulls off-site SEO data
from the separate `site-audit` probe engine, merges both into a neutral
report contract, and renders it through a pluggable template. The Google EAC report
is just the **first template** — drop a new folder under `templates/` to add your own.

## Architecture — three layers

```
DATA                         CONTRACT                  PRESENTATION
site-audit  ──┐
  (off-site probes)     ├─→  contract.py  ─────────→  templates/google/   ─→ HTML/PDF
this repo's eac_* probes┘    (neutral report dict)    templates/canmarket/ (your own)
  (on-site infra)
```

- **Data layer** never changes when you swap templates.
- **Templates** only restyle the same contract — see `contract.py` for the field list.
- Wiring to the engine is resolved via `$SITE_AUDIT_PATH` (legacy
  `$CANMARKET_AUDIT_PATH` still honored as a fallback; default
  `~/dev/site-audit-v1.1`); off-site probes are loaded by file path so the
  two repos' `probes` packages never collide.

## One command

```bash
pip install requests jinja2 weasyprint
export SERPER_API_KEY=...   # for off-site probes

# Full report: on-site + off-site, HTML + PDF, Google template
python report.py modernshade.org --brand "Modern Shade" --market US,EU,SEA --open

# Pick a template / format
python report.py example.com --template google --format pdf
python report.py --list-templates

# On-site only (no Serper calls)
python report.py example.com --no-offsite
```

Output lands in `output/{domain}/{template}-audit-{date}.{html,pdf,json}`.

## What it audits

**On-site infra** (4 modules, this repo's `probes/`):

| Module | Probe | Checks |
|--------|-------|--------|
| Global Speed | `eac_speed_probe` | TTFB, LCP (mobile+desktop), CDN, server geo, HTTP/2 |
| Lead Form Security | `eac_form_probe` | CAPTCHA, form friction, CMS detection, anti-spam |
| Trust Content | `eac_trust_probe` | Blog/content, product trust signals, SSL |
| Conversion Tracking | `eac_tracking_probe` | GA4, GTM, Google Ads, Meta Pixel, Consent Mode v2 |

**Off-site SEO** (3 modules, from the `site-audit` engine):

| Module | Probes | Checks |
|--------|--------|--------|
| Off-Site Authority | `backlink_scan`, `community_mention_scan`, `news_coverage_scan` | external mentions, Reddit/Quora, news coverage |
| Schema & AI Readiness | `schema_validator`, `content_freshness_scan` | JSON-LD, sitemap, content freshness |
| Social & NAP | `social_influence_scan`, `nap_consistency_scan` | follower reach, NAP consistency |

### Scoring

| Severity | Deduction | SLA |
|----------|-----------|-----|
| P0 — Critical | -25 pts | Fix within 7 days |
| P1 — Warning | -10 pts | Fix within 30 days |
| P2 — Info | -3 pts | Nice to have |

Overall score = 100 - total deductions (floor 0).

## Adding a template

Copy `templates/google/` to `templates/<name>/`, restyle `report.html.j2`
(it consumes the contract documented in `templates/canmarket/README.md`), and run
`python report.py <url> --template <name>`. No data-layer changes.

## Legacy direct CLI

`eac_audit.py` still runs the on-site 4-probe audit standalone (no off-site, no
template selection):

```bash
python eac_audit.py https://example.com --company "Example Corp" --market US,EU
```

### CLI Options

| Flag | Required | Description |
|------|----------|-------------|
| `url` | Yes | Target website URL |
| `--form-url` | No | Lead form page (skips form probe if omitted) |
| `--thank-you-url` | No | Thank-you page for conversion tracking check |
| `--product-page` | No | Product detail page for trust signal audit |
| `--company` | No | Company name (used in report header) |
| `--market` | No | Target markets, comma-separated (default: `全球`) |
| `--output-dir` | No | Output path (default: `output/{domain}`) |
| `--open` | No | Open HTML report in browser after generation |

## Project Structure

```
canreport/
├── report.py                 # CLI entry — URL → contract → template → HTML/PDF
├── engine.py                 # Adapter to the site-audit off-site probe engine
├── contract.py               # Neutral report contract (template-agnostic field set)
├── scoring.py                # Severity → deduction scoring
├── reconcile.py              # Cross-checks contract against raw probe data
├── weekly_report.py          # Weekly-agent-report variant
├── eac_audit.py              # Legacy standalone on-site 4-probe CLI
├── eac_demo.py               # Demo data generator for template testing
├── probes/                   # On-site infra probes (eac_*) + helpers
│   ├── eac_speed_probe.py    # TTFB, LCP, CDN, server location
│   ├── eac_form_probe.py     # CAPTCHA, form fields, CMS detection
│   ├── eac_trust_probe.py    # Blog, product specs, testimonials, SSL
│   ├── eac_tracking_probe.py # GA4, GTM, Ads, Consent Mode v2
│   ├── lighthouse_psi.py     # Google PageSpeed Insights API wrapper
│   └── ...                   # detection signatures, sitemap/prompt discovery, etc.
├── templates/                # Pluggable presentation layer
│   ├── google/               # report.html.j2, cover.html, executive.html, assets/
│   ├── canmarket/
│   └── weekly-agent-report/
└── render/
    └── html_to_pdf.py        # WeasyPrint HTML → PDF converter
```

## How It Works

```
                    ┌─────────────────┐
                    │   eac_audit.py  │
                    │  (orchestrator) │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───┐  ┌──────▼─────┐ ┌──────▼──────┐
     │ speed_probe │  │ form_probe │ │ trust_probe │
     │   + PSI API │  │            │ │             │
     └─────────────┘  └────────────┘ └─────────────┘
              │              │              │
              │       ┌──────▼──────┐       │
              │       │tracking_probe│      │
              │       └─────────────┘       │
              └──────────────┬──────────────┘
                             │
                    ┌────────▼────────┐
                    │  Jinja2 render  │
                    │  HTML → PDF     │
                    └─────────────────┘
```

All probes run in parallel via `ThreadPoolExecutor(max_workers=4)`.

## Dependencies

- **Python 3.10+**
- `requests` — HTTP client for probes
- `jinja2` — HTML report templating
- `weasyprint` — HTML to PDF conversion (optional, requires system deps)

No API keys required. The PageSpeed Insights API allows 25K queries/day per IP without authentication.

## Sample Reports

Generated reports land under `output/{domain}/`. Recent examples:

| Site | Score | Date | Key Findings |
|------|-------|------|-------------|
| canlah.ai | 77/100 | 2026-05-25 | Cloudflare CDN active, missing Consent Mode v2, product page needs trust signals |
| modernshade.org | 70/100 | 2026-05-25 | No blog/content section, missing product specs, needs competitor comparison |

## Extending

Each probe follows the same interface:

```python
def probe(url: str, **kwargs) -> dict:
    """Returns dict with 'findings' list and '_probe_status' string."""
    return {
        "_probe_status": "ok",  # or "error" / "skipped"
        "findings": [
            {
                "severity": "P1",
                "title_zh": "...",
                "impact_zh": "...",
                "action_zh": "...",
                "code_snippet": "...",  # optional inline fix
            }
        ]
    }
```

To add a new probe: create `probes/eac_new_probe.py` with a `probe()` function, then wire it into `eac_audit.py`.

## License

MIT

---

Built by [Canlah AI](https://canlah.ai) for the Google EAC program.

## Fonts (required for correct CJK rendering)

The Google template renders Chinese + Latin mixed text. It needs two fonts
installed so weights match across scripts (WeasyPrint does not synthesize bold
for CJK — a missing-weight CJK face renders thin next to bold Latin):

```bash
brew install --cask font-noto-sans-sc font-inter
```

`font-family` stacks lead with `Inter` (Latin) then `Noto Sans SC` (CJK). Do NOT
use `PingFang SC` in the stack — fontconfig mis-aliases it to Verdana (a
Latin-only font), which breaks CJK weight matching.
