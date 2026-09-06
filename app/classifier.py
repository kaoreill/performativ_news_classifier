"""LLM-based article classification."""

import json
import os
from groq import Groq

SYSTEM_PROMPT = """You are a news classifier for Performativ, a platform/OS for wealth managers (private banks, family offices, asset managers, RIAs).

Classify articles into exactly one of three labels:
- GOOD_NEWS: Materially relevant to Performativ's business AND net positive
- BAD_NEWS: Materially relevant to Performativ's business AND net negative
- UNRELATED: Not materially relevant to Performativ's business (regardless of sentiment)

CRITICAL: Decide RELEVANCE FIRST, independently. Then evaluate sentiment only if relevant.

RELEVANT THEMES: Wealth management software, portfolio management systems, private banks/asset managers/RIAs, regulation (DORA/MiFID II/FiDA), compliance/reporting/portfolio analytics, AI in regulated financial workflows, enterprise data integration, legacy system modernization, custodian connectivity.

NOT RELEVANT: General consumer tech, macro news with no wealth-tech impact, entertainment, local news with no sector bearing.

RESPONSE FORMAT (JSON):
{
  "label": "GOOD_NEWS" | "BAD_NEWS" | "UNRELATED",
  "confidence": 0.0-1.0,
  "reasoning": "1-2 sentences (40-60 words max). Explain relevance decision first, then sentiment if relevant.",
  "relevance_topics": ["topic1", "topic2"]
}"""


async def classify_article(title: str, text: str) -> dict:
    """Classify article using Groq. Returns dict with label, confidence, reasoning, topics."""
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    user_msg = f"""Classify this article:

TITLE: {title}

TEXT:
{text[:2000]}"""

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=300,
        )

        content = response.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(content[start:end])
        else:
            return json.loads(content)

    except Exception as e:
        return {"error": "classification_failed", "detail": str(e)[:100]}
