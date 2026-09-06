# Performativ News Classification API

Deterministic backend service classifying financial news articles as **GOOD_NEWS**,
**BAD_NEWS** or **UNRELATED** to Performativ's wealth management platform business.

Why it is built this way — architecture trade-offs, failure philosophy, security decisions and
what the evaluation actually measured — is in **[DESIGN.md](DESIGN.md)**.

## Architecture

```
POST /classify
  ↓
[DETERMINISTIC]  scheme / hostname / SSRF checks
  ↓
[DETERMINISTIC]  retrieval stage 1 — direct fetch (httpx)
  ↓
[DETERMINISTIC]  peer-address check · per-hop redirect validation
  ↓
[DETERMINISTIC]  content-type gate · 5MB cap · bot-challenge detection
  ↓              (on block / challenge / thin text)
[DETERMINISTIC]  retrieval stage 2 — reader fallback (jina.ai Reader)
  ↓
[DETERMINISTIC]  extraction + minimum-text check + machine-payload gate
  ↓
[PROBABILISTIC]  LLM classification (Groq)
  ↓
[DETERMINISTIC]  schema · label enum · topic vocabulary · confidence range
  ↓
[DETERMINISTIC]  SQLite persistence
  ↓
JSON response
```

Deterministic ingestion and validation, wrapped tightly around one well-scoped probabilistic step.

> The LLM **proposes** a classification. Deterministic validation **decides** whether that
> proposal is acceptable as an API result.

Gates above the model reject a request before a token is spent; gates below it reject the
model's answer before it reaches the caller.

### The output contract

Whatever the model returns, these hold for any `200`:

| Rule | Enforced in |
|---|---|
| `label ∈ {GOOD_NEWS, BAD_NEWS, UNRELATED}` | `app/classifier.py` `_parse` |
| `confidence ∈ [0.0, 1.0]` (clamped, never rejected) | `app/classifier.py` `_parse` |
| `relevance_topics ⊆` the closed vocabulary | `app/classifier.py` `_clean_topics` |
| `UNRELATED` ⟹ `relevance_topics == []` | `app/classifier.py` `_clean_topics` |
| `reasoning` is non-empty | `app/classifier.py` `_parse` |
| total request time ≤ 45s | `app/main.py` `TOTAL_REQUEST_BUDGET` |

A violating proposal is retried once, then reported as `classification_failed`.
Off-vocabulary topics are dropped rather than failing the request, but logged at `WARNING` so
contract violations stay visible.

## Stack

- FastAPI · httpx · SQLite · Groq (`openai/gpt-oss-120b`) · Render (free tier, native Python)
- **Extraction**: regex normalization of fetched HTML, falling back to jina.ai Reader in JSON
  mode — which reports the origin's HTTP status, so a reader-rendered error page is not
  mistaken for an article

## Setup

```bash
conda create -n performativ python=3.11 -y && conda activate performativ
pip install -r requirements.txt
cp .env.example .env      # then add GROQ_API_KEY (required)

uvicorn app.main:app --reload                        # development
uvicorn app.main:app --host 0.0.0.0 --port $PORT     # production
```

A plain virtualenv works identically (`python -m venv .venv`); nothing depends on conda.
`JINA_API_KEY` is optional locally, recommended in deployment — see Known Limitations.

## API Endpoints

- **`GET /health`** — liveness. `curl localhost:8000/health` → `{"status": "ok"}`
- **`GET /latest?limit=N`** — last N classifications. `limit`: 1–100, default 10.
- **`POST /classify`** — body `{"url": "https://example.com/article"}`; errors return
  `{"error": "extraction_failed", "detail": "..."}`

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

- **`confidence`** — the model's *own stated* confidence, clamped to `[0, 1]`. Not calibrated:
  a weak ordering signal, not a likelihood.
- **`relevance_topics`** — *canonical Performativ-relevant themes supporting this
  classification*, not "topics mentioned in the article". An article on consumer-crypto
  advertising rules genuinely concerns regulation but supports no relevance finding, so it is
  `UNRELATED` and carries no topics. `UNRELATED` always returns `relevance_topics: []`.

```
wealth_management_software   portfolio_management_systems   private_banks_asset_managers
regulation                   compliance_reporting           portfolio_analytics
ai_in_financial_workflows    data_integration               legacy_modernization
custodian_connectivity
```

A fixed vocabulary makes the field usable by the consumers the brief names — Slack alerts, CRM
enrichment, monitoring. Free-form topics returned the same concept as both `"Wealth management
software"` and `"wealth management"` across two runs, which nothing downstream can match on.

## Error Taxonomy

| Error | HTTP | Meaning |
|-------|------|---------|
| `invalid_url` | 422 | URL parse failure |
| `blocked_url` | 422 | SSRF block — non-http(s) scheme, or a hostname, redirect hop, or established connection landing on a loopback/private/reserved IP |
| `http_error` | 502 | 4xx/5xx from target |
| `fetch_failed` | 504 | Timeout, DNS, or connection failure |
| `request_timeout` | 504 | Pipeline as a whole exceeded the 45s budget |
| `unsupported_content_type` | 415 | Payload cannot be treated as an HTML article — a PDF or image (from headers), or structured data such as a JSON API response (from payload shape) |
| `extraction_failed` | 422 | No meaningful article text found (paywall or bot challenge) |
| `llm_unavailable` | 502 | Classifier provider unreachable, rate-limited, or unconfigured |
| `classification_failed` | 502 | Model output still unusable after one retry |

## Classification Taxonomy

