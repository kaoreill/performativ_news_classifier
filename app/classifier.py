"""LLM-based article classification.

This is the single probabilistic step in the pipeline. Everything around it is
deterministic: the request forces JSON-mode output, malformed output is retried
exactly once, and the result is validated and clamped before it leaves this
module. Transport failures (`llm_unavailable`) are kept distinct from the model
returning something unusable (`classification_failed`) so the caller can tell a
provider outage apart from a bad generation.
"""

import json
import logging
import os
import time

from groq import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncGroq,
    RateLimitError,
)

logger = logging.getLogger(__name__)

MODEL = "openai/gpt-oss-120b"
TEMPERATURE = 0.3
# gpt-oss is a reasoning model: its internal reasoning tokens are charged
# against max_tokens. A budget sized only for the JSON answer gets consumed by
# reasoning, leaving an empty generation that JSON mode then rejects. Keep the
# budget well above the answer size and hold reasoning effort low.
MAX_TOKENS = 1024
REASONING_EFFORT = "low"
MAX_INPUT_CHARS = 2000

# The SDK defaults to a 60s read timeout, so two attempts could alone outlast
# any sane request budget. Attempts draw down a shared allowance instead.
CLASSIFY_BUDGET = 15.0
LLM_ATTEMPT_TIMEOUT = 10.0
MIN_ATTEMPT_SECONDS = 2.0

VALID_LABELS = ("GOOD_NEWS", "BAD_NEWS", "UNRELATED")

# A closed vocabulary drawn from the brief's own "Likely Relevant" themes.
#
# `relevance_topics` means: canonical Performativ-relevant business themes that
# support this classification. It does NOT mean "topics detected in the
# article". An article on consumer-crypto advertising rules genuinely concerns
# regulation, but supports no relevance finding here, so it carries no topics.
#
# Free-form topics are unusable downstream: the same concept came back as both
# "Wealth management software" and "wealth management" across two runs. The
# brief positions this service behind Slack alerts and CRM enrichment, and those
# consumers need a value they can match on.
RELEVANCE_TOPICS = (
    "wealth_management_software",
    "portfolio_management_systems",
    "private_banks_asset_managers",
    "regulation",
    "compliance_reporting",
    "portfolio_analytics",
    "ai_in_financial_workflows",
    "data_integration",
    "legacy_modernization",
    "custodian_connectivity",
)

SYSTEM_PROMPT = """You are a news classifier for Performativ, a platform/OS for wealth managers (private banks, family offices, asset managers, RIAs).

Classify articles into exactly one of three labels:
- GOOD_NEWS: Materially relevant to Performativ's business AND net positive
- BAD_NEWS: Materially relevant to Performativ's business AND net negative
- UNRELATED: Not materially relevant to Performativ's business (regardless of sentiment)

Work through these steps in order.

STEP 1 - RELEVANCE. Decide relevance first, independently of any sentiment.
RELEVANT THEMES: Wealth management software, portfolio management systems, private banks/asset managers/RIAs, regulation (DORA/MiFID II/FiDA), compliance/reporting/portfolio analytics, AI in regulated financial workflows, enterprise data integration, legacy system modernization, custodian connectivity.
NOT RELEVANT: General consumer tech, macro news with no wealth-tech impact, entertainment, local news with no sector bearing.
If it is not materially relevant, the label is UNRELATED and you are finished.

STEP 2 - UNDERLYING DEVELOPMENT. State what actually changed: the event, decision or trend being reported, separate from how the author presents it.

STEP 3 - EFFECTS ON PERFORMATIV. Consider both directions before deciding.
Positive mechanisms:
- increases demand for wealth-management software, compliance, reporting, portfolio analytics or integration capabilities
- expands adoption of AI in regulated financial workflows
- increases investment or modernization spending in Performativ's target markets
- creates a market need that Performativ's capabilities are positioned to address
Negative mechanisms:
- materially strengthens a competing platform or provider, including a competitor's product launch, funding round or capability gain
- reduces customers' willingness or ability to spend on relevant software
- creates a substantial new cost, constraint or liability for Performativ itself
- makes a core Performativ capability less valuable or less differentiating
Sector growth on its own is not positive. Trace the mechanism to Performativ specifically.

STEP 4 - NET ASSESSMENT. Decide which effect is more significant on the evidence in the article. Many developments cut both ways: weigh the effects on Performativ specifically rather than treating relevance itself as positive. If neither effect clearly dominates, still choose a label, but lower your confidence and name the trade-off in your reasoning.

Two developments cut both ways often enough to need a stated rule:
- A competitor launching a product or raising funding also signals a growing market. Treat it as net negative: a strengthened rival is concrete, while category validation is diffuse. Only call it positive if the article shows the market expanding in a way Performativ is specifically positioned to capture.
- Regulation that burdens wealth managers also creates demand for the compliance and reporting tooling Performativ sells. Regulation is therefore not automatically negative. Judge it on which effect the article evidences, and lower confidence when both are present.

IMPORTANT: Do not infer positive or negative business impact from the author's tone, sentiment, or promotional framing. Factual claims in the article are evidence; tone is not. Base the classification on the underlying development and its likely effect on Performativ.

Base your judgement only on the article text provided. Do not use outside knowledge to fill gaps, and do not raise confidence to compensate for missing information.

RELEVANCE TOPICS: choose only from this exact list, and only those that support your
relevance finding. Use the exact strings. If the label is UNRELATED, return an empty list.
{topics}

Respond with a single JSON object and nothing else:
{{
  "label": "GOOD_NEWS" | "BAD_NEWS" | "UNRELATED",
  "confidence": 0.0-1.0,
  "reasoning": "1-2 sentences (40-60 words max). Explain the relevance decision first, then sentiment if relevant.",
  "relevance_topics": ["topic_from_the_list"]
}}""".format(topics="\n".join(f"- {t}" for t in RELEVANCE_TOPICS))


