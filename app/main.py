"""Performativ News Classification API."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import os
import logging
from datetime import datetime
from dotenv import load_dotenv

from .fetcher import fetch_and_extract, FetchError as FetcherError
from .classifier import classify_article
from .db import insert_classification, get_latest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
app = FastAPI(title="Performativ News Classifier")


class ClassifyRequest(BaseModel):
    url: str


class ClassificationResponse(BaseModel):
    url: str
    label: str = Field(..., description="GOOD_NEWS, BAD_NEWS, or UNRELATED")
    reasoning: str
    relevance_topics: list[str] = Field(default_factory=list)
    processed_at: datetime


class HealthResponse(BaseModel):
    status: str


@app.get("/health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/classify")
async def classify(req: ClassifyRequest) -> ClassificationResponse:
    """Classify a news article by URL."""
    try:
        print(f"DEBUG: Classifying {req.url}")
        logger.info(f"Classifying: {req.url}")

        # Fetch and extract
        article = await fetch_and_extract(req.url)
        if isinstance(article, FetcherError):
            logger.warning(f"Fetch error: {article.error}")
            status_map = {
                "invalid_url": 422,
                "blocked_url": 422,
                "http_error": 502,
                "fetch_failed": 504,
                "unsupported_content_type": 415,
                "extraction_failed": 422,
            }
            raise HTTPException(status_code=status_map.get(article.error, 400), detail=article.model_dump())

        print(f"DEBUG: Extracted - title={len(article.title)} chars, text={len(article.text)} chars")
        logger.info(f"Extracted article, classifying...")

        # Classify
        result = await classify_article(article.title, article.text)

        if "error" in result:
            logger.error(f"Classification error: {result}")
            raise HTTPException(
                status_code=502,
                detail={"error": result.get("error"), "detail": result.get("detail")}
            )

        # Validate output
        label = result.get("label", "").upper()
        if label not in ("GOOD_NEWS", "BAD_NEWS", "UNRELATED"):
            logger.error(f"Invalid label: {label}")
            raise HTTPException(status_code=502, detail={"error": "classification_failed", "detail": "Invalid label"})

        reasoning = result.get("reasoning", "")[:200].strip() or "No reasoning provided"
        topics = [t for t in result.get("relevance_topics", []) if isinstance(t, str)]

        response = ClassificationResponse(
            url=req.url,
            label=label,
            reasoning=reasoning,
            relevance_topics=topics,
            processed_at=datetime.utcnow(),
        )

        # Persist (keep confidence in DB for future analysis, just don't expose it)
        try:
            confidence = float(result.get("confidence", 0.5))
            insert_classification(req.url, label, confidence, reasoning, topics)
        except Exception as e:
            logger.warning(f"Failed to persist: {e}")

        logger.info(f"Classification complete: {label}")
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail={"error": "internal_error", "detail": str(e)})


@app.get("/latest")
async def latest(limit: int = 10) -> list[dict]:
    """Get latest N classifications. Limit: 1-100, default 10."""
    return get_latest(max(1, min(100, limit)))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
