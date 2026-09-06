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
| `unsupported_content_type` | 415 | Payload cannot be treated as an HTML article — a PDF or image (from headers), or structured data such as a JSON API response (from payload shape) |
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

17 real URLs covering the taxonomy and every failure mode, run with `JINA_API_KEY` set so
it exercises the same retrieval path as production. Latest run: **17/17 matched
expectation**, with all five failure modes classified correctly — though three cases are
only ~80% stable across repeated runs, so 16/17 is an equally likely result. Seven of these labels were
corrected after an explicit business-impact rubric disagreed with them and the disagreement
turned out to be right; see **Encoding business impact** for the before/after and the
stability caveat.

| Category | Cases | Matched |
|---|---|---|
| Relevant + positive (regulatory demand drivers) | 4 | 4 |
| Relevant + negative (competitor gains ground) | 3 | 3 |
| Unrelated | 4 | 4 |
| Index page (not an article) | 1 | 1 |
| Failure modes | 5 | 5 |

Failure-mode cases are named for the property they demonstrate, not for a publisher. An
earlier case asserted that Reuters "hard-blocks automated clients"; with an authenticated
reader it now retrieves, and Investopedia serves a direct fetch again too. Asserting another
company's anti-bot posture made the suite fragile and tested nothing about this service, so
those assertions were removed.

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

1. **Retrieval success is not the same as usable article text.** Three things need
   separating, and an earlier version of this README ran them together:

   ```
   unauthenticated retrieval  ≠  authenticated retrieval  ≠  usable article content
   ```

   Some publishers refuse an unauthenticated direct fetch (Reuters and Investopedia both
   returned 401/402 at one point, from residential and datacenter IPs alike). With
   `JINA_API_KEY` set, the reader fallback often retrieves those same URLs — but an
   HTTP 200 does not imply an article was obtained. `reuters.com/technology/` returns 200
   and yields mostly navigation, which the classifier then correctly reports as
   `UNRELATED`.

   Anti-bot posture is the publisher's to change at any time: both of the publishers named
   above served a direct fetch when this was last measured. Behaviour therefore depends on
   whether `JINA_API_KEY` is set, and no test in this repo asserts that a particular
   publisher blocks us.
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

- publishers that refused a direct fetch outright at the time of measurement, from
  residential and datacenter IPs alike (Reuters and Investopedia then returned 401/402;
  both have since served a direct fetch, which is the point — this is not a stable
  property);
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

### Encoding business impact (and what measuring it revealed)

The taxonomy gave relevance a full paragraph of guidance and gave sentiment none: "net
positive" was never defined, so the model filled the gap with the only signal available —
the author's tone. Promotional pieces read positive, cost-anxious pieces read negative, and
the same underlying development landed on opposite labels depending on who wrote it up.

The prompt now works through explicit steps: establish relevance, state the *underlying
development* separately from how it is presented, enumerate positive and negative mechanisms
by which it could affect Performativ, then decide which dominates. Tone is excluded
explicitly — factual claims in an article are evidence, tone is not.

Measuring the change mattered more than the change itself:

| | Score against the labels of the time |
|---|---|
| Baseline prompt (tone as implicit proxy) | 16/17 |
| Business-impact rubric, original labels | **12/17** |
| Rubric + tie-break rules, corrected labels | 17/17 |

The middle row is the useful one. Introducing the rubric moved five cases, and inspecting
them showed the *labels* were wrong, not the classifier:

- **Four regulation/compliance cases** were labelled `BAD_NEWS` on the unstated assumption
  that regulatory burden on customers is bad for Performativ. But Performativ sells
  compliance and reporting tooling, so that burden is a demand driver. The project brief
  warns against exactly this assumption: *"An article about new financial regulation is not
  automatically bad."* Corrected to `GOOD_NEWS`.
- **Three competitor cases** were labelled `GOOD_NEWS` because wealth-tech activity sounds
  good for wealth tech. A rival launching a product or raising $65M strengthens a competitor;
  category validation is real but diffuse, a better-funded rival is concrete. Corrected to
  `BAD_NEWS`.

The rubric also *exposed* an ambiguity rather than creating one. Competitor news oscillated
run-to-run, because two mechanisms both applied — "increases investment in Performativ's
target markets" and "strengthens a competing provider" — and nothing said which wins. That
instability was always latent; the old prompt merely hid it, resolving such stories as
positive because launch announcements read upbeat. Both cases now have a stated tie-break
rule.

