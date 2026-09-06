"""LLM-based article classification."""
import json, os
from groq import Groq

SYSTEM_PROMPT = """You are a news classifier for Performativ, a platform for wealth managers.
Classify articles into: GOOD_NEWS (relevant + positive), BAD_NEWS (relevant + negative), UNRELATED (not relevant).
Decide RELEVANCE FIRST, independent of sentiment.
Relevant: wealth management software, portfolio management, regulation (DORA/MiFID II/FiDA), compliance, AI in regulated workflows.
Response: {"label": "...", "confidence": 0.0-1.0, "reasoning": "1-2 sentences", "relevance_topics": [...]}"""

async def classify_article(title: str, text: str) -> dict:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"Classify:\nTITLE: {title}\nTEXT: {text[:2000]}"}],
            temperature=0.3,
            max_tokens=300,
        )
        content = response.choices[0].message.content.strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        return json.loads(content[start:end]) if start >= 0 and end > start else json.loads(content)
    except Exception as e:
        return {"error": "classification_failed", "detail": str(e)[:100]}
