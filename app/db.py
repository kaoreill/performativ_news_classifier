"""SQLite persistence layer."""

import sqlite3
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
    conn = sqlite3.connect(DB_PATH)
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
    conn.close()


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
    conn = sqlite3.connect(DB_PATH)
    topics_str = ",".join(topics) if topics else ""
    conn.execute(
        """INSERT INTO classifications
           (url, label, confidence, reasoning, relevance_topics, processed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (url, label, confidence, reasoning, topics_str, to_iso_z(processed_at)),
    )
    conn.commit()
    conn.close()


def get_latest(limit: int = 10) -> list[dict]:
    """Get latest N classifications."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT url, label, confidence, reasoning, relevance_topics, processed_at
           FROM classifications ORDER BY processed_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()

    return [
        {
            "url": r["url"],
            "label": r["label"],
            "reasoning": r["reasoning"],
            "relevance_topics": r["relevance_topics"].split(",") if r["relevance_topics"] else [],
            "processed_at": r["processed_at"],
        }
        for r in rows
    ]


# Initialize on import
init_db()
