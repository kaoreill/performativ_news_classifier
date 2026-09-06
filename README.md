# Performativ News Classification API

A small, deterministic backend service that classifies financial news articles as **GOOD_NEWS**, **BAD_NEWS**, or **UNRELATED** to Performativ's wealth management platform business.

## Architecture

```
POST /classify
  ↓
[DETERMINISTIC]  scheme / hostname / SSRF checks
  ↓
[DETERMINISTIC]  retrieval stage 1 — direct fetch (httpx)
  ↓
[DETERMINISTIC]  content-type gate · 5MB cap · bot-challenge detection
  ↓              (on block / challenge / thin text)
[DETERMINISTIC]  retrieval stage 2 — reader fallback (jina.ai Reader)
  ↓
[DETERMINISTIC]  extraction + minimum-text check
  ↓
[PROBABILISTIC]  LLM classification (Groq)
  ↓
[DETERMINISTIC]  schema · label enum · topic vocabulary · confidence range
  ↓
[DETERMINISTIC]  SQLite persistence
  ↓
JSON response
```

**Philosophy**: deterministic ingestion and validation wrapped tightly around a single,
well-scoped probabilistic step. Stated as one sentence:

> The LLM **proposes** a classification. Deterministic validation **decides** whether that
> proposal is acceptable as an API result.

Every gate above the probabilistic step can reject a request before a single token is spent;
every gate below it can reject the model's answer before it reaches the caller.

### The output contract

Whatever the model returns, these hold for any `200` response:

| Rule | Enforced in |
|---|---|
| `label ∈ {GOOD_NEWS, BAD_NEWS, UNRELATED}` | `app/classifier.py` `_parse` |
| `confidence ∈ [0.0, 1.0]` (clamped, never rejected) | `app/classifier.py` `_parse` |
| `relevance_topics ⊆` the closed vocabulary | `app/classifier.py` `_clean_topics` |
| `UNRELATED` ⟹ `relevance_topics == []` | `app/classifier.py` `_clean_topics` |
| `reasoning` is non-empty | `app/classifier.py` `_parse` |
| total request time ≤ 45s | `app/main.py` `TOTAL_REQUEST_BUDGET` |

A proposal violating the first, fifth or (structurally) any of them is retried once, then
reported as `classification_failed`. Off-vocabulary topics are dropped rather than failing the
request, but are logged at `WARNING` so contract violations stay visible.

## Stack

- **Framework**: FastAPI
- **HTTP**: httpx
- **Extraction**: regex normalization of fetched HTML, with a jina.ai Reader fallback
- **LLM**: Groq API (openai/gpt-oss-120b)
- **Persistence**: SQLite
- **Deployment**: Render (free tier, native Python)

## Setup

```bash
conda create -n performativ python=3.11 -y
conda activate performativ
pip install -r requirements.txt

cp .env.example .env      # then add GROQ_API_KEY (required)
```

A plain virtualenv works identically (`python -m venv .venv`); nothing depends on conda.

`JINA_API_KEY` is optional locally but recommended in deployment — see Known Limitations.

## API Endpoints

### `GET /health`
Liveness check.

```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

### `POST /classify`
Classify a news article by URL.

**Request:**
```json
{"url": "https://example.com/article"}
```

**Response (success):**
```json
{
  "url": "https://...",
  "label": "GOOD_NEWS",
  "confidence": 0.9,
  "reasoning": "The article describes a new AI platform targeting independent wealth advisers, directly relating to wealth management software and AI in regulated financial workflows.",
  "relevance_topics": ["wealth_management_software", "ai_in_financial_workflows"],
  "processed_at": "2026-09-06T16:23:12.228363Z"
}
```

`confidence` is the model's **own stated** confidence in its classification, clamped to
`[0, 1]`. It is not a calibrated probability and should not be read as one — treat it as a
weak ordering signal, not a likelihood.

`relevance_topics` are drawn from a **closed vocabulary** (below) and mean *canonical
Performativ-relevant themes supporting this classification* — not "topics mentioned in the
article". The distinction matters: an article about consumer-crypto advertising rules
genuinely concerns regulation, but supports no relevance finding here, so it is `UNRELATED`
and carries **no** topics. `UNRELATED` always returns an empty array.

The vocabulary: `wealth_management_software`, `portfolio_management_systems`,
`private_banks_asset_managers`, `regulation`, `compliance_reporting`, `portfolio_analytics`,
`ai_in_financial_workflows`, `data_integration`, `legacy_modernization`,
`custodian_connectivity`.

Fixing the vocabulary is what makes the field usable by the consumers the brief names —
Slack alerts, CRM enrichment, monitoring. Free-form topics returned the same concept as both
`"Wealth management software"` and `"wealth management"` across two runs, which nothing
downstream can match on reliably.

**Response (error):**
```json
{
  "error": "extraction_failed",
  "detail": "Could not extract article text"
}
```

### `GET /latest?limit=N`
Last N classifications. `limit`: 1–100 (default 10).

```bash
curl "http://localhost:8000/latest?limit=5"
```

## Error Taxonomy

| Error | HTTP | Meaning |
|-------|------|---------|
| `invalid_url` | 422 | URL parse failure |
| `blocked_url` | 422 | SSRF block (localhost, private IP, non-http/https) |
| `http_error` | 502 | 4xx/5xx from target |
| `fetch_failed` | 504 | Timeout, DNS, or connection failure |
| `request_timeout` | 504 | Pipeline as a whole exceeded the 45s budget |
| `unsupported_content_type` | 415 | Non-HTML content (PDF, image, etc.) |
| `extraction_failed` | 422 | No meaningful article text found (paywall or bot challenge) |
| `llm_unavailable` | 502 | Classifier provider unreachable, rate-limited, or unconfigured |
| `classification_failed` | 502 | Model output still unusable after one retry |

## Classification Taxonomy

- **GOOD_NEWS**: Materially relevant to Performativ's business AND net positive
- **BAD_NEWS**: Materially relevant to Performativ's business AND net negative
- **UNRELATED**: Not materially relevant (regardless of sentiment)

**Critical**: Relevance is decided first, independently of sentiment.

### Likely Relevant Themes
Wealth management software, portfolio management systems, private banks/asset managers/RIAs, regulation (DORA, MiFID II, FiDA), compliance/reporting/portfolio analytics, AI in regulated financial workflows, enterprise data integration, legacy modernization, custodian connectivity.

### Likely Unrelated Themes
General consumer tech, macro news with no wealth-tech angle, entertainment, local news with no sector bearing.

## Running Locally

```bash
# Development
uvicorn app.main:app --reload

