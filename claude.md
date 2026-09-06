# Performativ News Classification Agent — Project Context

## What this is

A take-home case study for Performativ, a platform/OS for wealth managers (private
banks, family offices, asset managers, RIAs). The deliverable is a small, deployed
backend service — **not a chatbot, not a manual analyst tool** — that classifies a
news article by URL as relevant/irrelevant to Performativ's business, and if
relevant, whether it's net positive or net negative.

Performativ's stated AI philosophy (from the case brief): AI agents should be
**governed components** — bounded decisions, clear scope, structured outputs,
human-readable reasoning. "Quiet, reliable, purposeful." This should shape the
architecture: deterministic validation and error handling wrapped tightly around
a single, well-scoped probabilistic step (the classification itself), not an
open-ended agent.

## Task

`POST /classify` with a news article URL. The service must:
1. Fetch and parse the article content
2. Classify it as one of three labels
3. Return a short reasoning explanation + structured metadata

## Classification taxonomy

- **GOOD_NEWS** — relevant to Performativ's business AND net positive
- **BAD_NEWS** — relevant to Performativ's business AND net negative
- **UNRELATED** — not materially relevant (label regardless of sentiment)

**Critical ordering**: relevance is decided first, independent of sentiment.
An article about new financial regulation is not automatically bad, and an
article about "growth" or "AI" is not automatically good or automatically
relevant. Only once something is judged relevant does sentiment (positive vs.
negative for Performativ specifically) get evaluated.

**Likely relevant themes**: wealth management software, portfolio management
systems, private banks / asset managers / family offices / RIAs, regulation
(DORA, MiFID II, FiDA), compliance/reporting/portfolio analytics, AI in
regulated financial workflows, enterprise data integration / legacy
modernization, custodian connectivity and orchestration.

**Likely unrelated themes**: general consumer tech with no financial-workflow
angle, macro news with no wealth-tech impact, local news with no sector
bearing, entertainment/sports/lifestyle content.

The case brief explicitly says: *"You are not expected to be perfect. We care
about the quality of your reasoning and how you encode it."* — favor legible,
well-justified design decisions over squeezing out accuracy.

## Architecture

```
POST /classify
      │
      ▼
URL validation + SSRF checks
      │
      ▼
HTTP fetch (httpx)
      │
      ├── failure ──► structured error (fetch_failed / http_error / timeout)
      ▼
Article extraction (trafilatura)
      │
      ├── failure ──► structured error (unsupported_content_type / extraction_failed)
      ▼
Normalized article (title + body text)
      │
      ▼
LLM classifier (Groq, open-weight model)
      │
      ├── malformed output ──► retry once ──► still bad ──► structured error (classification_failed)
      ▼
Pydantic validation (label enum, confidence range, reasoning length)
      │
      ▼
SQLite persistence
      │
      ▼
JSON response
```

Framing for the README: **"Deterministic ingestion and validation around a
probabilistic classifier."**

## Stack (decided — do not re-litigate without a good reason)

- **Framework**: FastAPI
- **HTTP client**: httpx only (not requests — avoid mixing)
- **Extraction**: trafilatura
- **LLM**: Groq API, an open-weight instruct model (e.g. `llama-3.1-8b-instant`
  or `llama-3.3-70b-versatile`). Chosen over self-hosted Ollama because free-tier
  PaaS (Render/Railway/Fly free allowances) doesn't have enough RAM to run even
  a small local model reliably — calling a free hosted inference API for an
  open-weight model keeps the "open-source model" spirit without a hosting
  headache.
- **Persistence**: SQLite (single file). Fine that it's ephemeral on Render's
  free tier (resets on redeploy/restart) — note this as a known limitation in
  the README, don't try to solve it.
- **Deployment**: Render, free tier, native Python (no Docker needed —
  simpler). Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.
- **Auth**: none — kept open per the case brief.

## Endpoints

- `GET /health` — liveness check
- `POST /classify` — body `{"url": "..."}` → returns classification JSON or a
  structured error
- `GET /latest?limit=N` — last N classifications from SQLite, `1 <= limit <= 100`,
  default e.g. 10, `ORDER BY processed_at DESC`

### Example success response

```json
{
  "url": "https://example.com/...",
  "label": "BAD_NEWS",
  "confidence": 0.82,
  "reasoning": "New regulatory requirements increase compliance overhead for wealth platforms like Performativ.",
  "relevance_topics": ["regulation", "compliance"],
  "processed_at": "2026-03-16T10:00:00Z"
}
```

Reasoning should be **short**: 1-2 sentences, roughly 40-60 words max. Don't
let the model produce a paragraph — the brief asks for "a short reasoning
explanation."

## Error taxonomy

Don't collapse all failures into one generic error — distinguish where in the
pipeline things broke:

| Failure | error code | HTTP status |
|---|---|---|
| Invalid/malformed URL | `invalid_url` | 422 |
| SSRF-blocked (localhost, private/reserved IP, non-http(s) scheme) | `blocked_url` | 422 |
| Target returns 404/403/5xx | `http_error` | 502 |
| Timeout / DNS / connection failure | `fetch_failed` | 504 |
| Unsupported content-type (PDF, image, etc.) | `unsupported_content_type` | 415 |
| HTML fetched but article text unextractable (e.g. paywall) | `extraction_failed` | 422 |
| LLM unavailable | `llm_unavailable` | 502 |
| LLM output invalid after one retry | `classification_failed` | 502 |

Example error response:
```json
{"error": "extraction_failed", "detail": "Could not identify meaningful article text"}
```

## Security

The API accepts an arbitrary caller-supplied URL, so before fetching:
- Only `http`/`https` schemes allowed
- Reject `localhost` / loopback addresses
- Resolve DNS and reject private/link-local/reserved IP ranges (this also
  covers cloud metadata endpoints like `169.254.169.254`)
- Cap response size while streaming — reject anything above ~5MB (an article
  page has no business being huge; this is also DoS protection)

## Confidence field

Confidence is the model's own stated confidence in the classification, based
solely on the extracted article content. It is not a calibrated probability —
don't over-trust it, and don't let the model use it to compensate for missing
information. Validate `0 <= confidence <= 1` and clamp if the model returns
something out of range.

## Explicit non-goals (don't build these — time-boxed to 3-5 hours)

- No idempotency/caching — classify each request independently, even for a
  duplicate URL
- No auth
- No chat UI — this is a headless backend service
- No comprehensive test suite — a small manual eval set is enough
- No production-grade persistence — SQLite file is fine

## Evaluation

Build a small `eval.py` / fixture with ~10-15 real article URLs covering:
- Clearly relevant + positive
- Clearly relevant + negative
- Clearly unrelated
- Regulation-specific example
- AI-in-financial-workflows example
- General consumer AI (should be unrelated)
- General macro news (should be unrelated)
- A paywalled/unextractable page (failure mode)
- A 404 URL (failure mode)
- A non-HTML URL, e.g. a PDF (failure mode)

Script should print expected vs. actual label per case. No need for high
accuracy — the brief explicitly doesn't expect perfection. Document results
in the README.

## Submission requirements
- README should cover: architecture and why, failure-handling philosophy, eval
  results, known limitations (ephemeral disk, free-tier cold starts).