from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx
from pydantic import BaseModel

from src.lib.config import Feed

logger = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 500
FETCH_TIMEOUT = 20.0
_TAG_RE = re.compile(r"<[^>]+>")
# Some feed servers reject header-less requests (415/403). Send a browser-like
# UA and an XML Accept so they serve the feed.
_FEED_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
# Tracking params to strip so the stored URL matches the canonical one Claude
# echoes back (and so links are clean). Keep meaningful query params.
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "_hsenc", "_hsmi"}


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.startswith("utm_") and k not in _TRACKING_KEYS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


class Entry(BaseModel):
    feed_name: str
    title: str
    url: str
    summary: str
    published_at: datetime
    topic_hint: str | None = None
    content: str | None = None  # full text if the feed embeds it (content:encoded)


def _strip_html(text: str) -> str:
    """Strip tags with stdlib only — no separate HTML parser dependency."""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _parsed_to_dt(struct) -> datetime | None:
    if not struct:
        return None
    return datetime(*struct[:6], tzinfo=UTC)


def _full_content(entry) -> str | None:
    content = entry.get("content")
    if content and isinstance(content, list) and content[0].get("value"):
        text = _strip_html(content[0]["value"])
        return text or None
    return None


def _title_filter(feed: Feed) -> Callable[[str], bool]:
    """Return a predicate: True if the title should be dropped."""
    patterns = [re.compile(p, re.IGNORECASE) for p in feed.exclude_title]
    return lambda title: any(p.search(title) for p in patterns)


def _clean_title(title: str, feed_name: str) -> str:
    """Drop the ' - Publisher' suffix aggregators (Google News) append."""
    suffix = f" - {feed_name}"
    return title[: -len(suffix)] if title.endswith(suffix) else title


def _blurb(summary: str, title: str) -> str:
    """Aggregators repeat the headline as the description — that carries no
    extra signal, so drop it rather than pay tokens for it twice."""
    if title and summary.replace("\xa0", " ").strip().startswith(title):
        return ""
    return summary


def fetch_recent_entries(feeds: list[Feed], since: datetime) -> list[Entry]:
    out: list[Entry] = []
    seen_urls: set[str] = set()
    with httpx.Client(
        timeout=FETCH_TIMEOUT, follow_redirects=True, headers=_FEED_HEADERS
    ) as client:
        for feed in feeds:
            try:
                resp = client.get(str(feed.url))
                resp.raise_for_status()
            except Exception as e:  # one bad feed must not abort the run
                logger.error("feed %s failed: %s", feed.name, e)
                continue
            parsed = feedparser.parse(resp.content)
            drop_title = _title_filter(feed)
            for entry in parsed.entries:
                published = _parsed_to_dt(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                )
                if published is None or published < since:
                    continue
                url = entry.get("link")
                if not url:
                    continue
                url = _canonical_url(url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                title = _clean_title(_strip_html(entry.get("title", "")), feed.name)
                if drop_title(title):
                    continue
                summary = _blurb(
                    _strip_html(entry.get("summary", ""))[:SUMMARY_MAX_CHARS], title
                )
                out.append(
                    Entry(
                        feed_name=feed.name,
                        title=title,
                        url=url,
                        summary=summary,
                        published_at=published,
                        topic_hint=feed.topic_hint,
                        content=_full_content(entry),
                    )
                )
    return out
