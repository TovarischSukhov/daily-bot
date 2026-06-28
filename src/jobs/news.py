from __future__ import annotations

import argparse
import json
import logging
import os
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from src.lib import claude, slack
from src.lib.config import load_feeds, load_profile
from src.lib.fulltext import resolve_text
from src.lib.rss import Entry, fetch_recent_entries
from src.lib.seen import (
    SeenItem,
    append_and_prune,
    format_for_prompt,
    load_seen,
    save_seen,
)

logger = logging.getLogger(__name__)

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
IMPORTANCE_THRESHOLD = int(os.getenv("IMPORTANCE_THRESHOLD", "3"))  # busy-day cream bar
QUIET_DAY_MAX = int(os.getenv("QUIET_DAY_MAX", "5"))  # <= this many vetted → show all
MAX_ITEMS_IN_DIGEST = int(os.getenv("MAX_ITEMS_IN_DIGEST", "15"))
MAX_ENTRIES_TO_TRIAGE = int(os.getenv("MAX_ENTRIES_TO_TRIAGE", "400"))
CONTENT_PICKS = int(os.getenv("CONTENT_PICKS", "3"))
CONTENT_POTENTIAL_FLOOR = int(os.getenv("CONTENT_POTENTIAL_FLOOR", "3"))

# claude-sonnet-4-6 pricing, USD per token
PRICE_IN = 3.0 / 1_000_000
PRICE_OUT = 15.0 / 1_000_000

PROMPTS_DIR = Path("prompts")


class Item(BaseModel):
    url: str
    title: str
    source: str
    topic: str
    importance: int
    content_potential: int
    summary: str = ""
    angle: str | None = None


def log(msg: str) -> None:
    print(msg)


