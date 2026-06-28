# morning-bot

Daily news digest. Fetches RSS, triages and summarizes with Claude, posts a
Block Kit message to Slack. First job of a larger personal-automation system.
Full design: [docs/SPEC.md](docs/SPEC.md).

It runs two Claude calls per day:
1. **Triage** — scores every entry on `importance` and `content_potential`,
   drops noise, suppresses stories already covered in the last two weeks.
2. **Deepen** — fetches the full text of the survivors (falling back to the RSS
   blurb when a page is paywalled/blocked) and writes summaries + content angles.

Output: an overview, a "Worth a take" picks section, and a topic-grouped digest.

## Setup

```bash
git clone <repo> && cd daily-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # all tests run offline
```

To run locally, set the two secrets (e.g. in a `.env` you load yourself — it is
gitignored, never commit it):

```bash
export ANTHROPIC_API_KEY=...
export SLACK_WEBHOOK_URL=...
python -m src.jobs.news --dry-run   # prints Block Kit JSON, skips Slack
python -m src.jobs.news             # posts to Slack for real
```

## Adding a feed

Edit [config/feeds.yml](config/feeds.yml), commit, push. The next scheduled run
picks it up. Native RSS and Substack (`<publication>.substack.com/feed`) both
slot straight in. Email-only sources (LinkedIn newsletters, TLDR) need an
email-to-RSS bridge first — treat those as manual one-offs.

`topic_hint` is optional; it nudges classification but Claude can override it.

## Tuning

Defaults live as env-overridable constants at the top of
[src/jobs/news.py](src/jobs/news.py): `IMPORTANCE_THRESHOLD`, `CONTENT_PICKS`,
`CONTENT_POTENTIAL_FLOOR`, `MAX_ITEMS_IN_DIGEST`, `LOOKBACK_HOURS`,
`MAX_ENTRIES_TO_TRIAGE`. Every run prints a `METRICS {json}` line (entries found,
selected per axis, full-text hit rate, tokens, cost) — watch these for the first
week or two and adjust the thresholds.

The reader profile in [config/profile.md](config/profile.md) drives both axes;
keep it short and concrete.

## Running it on a schedule

GitHub Actions runs it daily (`.github/workflows/news-digest.yml`, 07:30 BST).
Trigger manually from the Actions tab → **News Digest** → **Run workflow**
(optionally with `dry_run=true`). The "already covered" state (`seen.json`) is
tracked in the repo: each real run commits the updated file back, so local and
scheduled runs share one dedup history.

## Secrets

Set in GitHub → Settings → Secrets and variables → Actions:

- `ANTHROPIC_API_KEY`
- `SLACK_WEBHOOK_URL`

## Adding a new job later

Future jobs follow the same pattern: a file in `src/jobs/`, a workflow in
`.github/workflows/`, optional small libs in `src/lib/`. Reuse `claude`, `slack`,
`config` by importing them — don't refactor them.
