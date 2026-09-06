"""Performativ News Classification API."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from .fetcher import fetch_and_extract, FetchError as FetcherError
from .classifier import classify_article
from .db import insert_classification, get_latest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
app = FastAPI(title="Performativ News Classifier")

# Where in the pipeline a failure occurred determines the status code, so that
# callers can distinguish "your URL is bad" from "the target site blocked us"
# from "our classifier is down".
ERROR_STATUS = {
    "invalid_url": 422,
    "blocked_url": 422,
    "unsupported_content_type": 415,
    "extraction_failed": 422,
    "http_error": 502,
    "fetch_failed": 504,
    "llm_unavailable": 502,
    "classification_failed": 502,
}


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
        logger.info("Classifying: %s", req.url)

        # Retrieve and parse
        article = await fetch_and_extract(req.url)
        if isinstance(article, FetcherError):
            logger.warning("Retrieval failed (%s): %s", article.error, article.detail)
            raise HTTPException(
                status_code=ERROR_STATUS.get(article.error, 400),
                detail=article.model_dump(),
            )

        logger.info("Retrieved via %s: %d chars", article.source, len(article.text))

        # Classify — the single probabilistic step
        result = await classify_article(article.title, article.text)
        if "error" in result:
            logger.error("Classification failed (%s): %s", result["error"], result.get("detail"))
            raise HTTPException(
                status_code=ERROR_STATUS.get(result["error"], 502),
                detail={"error": result["error"], "detail": result.get("detail", "")},
            )

        response = ClassificationResponse(
            url=req.url,
            label=result["label"],
            reasoning=result["reasoning"],
            relevance_topics=result["relevance_topics"],
            processed_at=datetime.now(timezone.utc),
        )

        # Persist. Confidence is retained for later analysis but deliberately
        # not exposed in the response — it is not a calibrated probability.
        try:
            insert_classification(
                req.url,
                result["label"],
                result["confidence"],
                result["reasoning"],
                result["relevance_topics"],
            )
        except Exception as e:
            logger.warning("Failed to persist classification: %s", e)

        logger.info("Classified %s as %s", req.url, result["label"])
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
