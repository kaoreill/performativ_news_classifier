"""SQLite persistence layer.

A connection is opened per call and closed by `closing()` rather than held
open. The write path is a single statement on a request that has already
spent seconds on network I/O, so pooling would buy nothing, and closing on
the way out means a failing statement cannot leak the handle. Reading
DB_PATH per call rather than binding it at import is what lets the contract
tests point this module at a temporary file.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("classifications.db")


def to_iso_z(moment: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with a Z suffix.

    This is how the API serializes `processed_at` on /classify, so storing the
    same form keeps a single event reading identically from both endpoints.
    A naive datetime is assumed to be UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def init_db():
    """Create schema if not exists."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS classifications (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                label TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning TEXT NOT NULL,
                relevance_topics TEXT,
                processed_at TEXT NOT NULL
            )
        """)
        conn.commit()


def insert_classification(
    url: str,
    label: str,
    confidence: float,
    reasoning: str,
    topics: list[str],
    processed_at: datetime,
) -> None:
    """Insert a classification result.

    `processed_at` is supplied by the caller rather than generated here, so the
    persisted record carries the same instant the response reported.
    """
    # JSON, not a comma-joined string: a topic containing a comma would
    # otherwise split into two on the way back out.
    topics_str = json.dumps(topics)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """INSERT INTO classifications
               (url, label, confidence, reasoning, relevance_topics, processed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (url, label, confidence, reasoning, topics_str, to_iso_z(processed_at)),
        )
        conn.commit()


def get_latest(limit: int = 10) -> list[dict]:
    """Get latest N classifications."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT url, label, confidence, reasoning, relevance_topics, processed_at
               FROM classifications ORDER BY processed_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    return [
        {
            "url": r["url"],
            "label": r["label"],
            "confidence": r["confidence"],
            "reasoning": r["reasoning"],
            "relevance_topics": _load_topics(r["relevance_topics"]),
            "processed_at": r["processed_at"],
        }
        for r in rows
    ]


def _load_topics(stored: str) -> list[str]:
    """Read a topics column, tolerating rows written before JSON storage."""
    if not stored:
        return []
    try:
        loaded = json.loads(stored)
    except (json.JSONDecodeError, TypeError):
        return [t for t in stored.split(",") if t]
    return loaded if isinstance(loaded, list) else []


# Initialize on import
init_db()