def _as_id(value: object) -> int | None:
    """Coerce a model-returned id to int; None if it isn't a clean integer."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _load_prompt(name: str) -> tuple[str, str]:
    """Return (system_template, user_template), split on the first `---`."""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    system, _, user = text.partition("\n---\n")
    return system.strip(), user.strip()


class Usage(BaseModel):
    input: int = 0
    output: int = 0

    def add(self, resp: claude.ClaudeResponse) -> None:
        self.input += resp.input_tokens
        self.output += resp.output_tokens


def triage(
    entries: list[Entry], profile: str, seen_block: str, usage: Usage
) -> tuple[str, list[Item]]:
    system_tpl, user_tpl = _load_prompt("triage.md")
    by_id = dict(enumerate(entries))
    payload = [
        {"id": i, "title": e.title, "blurb": e.summary, "topic_hint": e.topic_hint}
        for i, e in enumerate(entries)
    ]
    system = system_tpl.format(profile=profile or "(no profile provided)", seen=seen_block)
    user = user_tpl.format(entries_json=json.dumps(payload, ensure_ascii=False))
    resp = claude.call(system=system, user=user, response_format="json")
    usage.add(resp)
    data = json.loads(resp.text)

    items: list[Item] = []
    for raw in data.get("items", []):
        entry = by_id.get(_as_id(raw.get("id")))
        if entry is None:
            logger.warning("triage returned unknown id %s; dropping", raw.get("id"))
            continue
        try:
            imp = int(raw["importance"])
            cp = int(raw["content_potential"])
        except (KeyError, ValueError, TypeError):
            logger.warning("triage item id=%s has bad scores; dropping", raw.get("id"))
            continue
        if not (1 <= imp <= 5 and 1 <= cp <= 5):
            logger.warning("triage item id=%s scores out of range; dropping", raw.get("id"))
            continue
        items.append(
            Item(
                url=entry.url,
                title=entry.title,
                source=entry.feed_name,
                topic=str(raw.get("topic") or entry.topic_hint or "Misc"),
                importance=imp,
                content_potential=cp,
                summary=entry.summary,  # placeholder until deepen
            )
        )
    return str(data.get("overview", "")), items


def deepen(
    items: list[Item], entries_by_url: dict[str, Entry], profile: str, usage: Usage
) -> tuple[int, int]:
    """Fill summary + angle in place. Return (fulltext_ok, fulltext_fallback)."""
    system_tpl, user_tpl = _load_prompt("deepen.md")
    ok = fallback = 0
    payload = []
    for i, it in enumerate(items):
        text, used_full = resolve_text(entries_by_url[it.url])
        ok += used_full
        fallback += not used_full
        payload.append(
            {
                "id": i,
                "title": it.title,
                "topic": it.topic,
                "importance": it.importance,
                "content_potential": it.content_potential,
                "source": it.source,
                "text": text,
            }
        )
    system = system_tpl.format(profile=profile or "(no profile provided)")
    user = user_tpl.format(items_json=json.dumps(payload, ensure_ascii=False))
    resp = claude.call(system=system, user=user, response_format="json")
    usage.add(resp)

    written = {_as_id(r.get("id")): r for r in json.loads(resp.text).get("items", [])}
    for i, it in enumerate(items):
        r = written.get(i)
        if r is None:
            logger.warning("deepen returned no summary for id=%d (%s); keeping blurb", i, it.url)
            continue
        it.summary = str(r.get("summary") or it.summary)
        angle = r.get("angle")
        it.angle = str(angle) if angle else None
    return ok, fallback


def effective_importance_threshold(n_vetted: int) -> int:
    """Adaptive: on a quiet day (few items survive triage) show everything;
    on a busy day keep only the cream above IMPORTANCE_THRESHOLD."""
    return 1 if n_vetted <= QUIET_DAY_MAX else IMPORTANCE_THRESHOLD


def select(items: list[Item], importance_threshold: int) -> tuple[list[Item], list[Item]]:
    filtered = [i for i in items if i.importance >= importance_threshold]
    picks = sorted(
        (i for i in items if i.content_potential >= CONTENT_POTENTIAL_FLOOR),
        key=lambda i: i.content_potential,
        reverse=True,
    )[:CONTENT_PICKS]
    return filtered, picks


def format_slack_blocks(overview: str, items: list[Item], picks: list[Item]) -> list[dict]:
    blocks: list[dict] = []

    if overview:
        today = datetime.now().strftime("%Y-%m-%d")
        blocks.append(_header(f"📰 Daily digest — {today}"))
        blocks.append(_section(overview))

    if picks:
        blocks.append(_header("🖊️ Worth a take"))
        for p in picks:
            text = f"*<{p.url}|{p.title}>*\n{p.summary}"
            if p.angle:
                text += f"\n\n💡 *Angle:* {p.angle}"
            blocks.append(_section(text))
            blocks.append(_meta(f"📰 {p.source}  ·  <{p.url}|Read article ↗>"))
            blocks.append({"type": "divider"})

    if items:
        blocks.append(_header("📂 What's going on"))
        shown, overflow = _cap_digest(items)
        by_topic: dict[str, list[Item]] = {}
        for it in sorted(shown, key=lambda i: i.importance, reverse=True):
            by_topic.setdefault(it.topic, []).append(it)
        for topic, topic_items in by_topic.items():
            blocks.append(_section(f"*{topic.upper()}*"))
            for it in topic_items:
                blocks.append(_section(f"*<{it.url}|{it.title}>*\n{it.summary}"))
                blocks.append(
                    _meta(
                        f"📰 {it.source}  ·  importance {it.importance}  ·  "
                        f"<{it.url}|Read article ↗>"
                    )
                )
            blocks.append({"type": "divider"})
        if overflow:
            blocks.append(_meta(f"+{overflow} more items below the cut"))
    return blocks


def _cap_digest(items: list[Item]) -> tuple[list[Item], int]:
    if len(items) <= MAX_ITEMS_IN_DIGEST:
        return items, 0
    ranked = sorted(items, key=lambda i: i.importance, reverse=True)
    return ranked[:MAX_ITEMS_IN_DIGEST], len(items) - MAX_ITEMS_IN_DIGEST


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _meta(text: str) -> dict:
    """A context block — small grey text, gives visual spacing between items."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _print_metrics(m: dict) -> None:
    line = " ".join(f"{k}={v}" for k, v in m.items())
    print(f"news-digest metrics: {line}")
    print(f"METRICS {json.dumps(m)}")


