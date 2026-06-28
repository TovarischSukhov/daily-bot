from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.jobs import news
from src.lib import claude, slack
from src.lib.config import Feed, FeedsConfig
from src.lib.rss import Entry

NOW = datetime.now(UTC)


def _entry(url: str, title: str) -> Entry:
    return Entry(
        feed_name="Feed", title=title, url=url, summary=f"blurb {title}", published_at=NOW
    )


# u1: high importance, low content → digest only
# u2: low importance, high content → pick only (axes are independent)
# u3: noise → triage drops it entirely
ENTRIES = [_entry("u1", "Alpha"), _entry("u2", "Beta"), _entry("u3", "Gamma")]

# ids are the entry's index in the batch: 0=Alpha(u1), 1=Beta(u2), 2=Gamma(u3)
TRIAGE_JSON = json.dumps(
    {
        "overview": "Today the theme is data.",
        "items": [
            {"id": 0, "topic": "AI", "importance": 5, "content_potential": 2},
            {"id": 1, "topic": "Data", "importance": 1, "content_potential": 5},
        ],
    }
)
# deepen receives [Alpha, Beta] (filtered + picks), so ids are 0 and 1 again
DEEPEN_JSON = json.dumps(
    {
        "items": [
            {"id": 0, "summary": "Alpha summary.", "angle": None},
            {"id": 1, "summary": "Beta summary.", "angle": "Write about data stacks"},
        ]
    }
)


def _resp(text: str) -> claude.ClaudeResponse:
    return claude.ClaudeResponse(text=text, input_tokens=10, output_tokens=5)


@pytest.fixture
def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(news, "load_feeds", lambda: FeedsConfig(feeds=[Feed(name="Feed", url="https://x.com/f")]))
    monkeypatch.setattr(news, "load_profile", lambda: "profile text")
    monkeypatch.setattr(news, "load_seen", lambda: [])
    monkeypatch.setattr(news, "fetch_recent_entries", lambda feeds, since: list(ENTRIES))
    monkeypatch.setattr(news, "resolve_text", lambda entry: (entry.summary, False))


def _calls_in_order(*texts: str):
    it = iter(texts)

    def fake_call(**kwargs):
        return _resp(next(it))

    return fake_call


def _headers_to_text(blocks: list[dict]) -> dict[str, str]:
    """Map each header's text to the concatenated text of sections under it."""
    out: dict[str, str] = {}
    current = None
    for b in blocks:
        if b["type"] == "header":
            current = b["text"]["text"]
            out[current] = ""
        elif b["type"] == "section" and current is not None:
            out[current] += b["text"]["text"] + "\n"
    return out


def test_dry_run_digest_and_picks(monkeypatch, capsys, _wire) -> None:
    monkeypatch.setattr(claude, "call", _calls_in_order(TRIAGE_JSON, DEEPEN_JSON))

    assert news.run(dry_run=True) == 0

    out = capsys.readouterr().out
    blocks = json.loads(out.split("news-digest metrics:")[0])
    sections = _headers_to_text(blocks)

    # Overview present
    assert any("Today the theme is data." in v for v in sections.values())
    # Worth a take contains Beta + its angle
    take = next(v for k, v in sections.items() if "Worth a take" in k)
    assert "Beta" in take and "Write about data stacks" in take
    # Digest contains Alpha (importance 5) but NOT Beta (importance 1 < threshold)
    digest = next(v for k, v in sections.items() if "going on" in k)
    assert "Alpha" in digest
    assert "Beta" not in digest


def test_metrics_emitted(monkeypatch, capsys, _wire) -> None:
    monkeypatch.setattr(claude, "call", _calls_in_order(TRIAGE_JSON, DEEPEN_JSON))
    news.run(dry_run=True)
    out = capsys.readouterr().out
    metrics = json.loads(out.split("METRICS ", 1)[1].splitlines()[0])
    assert metrics["entries_found"] == 3
    assert metrics["triage_selected"] == 2
    assert metrics["digest_items"] == 1
    assert metrics["picks"] == 1
    assert metrics["fulltext_fallback_blurb"] == 2
    assert metrics["claude_input_tokens"] == 20


def test_pick_not_duplicated_in_digest(monkeypatch, capsys, _wire) -> None:
    # One item that is BOTH a pick (cp=5) and clears the importance threshold —
    # it must appear only under "Worth a take", not in the digest.
    triage = json.dumps(
        {"overview": "o", "items": [
            {"id": 0, "topic": "Retail", "importance": 5, "content_potential": 5}]}
    )
    deepen = json.dumps({"items": [{"id": 0, "summary": "Alpha summary.", "angle": "take it"}]})
    monkeypatch.setattr(claude, "call", _calls_in_order(triage, deepen))

    assert news.run(dry_run=True) == 0
    out = capsys.readouterr().out
    sections = _headers_to_text(json.loads(out.split("news-digest metrics:")[0]))
    assert "Alpha" in next(v for k, v in sections.items() if "Worth a take" in k)
    assert not any("going on" in k for k in sections)  # digest section skipped entirely


def test_failure_reports_and_exits_1(monkeypatch, _wire) -> None:
    def boom(**kwargs):
        raise RuntimeError("triage exploded")

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(claude, "call", boom)
    monkeypatch.setattr(slack, "send_error", lambda job, err: sent.append((job, err)))

    assert news.run(dry_run=True) == 1
    assert sent and sent[0][0] == "news-digest"
    assert "triage exploded" in sent[0][1]
