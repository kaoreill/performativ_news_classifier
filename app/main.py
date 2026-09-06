"""Performativ News Classification API."""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import asyncio
import os
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from .fetcher import fetch_and_extract, FetchError as FetcherError, RETRIEVAL_BUDGET
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
    "request_timeout": 504,
    "llm_unavailable": 502,
    "classification_failed": 502,
}

# The contract for total request time. Stage timeouts inside the pipeline are
# tuning and can be retuned freely; this is the ceiling a caller can rely on,
# and it covers persistence too, not just the network stages.
TOTAL_REQUEST_BUDGET = 45.0
# Left unspent by classification so a result can still be persisted and returned.
PERSIST_RESERVE = 2.0


class ClassifyRequest(BaseModel):
    url: str


class ClassificationResponse(BaseModel):
    url: str
    label: str = Field(..., description="GOOD_NEWS, BAD_NEWS, or UNRELATED")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="The model's own stated confidence. Not a calibrated probability.",
    )
    reasoning: str
    relevance_topics: list[str] = Field(
        default_factory=list,
        description="Canonical Performativ-relevant themes supporting this classification; empty for UNRELATED.",
    )
    processed_at: datetime


class HealthResponse(BaseModel):
    status: str


@app.get("/health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


async def _run_pipeline(url: str) -> ClassificationResponse:
    """The full classify pipeline, sharing one time budget across its stages."""
    started = time.monotonic()
    logger.info("Classifying: %s", url)

    # Retrieve and parse
    article = await fetch_and_extract(url, budget=RETRIEVAL_BUDGET)
    if isinstance(article, FetcherError):
        logger.warning("Retrieval failed (%s): %s", article.error, article.detail)
        raise HTTPException(
            status_code=ERROR_STATUS.get(article.error, 400),
            detail=article.model_dump(),
        )

    logger.info("Retrieved via %s: %d chars", article.source, len(article.text))

    # Classify — the single probabilistic step. It gets whatever retrieval left,
    # less a small reserve so a successful classification can still be persisted
    # and returned inside the budget.
    remaining = TOTAL_REQUEST_BUDGET - (time.monotonic() - started) - PERSIST_RESERVE
    result = await classify_article(article.title, article.text, budget=remaining)
    if "error" in result:
        logger.error("Classification failed (%s): %s", result["error"], result.get("detail"))
        raise HTTPException(
            status_code=ERROR_STATUS.get(result["error"], 502),
            detail={"error": result["error"], "detail": result.get("detail", "")},
        )

    processed_at = datetime.now(timezone.utc)
    response = ClassificationResponse(
        url=url,
        label=result["label"],
        confidence=result["confidence"],
        reasoning=result["reasoning"],
        relevance_topics=result["relevance_topics"],
        processed_at=processed_at,
    )

    try:
        insert_classification(
            url,
            result["label"],
            result["confidence"],
            result["reasoning"],
            result["relevance_topics"],
            processed_at,
        )
    except Exception as e:
        logger.warning("Failed to persist classification: %s", e)

    logger.info("Classified %s as %s (%.1fs)", url, result["label"], time.monotonic() - started)
    return response


@app.post("/classify")
async def classify(req: ClassifyRequest) -> ClassificationResponse:
    """Classify a news article by URL."""
    try:
        return await asyncio.wait_for(_run_pipeline(req.url), timeout=TOTAL_REQUEST_BUDGET)
    except asyncio.TimeoutError:
        # The per-stage timeouts should normally fire first; reaching here means
        # the pipeline as a whole overran, so report it as such rather than
        # blaming whichever stage happened to be running.
        logger.error("Request exceeded %.0fs budget: %s", TOTAL_REQUEST_BUDGET, req.url)
        raise HTTPException(
            status_code=ERROR_STATUS["request_timeout"],
            detail={
                "error": "request_timeout",
                "detail": f"Classification exceeded the {TOTAL_REQUEST_BUDGET:.0f}s budget",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail={"error": "internal_error", "detail": str(e)})


@app.get("/latest")
async def latest(limit: int = 10) -> list[dict]:
    """Get latest N classifications. Limit: 1-100, default 10."""
    return get_latest(max(1, min(100, limit)))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
