"""Performativ News Classification API."""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import os
from datetime import datetime
from dotenv import load_dotenv
from .fetcher import fetch_and_extract, FetchError as FetcherError
from .classifier import classify_article
from .db import insert_classification, get_latest

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
    article = await fetch_and_extract(req.url)
    if isinstance(article, FetcherError):
        status_map = {"invalid_url": 422, "blocked_url": 422, "http_error": 502, "fetch_failed": 504, "unsupported_content_type": 415, "extraction_failed": 422}
        raise HTTPException(status_code=status_map.get(article.error, 400), detail=article.model_dump())
    result = await classify_article(article.title, article.text)
    if "error" in result:
        raise HTTPException(status_code=502, detail={"error": result.get("error"), "detail": result.get("detail")})
    label = result.get("label", "").upper()
    if label not in ("GOOD_NEWS", "BAD_NEWS", "UNRELATED"):
        raise HTTPException(status_code=502, detail={"error": "classification_failed", "detail": "Invalid label"})
    reasoning = result.get("reasoning", "")[:200].strip() or "No reasoning provided"
    topics = [t for t in result.get("relevance_topics", []) if isinstance(t, str)]
    response = ClassificationResponse(url=req.url, label=label, reasoning=reasoning, relevance_topics=topics, processed_at=datetime.utcnow())
    try:
        confidence = float(result.get("confidence", 0.5))
        insert_classification(req.url, label, confidence, reasoning, topics)
    except Exception:
        pass
    return response

@app.get("/latest")
async def latest(limit: int = 10) -> list[dict]:
    return get_latest(max(1, min(100, limit)))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
