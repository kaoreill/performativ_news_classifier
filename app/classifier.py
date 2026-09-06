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
from typing import Optional

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

VALID_LABELS = ("GOOD_NEWS", "BAD_NEWS", "UNRELATED")

SYSTEM_PROMPT = """You are a news classifier for Performativ, a platform/OS for wealth managers (private banks, family offices, asset managers, RIAs).

Classify articles into exactly one of three labels:
- GOOD_NEWS: Materially relevant to Performativ's business AND net positive
- BAD_NEWS: Materially relevant to Performativ's business AND net negative
- UNRELATED: Not materially relevant to Performativ's business (regardless of sentiment)

CRITICAL: Decide RELEVANCE FIRST, independently. Then evaluate sentiment only if relevant.

RELEVANT THEMES: Wealth management software, portfolio management systems, private banks/asset managers/RIAs, regulation (DORA/MiFID II/FiDA), compliance/reporting/portfolio analytics, AI in regulated financial workflows, enterprise data integration, legacy system modernization, custodian connectivity.

NOT RELEVANT: General consumer tech, macro news with no wealth-tech impact, entertainment, local news with no sector bearing.

Base your judgement only on the article text provided. Do not use outside knowledge to fill gaps, and do not raise confidence to compensate for missing information.

Respond with a single JSON object and nothing else:
{
  "label": "GOOD_NEWS" | "BAD_NEWS" | "UNRELATED",
  "confidence": 0.0-1.0,
  "reasoning": "1-2 sentences (40-60 words max). Explain the relevance decision first, then sentiment if relevant.",
  "relevance_topics": ["topic1", "topic2"]
}"""


def _parse(content: str) -> Optional[dict]:
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

    topics = [t.strip() for t in data.get("relevance_topics", []) if isinstance(t, str) and t.strip()]

    return {
        "label": label,
        "confidence": confidence,
        "reasoning": reasoning,
        "relevance_topics": topics,
    }


async def classify_article(title: str, text: str) -> dict:
    """Classify an article. Returns a validated result, or a dict with an `error` key."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return {"error": "llm_unavailable", "detail": "GROQ_API_KEY is not configured"}

    client = AsyncGroq(api_key=api_key)
    user_msg = f"Classify this article:\n\nTITLE: {title}\n\nTEXT:\n{text[:MAX_INPUT_CHARS]}"

    last_reason = "Model returned no parseable classification"

    # One retry: JSON mode makes malformed output rare, but sampling can still
    # produce an unusable object. Two attempts, then fail loudly.
    for attempt in (1, 2):
        try:
            response = await client.chat.completions.create(
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

        logger.warning("Unusable classifier output on attempt %d; retrying" if attempt == 1
                       else "Unusable classifier output on attempt %d; giving up", attempt)

    return {"error": "classification_failed", "detail": last_reason}