# Production
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Evaluation

Two complementary suites.

```bash
python test_contract.py   # offline: no network, no model calls, ~1s
python eval.py            # live: fetches 16 real URLs and calls the model
```

`test_contract.py` proves the guarantees in **The output contract** actually hold, using
stubbed model responses: off-vocabulary topics are discarded, `UNRELATED` returns no topics,
confidence is clamped, malformed output is retried exactly once and then fails as
`classification_failed`, an exhausted budget skips the call rather than issuing a doomed one,
and topics survive persistence with commas intact. 11 tests, all passing. These are the
claims most worth making executable, since they are the ones the README asserts.

`eval.py` measures classification quality against live articles. Because it depends on the
open web, a case that starts failing on retrieval is a fact about a publisher rather than a
regression — the report prints the retrieval path per case so the two can be told apart.

16 real URLs covering the taxonomy and every failure mode. Latest run: **15/16 matched
expectation**, with all six failure modes classified correctly.

| Category | Cases | Matched |
|---|---|---|
| Relevant + positive | 3 | 3 |
| Relevant + negative | 4 | 3 |
| Unrelated | 4 | 4 |
| Failure modes | 5 | 5 |

The single divergence is case 7, a compliance-cost piece published by a compliance
vendor: it was labelled `GOOD_NEWS` against an expectation of `BAD_NEWS`. Relevance
was decided correctly; the sentiment call is genuinely contested, since the article
frames compliance burden as a market opportunity. It is kept in the set rather than
tuned away, because it illustrates the boundary the taxonomy does not resolve on its
own.

Two of the unrelated cases are deliberate relevance traps — general consumer AI and
general macro business news. Both are subjects that a keyword-driven classifier would
pull in, and both were correctly rejected as immaterial.

## Known Limitations

1. **Some publishers cannot be retrieved at all.** Reuters and Investopedia return
   401/402 to any non-browser client, from residential and datacenter IPs alike, and
   the reader fallback is blocked by them too. This is a property of those publishers,
   not a bug to fix; the service reports it as a structured `http_error` rather than
   pretending to have read the page. Defeating it would require a headless browser or
   a commercial scraping proxy, which is out of scope here.
2. **The reader fallback is rate-limited when unauthenticated.** Requests are limited
   per source IP, and a shared PaaS egress IP exhausts that quickly. Set `JINA_API_KEY`
   in deployment to get a dedicated quota.
3. **Ephemeral SQLite**: resets on redeploy/restart on Render free tier.
4. **Confidence is not calibrated**: it is the model's own stated number, returned as the
   brief's example response specifies, but it should not be read as a probability. It is
   usable as a weak ordering signal and nothing stronger.
5. **Topics can come back empty on relevant articles**: the vocabulary is closed, so a
   genuinely relevant article about an off-list theme returns `relevance_topics: []`. The
   label and reasoning still carry the finding, and the discarded topic is logged. This is
   the deliberate cost of a vocabulary downstream consumers can match on.
6. **No idempotency**: each request is classified independently, even for a repeat URL.
7. **No auth**: open API, per the case brief.
8. **Cold starts**: Render free tier sleeps an idle service; the first request can take
   ~30s — which can exceed the 45s budget on a cold start plus a slow publisher.

## File Structure

