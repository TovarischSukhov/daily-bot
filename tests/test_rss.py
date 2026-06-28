from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import respx

from src.lib.config import Feed
from src.lib.rss import fetch_recent_entries

NOW = datetime.now(UTC)


def _rss(items: list[str]) -> str:
    body = "".join(items)
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel>{body}</channel></rss>"
    )


def _item(title: str, link: str, when: datetime, *, content: str | None = None) -> str:
    encoded = f"<content:encoded>{content}</content:encoded>" if content else ""
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>blurb for {title} &amp; more</description>"
        f"<pubDate>{format_datetime(when)}</pubDate>{encoded}</item>"
    )


@respx.mock
def test_window_and_dedup() -> None:
    feed_a = _rss(
        [
            _item("Fresh", "https://x.com/fresh", NOW - timedelta(hours=2)),
            _item("Old", "https://x.com/old", NOW - timedelta(hours=48)),
        ]
    )
    # Same URL as Fresh appears in feed B → must be deduped within the run.
    feed_b = _rss([_item("Dup", "https://x.com/fresh", NOW - timedelta(hours=1))])
    respx.get("https://a.com/feed").mock(return_value=httpx.Response(200, text=feed_a))
    respx.get("https://b.com/feed").mock(return_value=httpx.Response(200, text=feed_b))

    feeds = [
        Feed(name="A", url="https://a.com/feed"),
        Feed(name="B", url="https://b.com/feed"),
    ]
    entries = fetch_recent_entries(feeds, since=NOW - timedelta(hours=24))

    urls = [e.url for e in entries]
    assert urls == ["https://x.com/fresh"]  # Old dropped, Dup deduped
    assert "&" in entries[0].summary  # HTML entity unescaped


@respx.mock
def test_strips_tracking_params() -> None:
    url = "https://x.com/post?utm_source=rss&utm_medium=feed&id=42"
    feed = _rss([_item("T", url, NOW - timedelta(hours=1))])
    respx.get("https://a.com/feed").mock(return_value=httpx.Response(200, text=feed))
    feeds = [Feed(name="A", url="https://a.com/feed")]
    entries = fetch_recent_entries(feeds, NOW - timedelta(hours=24))
    # utm_* stripped, meaningful ?id=42 kept
    assert entries[0].url == "https://x.com/post?id=42"


@respx.mock
def test_full_content_extracted() -> None:
    feed = _rss(
        [_item("Withtext", "https://x.com/t", NOW - timedelta(hours=1), content="<p>Full body</p>")]
    )
    respx.get("https://a.com/feed").mock(return_value=httpx.Response(200, text=feed))
    feeds = [Feed(name="A", url="https://a.com/feed")]
    entries = fetch_recent_entries(feeds, NOW - timedelta(hours=24))
    assert entries[0].content == "Full body"


@respx.mock
def test_failing_feed_skipped(caplog) -> None:
    good = _rss([_item("Good", "https://x.com/good", NOW - timedelta(hours=1))])
    respx.get("https://bad.com/feed").mock(return_value=httpx.Response(500))
    respx.get("https://good.com/feed").mock(return_value=httpx.Response(200, text=good))
    feeds = [
        Feed(name="Bad", url="https://bad.com/feed"),
        Feed(name="Good", url="https://good.com/feed"),
    ]
    entries = fetch_recent_entries(feeds, NOW - timedelta(hours=24))
    assert [e.feed_name for e in entries] == ["Good"]
    assert "Bad" in caplog.text
