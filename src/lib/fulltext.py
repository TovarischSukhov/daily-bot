from __future__ import annotations

import logging

import httpx
import trafilatura

from src.lib.rss import Entry

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10.0
MIN_FULLTEXT_CHARS = 200
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _fetch_and_extract(url: str) -> str | None:
    try:
        resp = httpx.get(
            url,
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
    except Exception as e:  # blocked / timeout / non-2xx — degrade to blurb upstream
        logger.warning("fulltext fetch failed for %s: %s", url, e)
        return None
    extracted = trafilatura.extract(resp.text)
    if extracted and len(extracted) >= MIN_FULLTEXT_CHARS:
        return extracted
    return None  # paywall / nav / JS-only page


def resolve_text(entry: Entry) -> tuple[str, bool]:
    """Return (text, used_fulltext). Try easy, fall back to the blurb."""
    if entry.content and len(entry.content) >= MIN_FULLTEXT_CHARS:
        return entry.content, True
    fetched = _fetch_and_extract(entry.url)
    if fetched:
        return fetched, True
    return entry.summary, False