def run(dry_run: bool = False) -> int:
    started = time.monotonic()
    metrics: dict[str, object] = {}
    usage = Usage()
    try:
        config = load_feeds()
        profile = load_profile()
        seen = load_seen()
        since = datetime.now(UTC) - timedelta(hours=LOOKBACK_HOURS)

        entries = fetch_recent_entries(config.feeds, since)
        by_url = {e.url: e for e in entries}
        metrics.update(
            feeds_total=len(config.feeds),
            entries_found=len(entries),
            seen_list_size=len(seen),
        )
        if not entries:
            log("No entries in window; exiting cleanly.")
            _finish(metrics, usage, started)
            return 0

        entries.sort(key=lambda e: e.published_at, reverse=True)
        dropped_cap = max(0, len(entries) - MAX_ENTRIES_TO_TRIAGE)
        if dropped_cap:
            log(f"Capping triage input: dropping {dropped_cap} oldest entries.")
        triage_input = entries[:MAX_ENTRIES_TO_TRIAGE]
        metrics["entries_to_triage"] = len(triage_input)
        metrics["entries_dropped_cap"] = dropped_cap

        overview, scored = triage(triage_input, profile, format_for_prompt(seen), usage)
        threshold = effective_importance_threshold(len(scored))
        metrics["triage_selected"] = len(scored)
        metrics["triage_dropped"] = len(triage_input) - len(scored)
        metrics["day_mode"] = "quiet" if threshold == 1 else "busy"
        metrics["importance_threshold_used"] = threshold
        metrics["selected_content"] = sum(
            1 for i in scored if i.content_potential >= CONTENT_POTENTIAL_FLOOR
        )

        filtered, picks = select(scored, threshold)
        if not filtered and not picks:
            log("Nothing above threshold and no content picks; exiting cleanly.")
            _finish(metrics, usage, started)
            return 0

        # Deepen only what we will actually show (union of digest + picks).
        to_write = list({i.url: i for i in filtered + picks}.values())
        ok, fallback = deepen(to_write, by_url, profile, usage)

        # Picks are shown in "Worth a take"; keep them out of the digest so each
        # story appears once.
        pick_urls = {p.url for p in picks}
        digest_items = [i for i in filtered if i.url not in pick_urls]
        metrics["fulltext_ok"] = ok
        metrics["fulltext_fallback_blurb"] = fallback
        metrics["digest_items"] = len(digest_items)
        metrics["picks"] = len(picks)

        blocks = format_slack_blocks(overview, digest_items, picks)
        if dry_run:
            print(json.dumps(blocks, indent=2, ensure_ascii=False))
            _finish(metrics, usage, started)
            return 0

        slack.send_blocks(
            blocks, fallback_text=f"News digest: {len(picks)} picks, {len(digest_items)} items"
        )

        delivered = {i.url: i for i in filtered + picks}.values()
        today = datetime.now().date().isoformat()
        new_seen = [
            SeenItem(url=i.url, title=i.title, topic=i.topic, gist=i.summary, date=today)
            for i in delivered
        ]
        save_seen(append_and_prune(seen, new_seen))
        _finish(metrics, usage, started)
        return 0
    except Exception as e:
        traceback.print_exc()
        try:
            slack.send_error("news-digest", f"{type(e).__name__}: {e}")
        except Exception:
            logger.error("error reporter itself failed; original error already logged above")
        _finish(metrics, usage, started)
        return 1


def _finish(metrics: dict, usage: Usage, started: float) -> None:
    metrics["claude_input_tokens"] = usage.input
    metrics["claude_output_tokens"] = usage.output
    metrics["claude_cost_est"] = round(usage.input * PRICE_IN + usage.output * PRICE_OUT, 4)
    metrics["duration_seconds"] = round(time.monotonic() - started, 1)
    _print_metrics(metrics)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Daily news digest job")
    parser.add_argument("--dry-run", action="store_true", help="skip Slack, print Block Kit JSON")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