```
.
├── app/
│   ├── main.py          # FastAPI endpoints, request budget, error mapping
│   ├── fetcher.py       # Two-stage retrieval, SSRF checks, challenge detection
│   ├── classifier.py    # LLM call + output contract enforcement
│   └── db.py            # SQLite persistence
├── eval.py              # Evaluation suite (live URLs)
├── test_contract.py     # Contract tests (offline, no network or model calls)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Deployment on Render

1. Push to GitHub
2. Create Web Service on Render, connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Health check path: `/health`
6. Env: `GROQ_API_KEY=...` (required), `JINA_API_KEY=...` (optional but recommended —
   without it the reader fallback shares an unauthenticated per-IP rate limit)

## Design Notes

Per Performativ's AI philosophy, this is a **governed component**: bounded decisions, clear scope, structured outputs, human-readable reasoning. The LLM only handles classification; deterministic validation and error handling are enforced throughout.

**Reasoning**: capped at 60 words. Short, legible explanations over verbosity.

### Why Option A (classical pipeline) over Option B (LLM with web search)

The brief offers both and asks for a deliberate choice. Retrieval turned out to be the hard
part of this problem, and Option A is what makes retrieval failures *legible*.

A web-enabled LLM can still encounter publisher authentication, bot protection, or challenge
pages — those obstacles belong to the publisher, not to any particular architecture. The
difference is what the service can say afterwards. Because Option A performs retrieval
explicitly, it can distinguish "the publisher refused us" (`http_error`) from "the page was a
bot challenge" (`extraction_failed`) from "the model could not classify what we read"
(`classification_failed`). Fold retrieval into the model call and those collapse into one
indistinguishable "no useful answer".

That distinction is the entire error taxonomy above, and it is what lets a caller tell a
problem with their URL apart from a problem with our service.

There is a correctness argument too, not just an observability one. A challenge page returns
HTTP 200 with plausible-looking text, so it can be mistaken for article content and lead to a
confident classification of something that was never an article. Owning retrieval is what
creates the opportunity to detect and reject that case before the model ever sees it.

### Failure philosophy

The service never guesses. When it cannot produce a grounded answer it says so, with a code
naming the stage that failed:

```
no answer  →  explicit, structured failure          (what this service does)
no answer  →  plausible-looking guess               (what it deliberately avoids)
```

Concretely, there is **no rules-based fallback classifier** for when the LLM is unavailable.
Adding one is tempting — it superficially satisfies the brief's "deterministic fallbacks"
bonus. But a keyword-derived `GOOD_NEWS` firing into a Slack alert during a provider outage is
worse governance than a visible `llm_unavailable`: it is indistinguishable from a real
classification precisely when it is least trustworthy. A bounded component that reports its
own unavailability is more useful than one that degrades silently.

The deterministic work happens *before* the model call, where it can prevent bad input rather
than fabricate output: SSRF and scheme checks, the content-type gate, the 5MB cap, bot-challenge
detection, and the minimum-text threshold.

### Why the request budget is shared, not per-stage

Timeouts compose badly when each stage owns an independent one. The original pipeline could
spend 15s on a direct fetch, 25s on the fallback, then two LLM attempts at the SDK's default
60s read timeout — about 160s worst case, long after any caller had given up.

Stage timeouts are now *tuning*; the 45s `TOTAL_REQUEST_BUDGET` is the *contract*. Retrieval
receives a budget both of its stages draw from, so a slow direct fetch leaves the fallback
correspondingly less time. Classification receives whatever retrieval left, and its retry
receives whatever the first attempt left — if too little remains for a call to plausibly
return, it is skipped rather than issued and cancelled. An `asyncio.wait_for` around the whole
pipeline, persistence included, is the outer guarantee.

In practice a stage timeout fires first: a hanging fetch returns `fetch_failed` at ~10s rather
than `request_timeout` at 45s. The global budget is the backstop for anything that slips past
them, and is covered by a contract test for that reason.

### Why retrieval is two-stage

The first working version fetched pages directly and failed on a large fraction of real
news URLs. Diagnosis showed three distinct causes that had been collapsed into one
symptom:

- publishers that hard-block automated clients from any IP (Reuters, Investopedia);
- publishers that block a direct fetch but are readable through a reader service
  (Wikipedia, Finextra);
- publishers that return HTTP 200 with a cookie wall or bot challenge instead of the
  article, which naive extraction happily turns into text.

No single retrieval strategy handles all three, so retrieval tries a direct fetch first
and falls back to a hosted reader when the direct attempt is blocked, challenged, or
returns implausibly little text. The third case is why the fallback triggers on thin
output and not only on HTTP errors: a 200-with-challenge would otherwise be classified
as though it were an article. Challenge interstitials are detected and rejected as
`extraction_failed` rather than sent to the model.

Using a hosted extraction service keeps the input contract intact — the endpoint still
takes an article URL and nothing else. A news search/aggregation API was considered and
rejected: given a URL it cannot reliably return that article, so it would quietly change
the contract from "classify this article" to "classify something like it".

### Why the classifier forces JSON mode

`gpt-oss` is a reasoning model, and its reasoning tokens are charged against
`max_tokens`. With a budget sized only for the answer, reasoning consumed the entire
allowance and the model returned an empty generation, which JSON mode then rejected —
surfacing as an opaque provider 400. The budget is now well above the answer size with
reasoning effort held low. Output is parsed and validated in one place, malformed output
is retried exactly once, and a provider-side JSON validation failure is treated as a bad
generation to retry rather than an outage.
