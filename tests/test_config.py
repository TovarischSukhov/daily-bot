from __future__ import annotations

from pathlib import Path

import pytest

from src.lib.config import load_feeds, load_profile

FEEDS_YML = """
feeds:
  - name: Example
    url: https://example.com/feed
    topic_hint: Tech
  - name: NoHint
    url: https://example.org/rss
"""


def test_load_feeds(tmp_path: Path) -> None:
    p = tmp_path / "feeds.yml"
    p.write_text(FEEDS_YML)
    cfg = load_feeds(str(p))
    assert len(cfg.feeds) == 2
    assert cfg.feeds[0].name == "Example"
    assert cfg.feeds[0].topic_hint == "Tech"
    assert cfg.feeds[1].topic_hint is None


def test_load_profile(tmp_path: Path) -> None:
    p = tmp_path / "profile.md"
    p.write_text("# Reader\n- data person\n")
    assert "data person" in load_profile(str(p))


def test_load_profile_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert load_profile(str(tmp_path / "nope.md")) == ""
    assert "missing" in caplog.text


def test_load_profile_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = tmp_path / "profile.md"
    p.write_text("   \n")
    assert load_profile(str(p)) == ""
    assert "empty" in caplog.text
