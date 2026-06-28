from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx

from src.lib.fulltext import resolve_text
from src.lib.rss import Entry


def _entry(**kw) -> Entry:
    base = dict(
        feed_name="F",
        title="T",
        url="https://x.com/a",
        summary="short blurb",
        published_at=datetime.now(UTC),
    )
    base.update(kw)
    return Entry(**base)


def test_uses_embedded_content() -> None:
    text, full = resolve_text(_entry(content="x" * 300))
    assert full is True
    assert text == "x" * 300


@respx.mock
def test_falls_back_to_blurb_on_fetch_failure() -> None:
    respx.get("https://x.com/a").mock(return_value=httpx.Response(403))
    text, full = resolve_text(_entry())
    assert full is False
    assert text == "short blurb"


@respx.mock
def test_fetches_and_extracts() -> None:
    article = "<html><body><article>" + ("Real sentence. " * 40) + "</article></body></html>"
    respx.get("https://x.com/a").mock(return_value=httpx.Response(200, text=article))
    text, full = resolve_text(_entry())
    assert full is True
    assert "Real sentence" in text