- **GOOD_NEWS** — materially relevant to Performativ's business AND net positive
- **BAD_NEWS** — materially relevant AND net negative
- **UNRELATED** — not materially relevant, regardless of sentiment

**Relevance is decided first, independently of sentiment.** Sentiment is then judged by
business mechanism — what the development does to demand, competition, cost or differentiation
for Performativ — and explicitly *not* by the author's tone. See
[Encoding business impact](DESIGN.md#encoding-business-impact-and-what-measuring-it-revealed).

- **Likely relevant**: wealth management software, portfolio management systems, private
  banks/asset managers/RIAs, regulation (DORA, MiFID II, FiDA), compliance/reporting/portfolio
  analytics, AI in regulated financial workflows, enterprise data integration, legacy
  modernization, custodian connectivity.
- **Likely unrelated**: general consumer tech, macro news with no wealth-tech angle,
  entertainment, local news with no sector bearing.

## Evaluation

```bash
python test_contract.py   # offline: no network, no model calls, ~1s
python eval.py            # live: fetches 17 real URLs and calls the model
```

**`test_contract.py` — 19 tests, all passing.** Makes the output contract executable with
stubbed model responses: off-vocabulary topics discarded; `UNRELATED` returns no topics;
confidence clamped; malformed output retried exactly once then failing as
`classification_failed`; an exhausted budget skipping the call rather than issuing a doomed one;
topics surviving persistence with commas intact; the machine-payload gate accepting a technical
article that quotes JSON while rejecting an actual payload; a connection to a private address
refused whatever DNS reported.

**`eval.py`** measures classification quality against live articles, run with `JINA_API_KEY` set
so it exercises production's retrieval path. Because it depends on the open web, a case failing
on retrieval is a fact about a publisher, not a regression — the report prints the retrieval
path per case so the two can be told apart.

Last full run: **14/17 matched expectation**, all five failure modes behaving correctly. An
earlier run scored 17/17; that figure was replaced rather than kept, because re-running did not
reproduce it (see
[Residual instability](DESIGN.md#residual-instability-is-reported-not-hidden)).

| Category | Cases | Matched |
|---|---|---|
| Relevant + positive (regulatory demand drivers) | 4 | 2 |
| Relevant + negative (competitor gains ground) | 3 | 3 |
| Unrelated | 4 | 4 |
| Index page (not an article) | 1 | 0 |
| Failure modes | 5 | 5 |

The three divergences:

- **Cases 4 and 5** (regulatory demand drivers) returned `BAD_NEWS` against `GOOD_NEWS`. Both are
  close calls the rubric genuinely does not resolve — see
  [Residual instability](DESIGN.md#residual-instability-is-reported-not-hidden).
- **The index-page case**, then pointed at `reuters.com/technology/`, began returning a stable
  HTTP 401. That is the origin-status gate working, but it left the case asserting Reuters' bot
  policy rather than our behaviour, so it was repointed at another retrievable section front.
  Re-expecting `http_error` would have reintroduced the fragility deliberately removed from the
  failure-mode cases, which are named for the property they demonstrate, never for a publisher.
  **The suite has not been re-scored since** — 14/17 excludes the repointed case.

Two of the unrelated cases are deliberate relevance traps — general consumer AI, general macro
business news. Both would be pulled in by a keyword-driven classifier; both were correctly
rejected as immaterial.

## Known Limitations

1. **Retrieval success ≠ usable article text.** Three things need separating, which an earlier
   version of this README ran together:

   ```
   unauthenticated retrieval  ≠  authenticated retrieval  ≠  usable article content
   ```

   With `JINA_API_KEY` set, the reader fallback often retrieves URLs that refuse an
   unauthenticated direct fetch — but HTTP 200 does not imply an article: a section front
   returns 200 and yields mostly navigation, correctly reported as `UNRELATED`. Behaviour
   therefore depends on whether `JINA_API_KEY` is set. Anti-bot posture is the publisher's to
   change at any time, so no test here asserts that a particular publisher blocks us (see
   [Why retrieval is two-stage](DESIGN.md#why-retrieval-is-two-stage)).
2. **The reader fallback is rate-limited when unauthenticated** — per source IP, and a shared
   PaaS egress IP exhausts that quickly. Set `JINA_API_KEY` in deployment for a dedicated quota.
3. **Ephemeral SQLite** — resets on redeploy/restart on Render's free tier.
4. **Confidence is not calibrated** — returned because the brief's example response
   specifies it; a weak ordering signal, nothing stronger.
5. **Topics can come back empty on relevant articles** — the vocabulary is closed, so a relevant
   article on an off-list theme returns `[]`. Label and reasoning still carry the finding, and
   the discarded topic is logged. The deliberate cost of a vocabulary consumers can match on.
6. **No idempotency** — each request is classified independently, even for a repeat URL.
7. **No auth** — open API, per the case brief.
8. **Cold starts** — Render's free tier sleeps an idle service; the first request can take ~30s,
   which can exceed the 45s budget on a cold start plus a slow publisher.
9. **Free-tier model quota** — Groq's free tier caps tokens per day, low enough that one full
   `eval.py` run plus repeat-stability sampling exhausts it. Evaluation runs in batches.

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
├── DESIGN.md            # Architecture and evaluation rationale
└── requirements.txt · .env.example · .gitignore · README.md
```

## Deployment on Render

1. Push to GitHub; create a Web Service and connect the repo
2. Build `pip install -r requirements.txt`; start
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`; health check path `/health`
3. Env: `GROQ_API_KEY` (required), `JINA_API_KEY` (optional but recommended — without it the
   reader fallback shares an unauthenticated per-IP rate limit)