def _parse(content: str) -> dict | None:
    """Parse a model response into a validated classification, or None if unusable."""
    if not content:
        return None

    text = content.strip()
    # Tolerate a fenced block or stray prose around the object.
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return None

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    label = str(data.get("label", "")).strip().upper()
    if label not in VALID_LABELS:
        return None

    reasoning = str(data.get("reasoning", "")).strip()
    if not reasoning:
        return None

    # Confidence is the model's own stated confidence, not a calibrated
    # probability. Clamp rather than reject: a bad number should not discard an
    # otherwise sound classification.
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "label": label,
        "confidence": confidence,
        "reasoning": reasoning,
        "relevance_topics": _clean_topics(data.get("relevance_topics", []), label),
    }


def _clean_topics(raw, label: str) -> list[str]:
    """Reduce proposed topics to the closed vocabulary.

    Off-vocabulary topics are discarded, but logged first: a silent drop would
    destroy the evidence that the model broke its contract, which is precisely
    what we would want to know.
    """
    if label == "UNRELATED":
        # "relevance topics" on an article judged not relevant is a
        # contradiction under the definition above, whatever the model proposed.
        return []

    kept, discarded = [], []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        slug = item.strip().lower().replace("-", "_").replace(" ", "_")
        if slug in RELEVANCE_TOPICS:
            if slug not in kept:
                kept.append(slug)
        elif item.strip():
            discarded.append(item.strip())

    if discarded:
        logger.warning("Discarded off-vocabulary relevance topics: %s", discarded)

    return kept


async def classify_article(title: str, text: str, budget: float = CLASSIFY_BUDGET) -> dict:
    """Classify an article. Returns a validated result, or a dict with an `error` key.

    `budget` is the total seconds classification may consume. Attempts draw it
    down, so a slow first call leaves the retry correspondingly less time rather
    than a fresh allowance.
    """
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return {"error": "llm_unavailable", "detail": "GROQ_API_KEY is not configured"}

    started = time.monotonic()
    client = AsyncGroq(api_key=api_key)
    user_msg = f"Classify this article:\n\nTITLE: {title}\n\nTEXT:\n{text[:MAX_INPUT_CHARS]}"

    # This default covers the case where no attempt is ever issued. It is
    # replaced the moment a call goes out, so the reported detail always
    # describes the last thing that actually happened rather than blaming the
    # model for an answer it was never asked to give.
    last_reason = "No budget remained to call the classifier"

    # One retry: JSON mode makes malformed output rare, but sampling can still
    # produce an unusable object. Two attempts, then fail loudly.
    for attempt in (1, 2):
        remaining = budget - (time.monotonic() - started)
        if remaining < MIN_ATTEMPT_SECONDS:
            # Not enough budget left for this call to plausibly return; issuing
            # it would only guarantee a cancelled request.
            logger.warning("Skipping classifier attempt %d: %.1fs budget left", attempt, remaining)
            break

        last_reason = "Model returned no parseable classification"

        try:
            response = await client.with_options(
                timeout=min(LLM_ATTEMPT_TIMEOUT, remaining)
            ).chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                reasoning_effort=REASONING_EFFORT,
                response_format={"type": "json_object"},
            )
        except (APIConnectionError, APITimeoutError, RateLimitError) as e:
            return {"error": "llm_unavailable", "detail": f"{type(e).__name__}: {str(e)[:100]}"}
        except APIStatusError as e:
            # The provider rejects a generation that failed its own JSON
            # validation. That is a bad generation, not an outage, so it is
            # retried like any other unusable output.
            if getattr(e, "status_code", None) == 400 and "json_validate_failed" in str(e):
                last_reason = "Model produced output that failed JSON validation"
                logger.warning("Provider rejected generation as invalid JSON (attempt %d)", attempt)
                continue
            return {"error": "llm_unavailable", "detail": f"Provider returned HTTP {e.status_code}"}
        except Exception as e:
            return {"error": "llm_unavailable", "detail": str(e)[:100]}

        parsed = _parse(response.choices[0].message.content or "")
        if parsed:
            return parsed

        outcome = "retrying" if attempt == 1 else "giving up"
        logger.warning("Unusable classifier output on attempt %d; %s", attempt, outcome)

    return {"error": "classification_failed", "detail": last_reason}
