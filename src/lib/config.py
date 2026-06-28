from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, HttpUrl

logger = logging.getLogger(__name__)


class Feed(BaseModel):
    name: str
    url: HttpUrl
    topic_hint: str | None = None  # optional, helps Claude classify


class FeedsConfig(BaseModel):
    feeds: list[Feed]


def load_feeds(path: str = "config/feeds.yml") -> FeedsConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return FeedsConfig(**raw)


def load_profile(path: str = "config/profile.md") -> str:
    p = Path(path)
    if not p.exists():
        logger.warning("profile file %s missing; content_potential/angle will be generic", path)
        return ""
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        logger.warning("profile file %s empty; content_potential/angle will be generic", path)
        return ""
    return text
