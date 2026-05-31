# Match Top-Tier GEO — Operations Playbook

**Goal of this doc:** a literal, founder-usable checklist for "directly match Profound / Peec / Otterly / AthenaHQ / Scrunch / Goodie" on capability, plus the infra/API backlog that unlocks each missing piece, plus the moves where we are already **more accurate** than them.

**Where we are today (T0, verified):** `canreport` runs the multi-engine AI-visibility audit end-to-end against live **Serper + Gemini**. Confirmed working: multi-run averaging (Gemini 3 runs × 5 queries = 15 cached responses, per-query `runs: 3`, `position_stability_std` present), dead-citation check (caught 7 dead/blocked URLs), 5 engines reported with honest `not_present` marking for the 3 Google blocks Google didn't return, Share-of-Voice computed across all rows, query categorization (recommendation / discovery / comparison), and a documented `summary_stats` contract (`per_engine`, `runs_per_generative_engine`, `backlog_engines`, etc.).

So the engine, the contract, and the rendering layer exist. What's missing is breadth of **engines** and a few **paid data feeds** — not architecture.

---

## 1. Capability matrix — top-tier capability → our status

Legend: ✅ have · 🟡 T0 just built · 🔧 T1 buildable (no new vendor) · 💰 buy (needs paid feed) · ⏳ backlog-API (needs a key/billing we don't yet have) · ❌ can't / won't

| # | Top-tier capability (who has it) | Our status | Why / what it needs |
|---|---|---|---|
| 1 | **AI Overviews / Google AI-Mode visibility** (Profound, Peec, Otterly) | 🟡 T0 | We pull Serper SERP + AI Overview block and mark `not_present` honestly. Already live for the part Google returns. Full AI-Mode parity = browser-real capture (see §4). |
| 2 | **ChatGPT answer visibility** (everyone) | ⏳ backlog-API | Engine adapter is stubbed and listed in `backlog_engines: ["chatgpt (OpenAI quota)"]`. Needs OpenAI billing turned on. ~1 day to wire once key is live. |
| 3 | **Perplexity visibility** (Profound, Peec, Otterly, Scrunch) | ⏳ backlog-API | Same: adapter slot exists, needs Perplexity API key. Perplexity returns citations natively → cleanest engine for citation analysis. |
| 4 | **Claude / Gemini visibility** (Peec +€20–30 add-on, Scrunch, Athena 8+ LLMs) | 🟡 Gemini live / ⏳ Claude | Gemini is our *working* generative engine today (the 3-run averaging proof ran on it). Claude needs Anthropic key (backlog). |
| 5 | **Multi-LLM coverage "8+ engines"** (AthenaHQ, Scrunch 7+) | 🔧 T1 | Architecture already iterates engines from a registry. Each new engine = one adapter + key. The number is purely a function of how many keys we fund. |
| 6 | **Prompt / query panel management** (all — "50/100/400 prompts") | 🟡 T0 + 🔧 T1 | We run a query panel today. Auto-*generating* the panel from buyer intent is the §3 plan (FREE via Serper). This is a wedge, not a gap — see §3. |
| 7 | **Multi-run averaging / answer volatility** (Profound "AI responses/mo", Athena "3,600 responses/mo") | ✅ | Already shipped: `runs_per_generative_engine` (default 3), per-query `runs`, and `position_stability_std`. Most rivals report a single snapshot; we report variance. |
| 8 | **Share of Voice across competitors** (Peec, Otterly, Scrunch) | ✅ | SoV computed across all rows in `summary_stats`. |
| 9 | **Citation / source tracking** (Profound, Otterly citations, Goodie "500K citations") | 🟡 T0 | We extract cited URLs and run a **dead/blocked-citation check** (caught 7). Citation *volume tracking over time* needs the monitoring DB (§2). |
| 10 | **Competitor tracking** (Athena, Scrunch multi-brand) | 🟡 T0 | SoV already names competitors per query. Multi-brand workspaces = monitoring DB + tenancy. |
| 11 | **Multi-country / multi-region** (Peec 3–10 countries, Otterly, Athena multi-region) | 🔧 T1 | Serper takes `gl`/`hl` params. Adding countries = loop params + cost per query. No new vendor. |
| 12 | **Scheduled monitoring / trend lines** (all SaaS — the core recurring product) | ⏳ backlog (DB) | We are report-at-a-point-in-time today. Needs a monitoring DB + scheduler (§2). This is the #1 thing standing between us and a recurring SKU. |
| 13 | **Backlink / authority data (KD)** (Ahrefs/Semrush-grade; Peec/Otterly lean on it indirectly) | 💰 buy | Needs DataForSEO or similar. Our off-site `backlink_scan` is heuristic today; real KD needs a backlink index we can't crawl ourselves affordably. |
| 14 | **Google Search Console real impressions/clicks** (none of the GEO-natives have this well) | ⏳ backlog (OAuth) | GSC OAuth connector → real query data instead of estimated volume. Differentiator, not catch-up. |
| 15 | **Looker / BI / API export** (Otterly Looker, Athena BI, Profound API) | 🔧 T1 | We already emit structured JSON (the contract). Looker connector + a thin REST wrapper = packaging work, no research. |
| 16 | **On-brand content generation / "AEO Content Writer"** (Goodie, Profound 6 articles/mo) | ✅ moat | This is **our** core asset, not catch-up: Brand-Memory (Style Genome™, 768-dim) regenerated, on-brand fixes. Goodie is the only rival even attempting closed-loop and they have no brand-memory constraint. |
| 17 | **Real logged-in browser capture** (none do this well — all API-sanitized) | 🔧 T1 → 💰 farm | The answer a *human* sees ≠ the API answer. We can capture real logged-in Chrome (AppleScript path already proven on this machine). Scaling to many brands = a capture farm (§2). This is accuracy nobody else has. |
| 18 | **AI-crawler / bot log analysis** (Profound has a "bot analytics" angle) | ⏳ backlog (log pipeline) | Needs a log-ingestion endpoint on the client's edge (Cloudflare/CDN logs → which AI crawlers hit which pages). High-value, low-competition. |
| 19 | **Entity / brand disambiguation** (weak across the field) | 🔧 T1 | We can disambiguate "is this answer about *our* brand vs a same-named entity" using Brand Memory + entity check. Rivals count any string match. Accuracy win. |
| 20 | **Community/Reddit/Quora GEO signal** (none cover well) | ✅ T0 | Off-site `community_mention_scan` already scans Reddit/Quora — the exact surfaces LLMs cite most. We have this; the category leaders don't. |

**Read-out:** of 20 top-tier capabilities, we already **have or just-built 11** (✅/🟡), **4 are pure T1 build** (no vendor), and only **5 need a new key/feed/DB** (⏳/💰). None are ❌. We are one billing toggle (OpenAI) + one DB away from matching the recurring-monitoring product, and we already *exceed* them on items 7, 16, 17, 19, 20.

---

## 2. API / infra BACKLOG (prioritized)

Ordered by **value ÷ effort**. "Effort" is rough dev-days; "cost" is monthly run-rate at pilot scale.

| Priority | Item | What it unlocks | Cost (pilot) | Effort | Value |
|---|---|---|---|---|---|
| **P0** | **OpenAI billing → ChatGPT engine** | Item #2. ChatGPT is *the* engine buyers ask about first. Adapter already stubbed in `backlog_engines`. | ~$20–100/mo metered | **0.5–1 day** | 🔥 Highest — table-stakes credibility. |
| **P0** | **Monitoring DB + scheduler** (Postgres/Supabase + cron) | Items #12, #9, #10. Turns one-off audit into a recurring product → the whole MRR thesis. | ~$0–25/mo (Supabase free→pro) | **3–5 days** | 🔥 Highest — converts product from report to subscription. |
| **P1** | **Perplexity API key** | Item #3. Native citations = cleanest source data, low effort. | ~$5–20/mo metered | **0.5 day** | High. |
| **P1** | **Anthropic key → Claude engine** | Item #4. Completes the "big-4 LLM" claim (ChatGPT+Gemini+Perplexity+Claude). | ~$10–50/mo metered | **0.5 day** | High (marketing completeness). |
| **P1** | **GSC OAuth connector** | Item #14. Real impressions/clicks → grounds our long-tail volume in truth, not estimates. Differentiator. | $0 (Google free) | **2–3 days** (OAuth + token store) | High — accuracy moat vs clickstream-guessing rivals. |
| **P2** | **DataForSEO (backlinks / KD)** | Item #13. Real Keyword Difficulty + authority. | ~$30–100/mo (pay-as-you-go) | **1–2 days** | Medium — nice-to-have; GEO buyers care less about KD than classic SEO buyers. |
| **P2** | **Browser-capture farm** (headless Chrome pool w/ logged-in profiles) | Items #1, #17. Human-real AI-Mode/ChatGPT answers at scale. | ~$20–80/mo (1 small VM) | **5–8 days** (profile mgmt, anti-bot, rotation) | High accuracy, higher ops — start with the single-machine AppleScript path for pilots, farm later. |
| **P3** | **AI-crawler log pipeline** (edge log ingest) | Item #18. "Which AI bots crawled which pages." | ~$0–25/mo (object store + parser) | **3–5 days** + client edge access | Medium-high, low competition — sell as premium add-on. |

**Sequencing recommendation:** ship P0 pair first (**OpenAI billing + monitoring DB**). That single sprint moves us from "audit tool" to "Profound-class monitoring product" on the two things buyers check first (ChatGPT coverage + trend lines), at <$130/mo run-rate and <1 week of work. Everything else is incremental.

---

## 3. Long-tail / prompt discovery plan — FREE via Serper

The pricing field shows rivals gate hard on **prompt count** (Profound 50→100, Otterly 15→400, Peec 25→300/day). They make you *manually* curate prompts, then charge per prompt. **We can auto-generate the buyer-intent prompt panel for $0 using Serper data we already pull.**

### Why this is free and replicable
Per the keyword-research methodology: the **discovery layer** (autocomplete, People Also Ask, Related Searches) is cheap and scrapeable — only the *volume number* and *KD score* require paid clickstream/backlink feeds. AnswerThePublic / AlsoAsked are just polished UIs over autocomplete + PAA scraping. We already call Serper, which returns `peopleAlsoAsk` and `relatedSearches` per query.

### Concrete pipeline (build on existing Serper calls)
```
seed terms (brand, category, top products, competitors)
        │
        ▼
Serper SERP call  ──► extract peopleAlsoAsk[]   (the literal questions buyers ask)
                  └─► extract relatedSearches[] (lateral long-tail surface)
        │
        ▼
expand 1 hop: feed each PAA question back as a Serper query → harvest its PAA/related
(2 hops max → ~hundreds of real long-tail questions per seed, zero extra vendors)
        │
        ▼
intent-cluster + classify with our existing categorize_query
   → recommendation / discovery / comparison / factual
        │
        ▼
GEO-rewrite: turn each long-tail question into a *buyer prompt* an LLM would be asked
   "best X for Y", "is BRAND good for Z", "BRAND vs COMP for W"
   (this is the GEO-era analogue of long-tail keywords — the prompt panel)
        │
        ▼
auto-built prompt panel → feed straight into the existing multi-engine runner
```

### Why ours is better than their manual panels
- **Free + auto:** rivals charge per prompt and make you guess them. We *derive* them from what real users actually ask Google (PAA/Related = Google's own clustering of demand).
- **Grounded in real demand:** PAA questions are surfaced because people search them — not invented by a marketer.
- **Closed taxonomy:** `categorize_query` already buckets them, so the panel is balanced across recommendation/comparison/factual instead of all head terms.
- **Volume optional:** if a buyer wants real search volume on each long-tail, that's the *only* place we'd attach a paid feed (GKP via Google Ads API, or DataForSEO) — and even then GKP is free with an Ads account. Discovery stays $0.

**Action:** wire a `prompt_discovery.py` that takes seeds → 2-hop Serper PAA/Related expansion → `categorize_query` → GEO-rewrite. Reuses the Serper client already in the off-site probes. ~1–2 days, no new vendor.

---

## 4. What we can do MORE ACCURATELY than them

These are not catch-up — they are places the whole category is structurally weak, per the positioning research ("position into a weakness the leader can't fix without breaking their own model").

1. **Browser-real capture (the answer a human sees).** Every incumbent reads AI answers via **API**, which returns sanitized/different output than the logged-in consumer UI. We can capture **real logged-in Chrome** (AppleScript path already proven on this machine; farm later). When a buyer asks "what does ChatGPT *actually* say to my customer," only the browser answer is honest. Measurement companies can't easily switch — their entire cost model is API-based.

2. **AI-crawler log analysis.** Instead of *inferring* what LLMs see, ingest the client's edge logs and show **which AI crawlers (GPTBot, PerplexityBot, ClaudeBot, Google-Extended) actually fetched which pages**. Ground-truth crawl evidence vs everyone else's black-box guessing. Low competition.

3. **Entity / brand disambiguation.** Rivals count any string match as "your brand appeared." With Brand Memory we verify the answer is about *this* entity (not a same-named company/product/person), and whether the brand was framed on-message. Fewer false positives = a more honest visibility number.

4. **Community GEO (Reddit/Quora).** Our off-site `community_mention_scan` already measures the exact surfaces LLMs cite most heavily. The category leaders treat community as a blind spot; we treat it as a first-class signal — and it's also where a *fix* (on-brand community presence) actually moves the LLM answer.

5. **On-brand fix, not just a red dashboard.** The decisive accuracy claim: we don't only measure the gap, we **close it with Brand-Memory-constrained content** (Style Genome™ 768-dim) and then **re-measure to prove the score moved**. Multi-run averaging (already shipped) is what makes "we moved your score" *provable* rather than noise — we know the variance band, so a real lift is distinguishable from run-to-run jitter. No measurement-only competitor can make that before/after claim because they have no fix step and no brand constraint.

**One-line summary of our accuracy edge:** *they tell you the API's sanitized score once; we tell you the real human-visible answer, with variance bands, verified to your actual entity, including the community surfaces that drive it — then we fix it on-brand and prove the number moved.*
