from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

WINDOW_DAYS = 14


class SeenItem(BaseModel):
    url: str
    title: str
    topic: str
    gist: str  # one-line summary of what was delivered
    date: str  # ISO date (YYYY-MM-DD)


def load_seen(path: str = "seen.json") -> list[SeenItem]:
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [SeenItem(**i) for i in raw]


def format_for_prompt(seen: list[SeenItem]) -> str:
    if not seen:
        return "(nothing covered yet)"
    return "\n".join(f"- [{i.topic}] {i.title} — {i.gist}" for i in seen)


def append_and_prune(
    seen: list[SeenItem],
    new_items: list[SeenItem],
    today: date | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[SeenItem]:
    today = today or datetime.now().date()
    cutoff = today - timedelta(days=window_days)
    combined = seen + new_items
    kept = [i for i in combined if _parse(i.date) >= cutoff]
    return kept


def save_seen(items: list[SeenItem], path: str = "seen.json") -> None:
    Path(path).write_text(
        json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()
