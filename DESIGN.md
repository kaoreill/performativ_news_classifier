# Design Notes

Why the service is built the way it is. For endpoints, error codes and evaluation results, see
**[README.md](README.md)**.

Per Performativ's AI philosophy this is a **governed component**: bounded decisions, clear scope,
structured outputs, human-readable reasoning. The LLM only classifies. Reasoning is capped at 60
words — short, legible explanations over verbosity.

### Why Option A (classical pipeline) over Option B (LLM with web search)

Retrieval is the hard part here, and Option A makes its failures *legible*.

- A web-enabled LLM still meets publisher authentication, bot protection and challenge pages —
  obstacles belonging to the publisher, not to any architecture.
- What differs is what the service can say afterwards. Owning retrieval separates "the publisher
  refused us" (`http_error`), "the page was a bot challenge" (`extraction_failed`) and "the model
  could not classify what we read" (`classification_failed`). Folded into the model call, all three
  collapse into one "no useful answer".
- That distinction *is* the [error taxonomy](README.md#error-taxonomy); it lets a caller tell a
  problem with their URL from a problem with our service.
- Correctness, not only observability: a challenge page returns HTTP 200 with plausible-looking
  text, so it can be classified confidently as an article it never was. Owning retrieval is the
  chance to reject it first.

### Failure philosophy

The service never guesses. Without a grounded answer it says so, naming the stage that failed:

```
no answer  →  explicit, structured failure          (what this service does)
no answer  →  plausible-looking guess               (what it deliberately avoids)
```

- **No rules-based fallback classifier** for LLM outages. It is tempting, and superficially
  satisfies the brief's "deterministic fallbacks" bonus. But a keyword-derived `GOOD_NEWS` firing
  into a Slack alert during an outage is worse governance than a visible `llm_unavailable`:
  indistinguishable from a real classification precisely when least trustworthy. A component
  reporting its own unavailability beats one degrading silently.
- Deterministic work happens *before* the model call, preventing bad input rather than fabricating
  output: SSRF, scheme and peer-address checks, per-hop redirect validation, the content-type
  gate, the 5MB cap, bot-challenge detection, the minimum-text threshold.

### Why the URL is validated twice

The service fetches whatever URL a caller sends, so SSRF is the one place a bug is not merely a
wrong label. Two gaps, both found by *testing* the guard rather than reading it, both from one
mistake — validating a *prediction* of the request instead of the request.

- **Pre-flight is not binding on the connection.** `validate_url` resolves the hostname and
  rejects private addresses; the client then resolves it *again* on connect. Two lookups, so
  attacker-controlled DNS can answer differently — public for the check, loopback for the
  connection. Fixed by reading the peer address off the open socket. Demonstrated with a live
  listener on loopback reached via a public hostname resolving to `127.0.0.1`: the connection
  succeeds, and the peer check refuses it.
- **Automatic redirects validated the first URL and nothing after.** An allowed page could hand
  the request to `169.254.169.254` via a `Location` header. Redirects are now followed by hand,
  each hop re-validated, chain capped at 5.
- Neither gap was reachable through the eval set — the point worth recording. Both were found by
  pointing the fetcher at a deliberately hostile target, and both are regression-tested.
- The layers are deliberately redundant and say which fired: `Target resolves to...` is
  pre-flight, `Connection resolved to...` is the socket. The peer check fails *open* when the
  transport reports no address, returning the guarantee to pre-flight rather than downing the
  service on a library change.

### Why the request budget is shared, not per-stage

Timeouts compose badly when each stage owns an independent one. The original pipeline could spend
15s on a direct fetch, 25s on the fallback, then two LLM attempts at the SDK's default 60s read
timeout — ~160s worst case, long after any caller gave up.

- Stage timeouts are *tuning*; the 45s `TOTAL_REQUEST_BUDGET` is the *contract*.
- Retrieval receives a budget both stages draw from, so a slow direct fetch leaves the fallback
  correspondingly less time.
- Classification receives whatever retrieval left; its retry receives whatever the first attempt
  left. Too little to plausibly return means the call is skipped, not issued and cancelled.
- `asyncio.wait_for` around the whole pipeline, persistence included, is the outer guarantee.
- In practice a stage timeout fires first — a hanging fetch returns `fetch_failed` at ~10s rather
  than `request_timeout` at 45s. The global budget is the backstop for anything slipping past
  them, and is covered by a contract test for that reason.

### Why retrieval is two-stage

The first version fetched pages directly and failed on a large fraction of real news URLs. Three
distinct causes had collapsed into one symptom:

- publishers refusing a direct fetch outright at the time of measurement, from residential and
  datacenter IPs alike (Reuters and Investopedia then returned 401/402; both have since served a
  direct fetch, which is the point — this is not a stable property);
- publishers blocking a direct fetch but readable through a reader service (Wikipedia, Finextra);
- publishers returning HTTP 200 with a cookie wall or bot challenge instead of the article, which
  naive extraction happily turns into text.

No single strategy handles all three, so retrieval tries a direct fetch first and falls back to a
hosted reader when that attempt is blocked, challenged, or returns implausibly little text.

- The third case is why the fallback triggers on thin output and not only on HTTP errors: a
  200-with-challenge would otherwise be classified as an article. Challenge interstitials are
  detected and rejected as `extraction_failed` rather than sent to the model.
- A hosted extraction service keeps the input contract intact — the endpoint still takes an
  article URL and nothing else. A news search/aggregation API was considered and rejected: given
  a URL it cannot reliably return *that* article, so it would quietly change the contract from
  "classify this article" to "classify something like it".

### Encoding business impact (and what measuring it revealed)

The taxonomy gave relevance a full paragraph and sentiment none. "Net positive" was undefined, so
the model filled the gap with the only signal available — the author's tone. Promotional pieces
read positive, cost-anxious pieces read negative, and the same development landed on opposite
labels depending on who wrote it up.

The prompt now works through explicit steps: establish relevance; state the *underlying
development* separately from its presentation; enumerate positive and negative mechanisms by which
it could affect Performativ; decide which dominates. Tone is excluded explicitly — factual claims
are evidence, tone is not.

Measuring the change mattered more than the change itself:

| | Score against the labels of the time |
|---|---|
| Baseline prompt (tone as implicit proxy) | 16/17 |
| Business-impact rubric, original labels | **12/17** |
| Rubric + tie-break rules, corrected labels | 17/17 (not reproducible; a later run returned 14/17) |

The middle row is the useful one. The rubric moved five cases, and inspecting them showed the
*labels* were wrong, not the classifier:

- **Four regulation/compliance cases** were labelled `BAD_NEWS` on the unstated assumption that
  regulatory burden on customers is bad for Performativ. But Performativ sells the compliance and
  reporting tooling that burden creates demand for. The brief warns against exactly this: *"An
  article about new financial regulation is not automatically bad."* Corrected to `GOOD_NEWS`.
- **Three competitor cases** were labelled `GOOD_NEWS` because wealth-tech activity sounds good
  for wealth tech. A rival launching a product or raising $65M strengthens a competitor; category
  validation is real but diffuse, a better-funded rival is concrete. Corrected to `BAD_NEWS`.
- The rubric also *exposed* an ambiguity rather than creating one. Competitor news oscillated
  run-to-run because two mechanisms both applied — "increases investment in Performativ's target
  markets" and "strengthens a competing provider" — and nothing said which wins. That instability
  was always latent; the old prompt hid it, resolving such stories as positive because launch
  announcements read upbeat. Both cases now have a stated tie-break rule.

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

The unstable cases carry the lowest confidences in the set — the intended behaviour: where the
rubric does not resolve a case, the classifier picks a side *and* signals it is close.

Re-measured later, fefundinfo is not a 4:1 lean but a coin flip — four clean runs split **2
`GOOD_NEWS` / 2 `BAD_NEWS`**, the two readings citing opposite mechanisms from the same rubric:

> *"...rising compliance burdens and costs, which likely reduce firms' willingness to spend and
> could strain budgets..."* — `BAD_NEWS`, confidence 0.68

> *"...growing regulatory burdens, which creates demand for compliance and data-management
> software services Performativ provides."* — `GOOD_NEWS`, confidence 0.85

- Both are listed mechanisms: *reduces customers' willingness or ability to spend on relevant
  software*, and *increases demand for wealth-management software, compliance, reporting*. The
  article evidences both, and the rubric does not say which dominates when a regulatory burden
  falls on the customer and creates demand at once.
- The expectation stays `GOOD_NEWS` — changing it to match the run would record the sampler's
  mood as a finding. rsmus behaved likewise: `BAD_NEWS` in the eval run, `GOOD_NEWS` at 0.70 on a
  re-run.
- The re-measurement was cut short by the provider's daily token cap, so those are 4 and 1 clean
  samples rather than 5 each — small, and reported as such rather than rounded into a rate.

The direction matters more than the number: **more measurement made the result worse, not better.**
17/17 came from a favourable run of a set whose regulation cluster is bistable; 14/17 is the honest
current figure, and the spread is roughly 14–17 rather than a point estimate. Neither claims general
accuracy. The brief asks for reasoning quality, and a suite reporting its own variance is better
evidence of that than one tuned until it scores full marks.

### Why there is no article-vs-index detector

Production testing found the service classifying a section front from its navigation text. The
obvious fix is a gate rejecting non-articles. Two candidate signals were measured across real
articles and real index pages:

| Signal | Articles | Index pages | Separates? |
|---|---|---|---|
| Sentences per 1k chars | 2.8 – 4.9 | 0.4 – 2.6 | No — BBC Sport 2.6 vs fintech.global 2.8 |
| Anchor tags per 1k chars | 22.5 – 54.1 | 23.6 – 54.0 | No — ranges overlap entirely |

- Neither separates the classes, and a threshold anywhere in those overlapping ranges would reject
  real articles — worse than processing an index page. So the system does not attempt the
  distinction; such pages are processed when meaningful text is available.
- This is a known limitation, not a solved problem: a section page could in principle carry enough
  relevant content to merit classification, and equally a nav-only page produces a judgement made
  on weak input.
- Recording the negative result is the honest option. Adding progressively more arbitrary
  thresholds until something appeared to work would have produced a detector that looked
  principled and was not.

### Why machine data is rejected by shape, not by header

The direct path rejects non-HTML resources from the `Content-Type` header. The reader path cannot:
it normalizes every source into text and does not report the origin's content type, so a JSON API
response arrives looking like prose. A real defect — a slow JSON endpoint was retrieved by the
fallback, classified, and returned `UNRELATED` at confidence 1.0, its reasoning describing "a
technical HTTP request dump".

- **Deterministic metadata is used first.** The reader is queried in JSON mode, returning the
  *origin's* HTTP status, so a reader-rendered 404 is reported as `http_error` rather than passing
  as a successful retrieval. Content shape is the backstop only for what metadata cannot cover.
- **Both signals must fire together.** Machine payloads exhibited a distinct structural signature
  in our test cases — JSON key/value pairs and a high share of structural punctuation — used only
  as a conservative backstop, not as a general claim that prose and data are always separable.
  Either alone would misfire on exactly the articles this service exists to find: enterprise data
  integration, legacy modernization and custodian connectivity pieces routinely quote JSON and
  config. A contract test pins that case: an article quoting a holdings payload trips the
  key/value signal, does *not* trip the structural one, and is accepted.
- `unsupported_content_type` therefore means **the retrieved payload cannot be treated as an HTML
  article** — from HTTP metadata on the direct path, from payload shape on the reader path. The
  reader path infers where the direct path observes; the taxonomy keeps one error rather than two
  because the caller's remedy is identical either way.

### Why the classifier forces JSON mode

`gpt-oss` is a reasoning model, and its reasoning tokens are charged against `max_tokens`.

- With a budget sized only for the answer, reasoning consumed the entire allowance and the model
  returned an empty generation, which JSON mode then rejected — surfacing as an opaque provider
  400. The budget is now well above the answer size with reasoning effort held low.
- Output is parsed and validated in one place, malformed output is retried exactly once, and a
  provider-side JSON validation failure is treated as a bad generation to retry rather than an
  outage.