#### Residual instability is reported, not hidden

Labels are sampled at temperature 0.3, so they are not deterministic. Five runs per case:

| Case | Outcome | Confidence |
|---|---|---|
| Competitor launch (WealthAi) | BAD ×5 | 0.70 – 0.80 |
| Competitor funding (Wealth.com) | BAD ×5 | 0.70 – 0.93 |
| Compliance costs (ncontracts) | GOOD ×5 | 0.80 |
| Compliance spend (fourthline) | GOOD ×5 | 0.78 – 0.80 |
| Advisor-transition tooling (Dispatch) | BAD ×4, GOOD ×1 | 0.70 – 0.90 |
| Hidden compliance costs (fefundinfo) | GOOD ×4, BAD ×1 | 0.65 – 0.85 |
| Tighter SEC regulation (rsmus) | GOOD ×4, BAD ×1 | 0.65 – 0.80 |

The three unstable cases carry the lowest confidences in the set, which is the intended
behaviour: where the rubric genuinely does not resolve a case, the classifier is meant to
pick a side *and* signal that it is close. A repeat eval run may therefore score 16/17
rather than 17/17. The 100% figure reflects corrected expectations on a 17-case set — it is
evidence that the labels and the reasoning now agree, not a claim of general accuracy.

### Why there is no article-vs-index detector

Production testing found the service classifying a section front (`reuters.com/technology/`)
from its navigation text. The obvious fix is a gate that rejects anything that is not an
article. Two candidate signals were measured across real articles and real index pages:

| Signal | Articles | Index pages | Separates? |
|---|---|---|---|
| Sentences per 1k chars | 2.8 – 4.9 | 0.4 – 2.6 | No — BBC Sport 2.6 vs fintech.global 2.8 |
| Anchor tags per 1k chars | 22.5 – 54.1 | 23.6 – 54.0 | No — ranges overlap entirely |

Neither separates the two classes. A threshold placed anywhere in those overlapping ranges
would reject real articles, and rejecting a real article is a worse outcome than processing
an index page.

So the system does not currently attempt to reliably distinguish article pages from
section/index pages; such pages are processed when meaningful text is available. This is a
known limitation rather than a solved problem — a section page could in principle carry
enough relevant content to merit classification, and equally a nav-only page produces a
judgement made on weak input.

Recording the negative result is the honest option. Adding progressively more arbitrary
thresholds until something appeared to work would have produced a detector that looked
principled and was not.

### Why machine data is rejected by shape, not by header

The direct path rejects non-HTML resources from the `Content-Type` response header. The
reader path cannot: it normalizes every source into text and does not report the origin's
content type, so a JSON API response arrives looking like prose. This was a real defect —
a slow JSON endpoint was retrieved by the fallback, classified, and returned `UNRELATED`
with confidence 1.0 on reasoning that described "a technical HTTP request dump".

Where deterministic metadata exists it is used first: the reader is queried in JSON mode,
which returns the *origin's* HTTP status, so a reader-rendered 404 is reported as
`http_error` instead of passing as a successful retrieval. Content shape is the backstop for
what the metadata does not cover.

Machine-generated payloads exhibited a distinct structural signature in our test cases —
JSON key/value pairs and a high share of structural punctuation — which is used only as a
conservative backstop, not as a general claim that prose and data are always separable. Both
signals must fire together. Either alone would misfire on exactly the articles this service
exists to find: enterprise data integration, legacy modernization and custodian connectivity
pieces routinely quote JSON and config. A contract test pins that case, asserting that an
article quoting a holdings payload trips the key/value signal, does *not* trip the structural
one, and is therefore accepted.

`unsupported_content_type` accordingly means **the retrieved payload cannot be treated as an
HTML article** — determined from HTTP metadata on the direct path, and from payload shape on
the reader path. The reader path infers where the direct path observes; the taxonomy is kept
to one error rather than two because the caller's remedy is identical either way.

### Why the classifier forces JSON mode

`gpt-oss` is a reasoning model, and its reasoning tokens are charged against
`max_tokens`. With a budget sized only for the answer, reasoning consumed the entire
allowance and the model returned an empty generation, which JSON mode then rejected —
surfacing as an opaque provider 400. The budget is now well above the answer size with
reasoning effort held low. Output is parsed and validated in one place, malformed output
is retried exactly once, and a provider-side JSON validation failure is treated as a bad
generation to retry rather than an outage.
