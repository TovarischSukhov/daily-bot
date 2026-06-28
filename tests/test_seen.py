from __future__ import annotations

from datetime import date

from src.lib.seen import SeenItem, append_and_prune, format_for_prompt, load_seen, save_seen


def _item(url: str, d: str) -> SeenItem:
    return SeenItem(url=url, title="T", topic="AI", gist="g", date=d)


def test_prune_drops_old() -> None:
    today = date(2026, 6, 16)
    seen = [_item("old", "2026-05-01"), _item("recent", "2026-06-10")]
    new = [_item("today", "2026-06-16")]
    kept = append_and_prune(seen, new, today=today, window_days=14)
    urls = {i.url for i in kept}
    assert urls == {"recent", "today"}


def test_format_and_roundtrip(tmp_path) -> None:
    assert "nothing" in format_for_prompt([])
    items = [_item("u", "2026-06-16")]
    assert "[AI]" in format_for_prompt(items)
    p = tmp_path / "seen.json"
    save_seen(items, str(p))
    assert load_seen(str(p))[0].url == "u"


def test_load_missing(tmp_path) -> None:
    assert load_seen(str(tmp_path / "nope.json")) == []
