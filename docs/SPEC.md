# News Digest Bot — Spec for Claude Code

## Goal

Build the first job of the `morning-bot` system: a daily news digest that fetches RSS feeds, summarizes and classifies entries with Claude, and delivers a structured digest to Slack via incoming webhook.

The digest serves two purposes for the reader (a founder working in data / AI / BI for e-commerce):

1. **Stay informed** — a short overview of what's going on plus a topic-grouped scan of the day.
2. **Feed content creation** — surface 2–3 items the reader could write their own commentary about, each with a TL;DR, a link, and a suggested angle.

These are scored on **two independent axes**: `importance` (worth knowing) drives the informational digest; `content_potential` (a good basis for the reader's own content) drives the picks. They don't correlate — a major-but-generic story is high importance / low content_potential; a niche story matching the reader's angle is the reverse.

This is the MVP of a larger personal-automation system. The architecture established here (lib layout, secrets, workflow pattern, error handling) will be reused for future jobs (day plan, health summary, leads). Build it so adding more jobs requires importing, not refactoring.

## Stack

- Python 3.11+
- GitHub Actions (cron + secrets)
- Anthropic API for summarization, model `claude-sonnet-4-6`
- Slack incoming webhook for delivery
- `feedparser` for RSS
- `httpx` for HTTP
- `pydantic` for typed config and payloads
- `pytest` + `respx` for tests (the HTTP client is `httpx`; `responses` only patches `requests`, so it will NOT work here)

No paid services. Everything runs on GitHub Actions free tier.

## Repository Layout

```
morning-bot/
├── .github/workflows/
│   └── news-digest.yml
├── src/
│   ├── __init__.py
│   ├── jobs/
│   │   ├── __init__.py
│   │   └── news.py
│   └── lib/
│       ├── __init__.py
│       ├── claude.py
│       ├── slack.py
│       ├── rss.py
│       └── config.py
├── config/
│   ├── feeds.yml
│   └── profile.md
├── prompts/
│   └── news.md
├── tests/
│   ├── __init__.py
│   ├── test_claude.py
│   ├── test_slack.py
│   ├── test_rss.py
│   └── test_news_job.py
├── pyproject.toml
├── README.md
└── .gitignore
```

Future jobs will add `src/jobs/plan.py`, `src/jobs/health.py`, etc., and additional libs (`telegram.py`, `clickup.py`, etc.). Do NOT scaffold them yet — only what this job needs.

## What the Job Does

On schedule:

1. Load feed list from `config/feeds.yml` and the reader profile from `config/profile.md`
2. Fetch entries from each feed published in the last 24 hours
3. Deduplicate (same URL across feeds)
4. Cap entries to `MAX_ENTRIES_TO_CLAUDE` (newest first) — see Cost & Token Control
5. Send the capped entries to Claude in one call with the prompt from `prompts/news.md`, with the profile injected
6. Claude returns structured JSON: an `overview` string plus a list of items with title, summary, url, topic, `importance`, `content_potential`, `angle`, source
7. Build the **digest**: items where `importance >= effective_importance_threshold(n_vetted)` — adaptive (see `QUIET_DAY_MAX` under Constants): show all on quiet days, only the cream on busy days. Grouped by topic, sorted by importance descending within each topic
8. Build the **content picks**: items where `content_potential >= CONTENT_POTENTIAL_FLOOR`, ranked by `content_potential` descending, top `CONTENT_PICKS` (picks are selected independently of the importance filter — a pick may be below the importance threshold)
9. Format as a Slack message using Block Kit: Overview → Worth a take (picks) → topic-grouped digest
10. POST to Slack webhook
11. Exit 0 on success; on any failure, POST a brief error message to Slack and exit 1

Out of scope for this job: routing to multiple channels, Telegram delivery, persistence between runs, deduplication across days. Keep it strictly one feed-list → one Slack message.

## Libraries

### `src/lib/config.py`

Loads and validates configuration. Uses pydantic.

```python
class Feed(BaseModel):
    name: str
    url: HttpUrl
    topic_hint: str | None = None  # optional, helps Claude classify

class FeedsConfig(BaseModel):
    feeds: list[Feed]

def load_feeds(path: str = "config/feeds.yml") -> FeedsConfig: ...

def load_profile(path: str = "config/profile.md") -> str: ...
```

`topic_hint` is optional — if the user knows a feed is always about, e.g., "AI research", they can pre-label it; Claude can still override. Default behavior is no hint.

`load_profile` reads `config/profile.md` and returns its contents as a plain string (the reader profile injected into the prompt). If the file is missing or empty, return `""` and log a warning — the job still runs, but `content_potential` and `angle` quality degrades to generic.

### `src/lib/rss.py`

Single function:

```python
def fetch_recent_entries(
    feeds: list[Feed],
    since: datetime,
) -> list[Entry]: ...
```

`Entry` is a pydantic model: `{feed_name, title, url, summary, published_at, topic_hint}`. Skip entries without a valid `published_parsed` or with `published_at < since`. Truncate `summary` to 500 chars (strip HTML tags first using `feedparser`'s built-in handling; do NOT pull a separate HTML parser). Deduplicate by URL.

Handle failures gracefully: if one feed throws, log the error to stdout with feed name and continue. Do not abort the run for one bad feed.

### `src/lib/claude.py`

Wrapper around the Anthropic Python SDK.

```python
def call(
    system: str,
    user: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    response_format: Literal["text", "json"] = "text",
) -> str: ...
```

- Reads `ANTHROPIC_API_KEY` from env
- Retries on 429 and 5xx with exponential backoff (3 attempts, base 2s)
- When `response_format="json"`, instruct in the system prompt that the response must be valid JSON only with no preamble or code fences; strip ` ```json ` / ` ``` ` wrappers if Claude adds them anyway, then `json.loads` and return as a string (caller parses)
- Logs token usage to stdout

### `src/lib/slack.py`

```python
def send_blocks(blocks: list[dict], fallback_text: str) -> None: ...
def send_error(job_name: str, error: str) -> None: ...
```

- Reads `SLACK_WEBHOOK_URL` from env
- `send_blocks` posts a Block Kit message; `fallback_text` is the `text` field used for notifications
- `send_error` posts a simple red-formatted error: job name, first 500 chars of error, run URL if available from `GITHUB_RUN_URL` env (Actions sets this automatically — actually it sets `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`; compose the URL from those)
- Raises on non-2xx response so the job fails loudly if Slack rejects the payload

## Job: `src/jobs/news.py`

Entry point: `python -m src.jobs.news`.

Flow:

```python
def run(dry_run: bool = False) -> int:
    try:
        config = load_feeds()
        profile = load_profile()
        since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        entries = fetch_recent_entries(config.feeds, since)
        if not entries:
            log("No entries in window; exiting cleanly.")
            return 0
        digest = summarize_and_classify(entries, profile)
        filtered = [i for i in digest.items if i.importance >= IMPORTANCE_THRESHOLD]
        picks = sorted(
            (i for i in digest.items if i.content_potential >= CONTENT_POTENTIAL_FLOOR),
            key=lambda i: i.content_potential,
            reverse=True,
        )[:CONTENT_PICKS]
        if not filtered and not picks:
            log("Nothing above threshold and no content picks; exiting cleanly.")
            return 0
        blocks = format_slack_blocks(digest.overview, filtered, picks)
        if dry_run:
            print(json.dumps(blocks, indent=2))
            return 0
        send_blocks(blocks, fallback_text=f"News digest: {len(picks)} picks, {len(filtered)} items")
        return 0
    except Exception as e:
        traceback.print_exc()
        try:
            send_error("news-digest", f"{type(e).__name__}: {e}")
        except Exception:
            pass  # never fail in the error reporter
        return 1
```

`--dry-run` flag (via argparse) skips Slack delivery and prints the Block Kit JSON to stdout. Used by integration test and for manual debugging.

### Constants (top of file, overridable via env)

- `LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))`
- `IMPORTANCE_THRESHOLD = int(os.getenv("IMPORTANCE_THRESHOLD", "3"))` — the cream bar applied on busy days
- `QUIET_DAY_MAX = int(os.getenv("QUIET_DAY_MAX", "5"))` — adaptive gate: if triage vets `<= QUIET_DAY_MAX` items, show all of them (effective threshold 1); above that, fall back to `IMPORTANCE_THRESHOLD`. So quiet days never go empty and busy days stay selective. Triage is the real relevance filter; this only decides how much of its output to surface.
- `MAX_ITEMS_IN_DIGEST = int(os.getenv("MAX_ITEMS_IN_DIGEST", "15"))` — hard cap to keep Slack message readable
- `MAX_ENTRIES_TO_CLAUDE = int(os.getenv("MAX_ENTRIES_TO_CLAUDE", "60"))` — hard cap on entries sent to Claude in one call; bounds input-token spend (see Cost & Token Control)
- `CONTENT_PICKS = int(os.getenv("CONTENT_PICKS", "3"))` — how many "Worth a take" items to surface
- `CONTENT_POTENTIAL_FLOOR = int(os.getenv("CONTENT_POTENTIAL_FLOOR", "3"))` — minimum `content_potential` for an item to be eligible as a pick

### `summarize_and_classify(entries, profile) -> Digest`

`Digest` is a pydantic model: `{overview: str, items: list[Item]}`.

`Item` is a pydantic model: `{title, summary (≤2 sentences), url, topic, importance (1–5), content_potential (1–5), angle (str | None), source (= original feed_name)}`.

Before serializing, sort entries by `published_at` descending and keep at most `MAX_ENTRIES_TO_CLAUDE` (drop the oldest beyond the cap; log how many were dropped). This is the only place that bounds input-token spend — `IMPORTANCE_THRESHOLD`, `CONTENT_PICKS`, and `MAX_ITEMS_IN_DIGEST` filter *after* the call and save no tokens.

Load the prompt from `prompts/news.md` and inject `profile` into the system half (see Prompt section). Build the user prompt by serializing the capped entries as compact JSON (only `feed_name`, `title`, `url`, `summary`, `topic_hint`). Call `claude.call(system=..., user=..., response_format="json")`. Parse the returned JSON object into a `Digest`.

Validate every returned item: required fields present, URL is one of the URLs that was sent in, `importance` and `content_potential` in 1..5, `angle` is a string when `content_potential >= CONTENT_POTENTIAL_FLOOR` (else may be null). Drop invalid items, log a warning. `overview` is a plain string; if missing, default to `""`.

### `format_slack_blocks(overview, items, picks) -> list[dict]`

Emit three parts in order:

**1. Overview** (skip if `overview` is empty):
- A `header` block: "📰 Daily digest — <date>"
- A `section` block with the `overview` text

**2. Worth a take** (skip if `picks` is empty):
- A `header` block: "🖊️ Worth a take"
- For each pick: a `section` block with mrkdwn: `*<url|title>*\n<summary>\n_Angle:_ <angle>`
- A `divider` after the section

**3. Digest** (skip if `items` is empty):
- A `header` block: "📂 What's going on"
- Group `items` by `topic`. For each topic: a `header` (or bold `section`) with the topic name, then for each item a `section` block with mrkdwn: `*<url|title>* _(importance N, source)_\n<summary>`, and a `divider` between topics.
- Cap the digest at `MAX_ITEMS_IN_DIGEST` items; if more, keep the highest-importance ones and add a final `context` block: "+N more items below the cut".

Picks are excluded from the digest so each story appears once: an item shown under "Worth a take" is not repeated in "What's going on", even if it also clears the importance threshold.

## Cost & Token Control

This job makes **one** Claude call per run with **no tools and no web access** — Claude only sees the entries serialized into the prompt; it cannot fetch URLs, search, or loop. So spend is bounded on both ends:

- **Input** — bounded by `MAX_ENTRIES_TO_CLAUDE` (the pre-call cap) × per-entry size (summaries truncated to 500 chars in `rss.py`). This is the dominant cost. A busy feed day or a misconfigured high-volume feed cannot blow up the bill past this cap.
- **Output** — bounded by `max_tokens=4096` in `claude.call`, a hard ceiling the API enforces.

Do **not** add prompt caching: the cache TTL is 5 min / 1 h, so a once-daily call always hits a cold cache and only pays the write premium for no read benefit. Caching is only worthwhile for calls made close together.

Model is `claude-sonnet-4-6` (not Opus) — summarize + classify is a simple, high-volume task where Opus would cost ~5x for no quality gain. Expected spend at default caps: roughly $0.10–0.20 per run (~$3–6/month).

If cost ever needs halving and latency doesn't matter (it doesn't — this runs on cron), the Batches API gives 50% off the same call. Out of scope for the MVP; note it as a future lever.

## Monitoring

- `claude.py` logs `usage.input_tokens` and `usage.output_tokens` to stdout on every call (GitHub Actions captures stdout). Eyeball these per run to catch drift.
- `summarize_and_classify` logs how many entries were dropped by the `MAX_ENTRIES_TO_CLAUDE` cap — a non-zero number day after day signals feed volume creeping up.
- `rss.py` logs each feed that errors (name + error) and continues. A feed that errors every run is a signal to fix or remove it.
- Optional guard: call `claude.messages.count_tokens` on the assembled prompt before the real call and `log + skip` (return 0) if it exceeds a sanity threshold, so a runaway feed can never trigger a surprise charge. Cheap insurance; add if feed sources are untrusted.

## Phase 2 — Evolved pipeline (this is what we build)

Phase 1 above describes a single Claude call over capped blurbs. We build the evolved version directly, because the calibration metrics below only make sense with it. Phase 1 text stays as the reference for the contracts that DON'T change — `config.py`, `rss.py`, `slack.py`, the `claude.call` wrapper, secrets, workflow. Only the job orchestration and a couple of constants change. Don't over-engineer; loose is fine, this is a personal tool we'll tune by watching it run.

### Two calls instead of one

1. **Triage (cheap).** Send all recent entries as `title + blurb + topic_hint`, plus the "already covered" list (see Dedup). One call. Ranks both axes and applies same-story suppression. Returns selected items (`url, topic, importance, content_potential`) — no summaries, no full text yet.
2. **Deepen (the expensive one).** For the union of selected items, get full text (see Full-text) and produce `summary` + `angle`. Returns the final `Digest`.

Two axes = two output *fields*, not two calls. Do not split triage into separate importance/content calls.

### Raised cap, no tag gate

- Replace `MAX_ENTRIES_TO_CLAUDE=60` with `MAX_ENTRIES_TO_TRIAGE` (default ~400) — a safety ceiling against a runaway feed, not a cost knob. Blurbs are ~125 tokens each; send essentially the whole day so triage sees everything.
- Do **not** hard-filter entries by `topic_hint` before triage. A coarse tag can't separate a generic story from the niche on-brand one; pre-gating kills the `content_potential` discovery that's the whole point. Tag is a signal in the prompt, not a filter.

### Full-text fetching — try easy, fall back to blurb

For each selected item, in order:
1. If the RSS entry already carries full content (`entry.content` / `content:encoded`), use it. Free, no fetch.
2. Else `httpx.get` the url (timeout ~10s, real User-Agent) and run `trafilatura.extract`.
3. Else — fetch failed / blocked / timed out / extracted text < ~200 chars (likely paywall or nav) — fall back to the entry blurb.

Never crash on a fetch failure: log and degrade. We only fetch the handful of selected items, so a couple of fallbacks just means a couple of weaker summaries. Add `trafilatura` to deps. No headless browser — JS-only pages degrade to blurb, acceptable.

### Dedup against what we shared (committed state)

The 24h window is adjacent day-to-day, so the same URL almost never recurs — the repetition that hurts is the same *story* via new URLs. So dedup semantically against what we actually delivered, not a URL set.

- **State:** `seen.json`, a rolling ~14-day list of delivered items: `{url, title, topic, gist, date}`. Only items we sent to Slack go in — if something was filtered out and later becomes the strongest item, we want it to resurface.
- **Where:** inject the seen list into the triage call; instruct the model to drop / down-rank items that are substantially the same story as anything in it. Folds into call 1 — no extra call, no embeddings.
- **After delivery:** append today's delivered items, prune to the window, save.
- **Storage:** `seen.json` is tracked in the repo. The scheduled workflow commits the updated file back after each real run (`permissions: contents: write`), so local and deployed runs share one inspectable dedup history. (Keep the existing within-run URL dedup in `rss.py` — that catches the same article across two feeds on the same day, which is a different and real case.)

## Metrics & Calibration

Emit a metrics summary at the end of every run so the first week or two can be tuned from the Actions logs. Two lines: one human-readable for scanning, one `METRICS {json}` for later grep/parse. Throwaway-friendly — the point is calibration, not observability infra. Trim once thresholds settle. Counts:

- `feeds_total`, `feeds_failed`
- `entries_found` (after window filter), `entries_after_url_dedup`
- `entries_to_triage` (+ `entries_dropped_cap` if the ceiling hit)
- `dropped_as_seen` — suppressed as already-covered (cross-day dedup working)
- `selected_importance`, `selected_content` — how many cleared each axis
- `fulltext_ok`, `fulltext_fallback_blurb` — how often fetching actually worked
- `digest_items`, `picks` — what shipped
- `claude_input_tokens`, `claude_output_tokens` (summed over both calls), `claude_cost_est`
- `duration_seconds`

These map straight to the knobs: `selected_*` clustering high → tighten triage prompt / raise thresholds; `dropped_as_seen` near zero → dedup isn't earning its keep; `fulltext_fallback_blurb` dominating → call 2 isn't worth much for your feeds.

## Prompt: `prompts/news.md`

Two sections separated by `---`. System above, user template below. The loader reads the file, splits on the first `---`, `.format(profile=...)`-s the system half (literal JSON braces in that half are doubled `{{ }}` so `.format` leaves them intact), and `.format(entries_json=...)`-s the user half.

```
You are producing a personal daily news digest for the reader described below. You receive RSS entries from the last 24 hours and return structured JSON.

## Reader profile
{profile}

## Your job
Write a short overview, then evaluate every entry on two INDEPENDENT axes.

overview: 2–3 sentences synthesizing the day for the reader above — the main themes and what's notable. No fluff.

For each entry, decide:
- summary: factual, 1–2 sentences, no marketing language, in the same language as the source title
- topic: short label (1–3 words). Reuse labels across entries — prefer "AI", "Macro", "Tech", "Product", "Data", "Geopolitics", "Science", "Career", "Misc". Invent one only if none fit, then reuse it.
- importance: integer 1 (trivia) to 5 (must-read for the reader). Be strict; 5 is rare.
- content_potential: integer 1 (nothing original to say) to 5 (excellent basis for the reader to write their own commentary, given their profile and audience). INDEPENDENT of importance — a major-but-generic story can be low; a niche story matching the reader's angle can be high.
- angle: for entries with content_potential >= 3, a single sentence suggesting the take or hook the reader could write from. Otherwise null.
- source: the original feed_name.

Output a JSON object:
{{"overview": "...", "items": [{{"title": "...", "summary": "...", "url": "...", "topic": "...", "importance": N, "content_potential": N, "angle": "..." or null, "source": "..."}}, ...]}}

No preamble, no code fences, no commentary. JSON only.

---

Entries (JSON):
{entries_json}
```

## Config: `config/feeds.yml`

Start with a small placeholder so the test passes; user replaces with real feeds.

```yaml
feeds:
  - name: Hacker News Front Page
    url: https://hnrss.org/frontpage
    topic_hint: Tech
  - name: TechCrunch
    url: https://techcrunch.com/feed/
  - name: Ars Technica
    url: http://feeds.arstechnica.com/arstechnica/index
  - name: MIT Technology Review
    url: https://www.technologyreview.com/feed/
    topic_hint: AI
  - name: Stratechery
    url: https://stratechery.com/feed/
    topic_hint: Product
  # Substacks: any publication's feed is <publication>.substack.com/feed
  # (or <custom-domain>/feed). Example:
  - name: Platformer
    url: https://www.platformer.news/feed
    topic_hint: Tech
  # Add more here
```

**Source notes.** Native RSS and Substack both slot straight in here (Substack feed = `<publication>.substack.com/feed`). Email-only sources without a public feed — **LinkedIn newsletters** and **TLDR** — are *not* RSS-addressable; the only clean path is an email-to-RSS bridge (e.g. Kill the Newsletter) set up per newsletter. Treat those as manual one-offs, not part of the core list. Don't build scraping into this job.

## Profile: `config/profile.md`

Plain markdown describing the reader, injected verbatim into the system prompt. It drives both `importance` (relevant to me) and `content_potential` / `angle` (matches what I write about). Keep it short and concrete — a few lines beat a page. Starter content the user edits:

```markdown
# Reader profile

- Role: founder of a data consulting firm + a SaaS for e-commerce.
- Domain: data, AI, business intelligence — practical, no-hype.
- Audience for my content: founders and data/BI practitioners at e-com companies.
- Weight HIGH: applied LLMs, data tooling/infrastructure, BI/analytics, e-commerce data, AI that changes how teams work.
- Weight LOW: consumer gadgets, celebrity/funding-gossip, crypto price moves, generic "AI will change everything" takes.
- Content angles I write from: turning hype into "what this means for your data stack", contrarian takes on BI tooling, practical applied-AI for e-com.
```

If `config/profile.md` is absent, the job still runs but picks/angles degrade to generic — `load_profile` logs a warning.

## Secrets

Register these in GitHub repo → Settings → Secrets and variables → Actions:

- `ANTHROPIC_API_KEY`
- `SLACK_WEBHOOK_URL`

The workflow exposes them as env vars. No `.env` file in repo. `.gitignore` should include `.env` anyway.

## GitHub Actions Workflow: `.github/workflows/news-digest.yml`

```yaml
name: News Digest

on:
  schedule:
    - cron: "30 6 * * *"  # 07:30 London time during BST; user adjusts
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Run without sending to Slack"
        required: false
        default: "false"

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -e .
      - name: Run news digest
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          GITHUB_SERVER_URL: ${{ github.server_url }}
          GITHUB_REPOSITORY: ${{ github.repository }}
          GITHUB_RUN_ID: ${{ github.run_id }}
        run: |
          if [ "${{ github.event.inputs.dry_run }}" = "true" ]; then
            python -m src.jobs.news --dry-run
          else
            python -m src.jobs.news
          fi
```

## Tests

### `tests/test_rss.py`

- `respx` mocks two feed URLs with sample Atom/RSS XML
- Asserts entries within window are returned, out-of-window are dropped
- Asserts a feed that 500s is logged and skipped, not raised

### `tests/test_claude.py`

- Mocks Anthropic API with `respx` (the SDK uses `httpx` under the hood)
- Asserts retry on 429 then 200 succeeds
- Asserts JSON mode strips code fences

### `tests/test_slack.py`

- Mocks the webhook URL
- Asserts payload shape and that a non-2xx raises

### `tests/test_news_job.py`

- End-to-end: monkeypatch `load_feeds` to return one synthetic feed and `load_profile` to return a fixed string, mock RSS, mock Claude returning a known JSON (`{"overview": ..., "items": [...]}` with `content_potential` and `angle` populated), mock Slack
- Run `news.run(dry_run=True)` and assert exit 0; assert the printed Block Kit JSON contains the Overview, a "Worth a take" pick (with its angle), and the topic-grouped digest
- Assert an item with `content_potential >= CONTENT_POTENTIAL_FLOOR` but `importance < IMPORTANCE_THRESHOLD` appears as a pick but not in the digest (the two axes are independent)
- Separately, force `summarize_and_classify` to raise and assert `send_error` is called and exit is 1

All tests must pass under `pytest -q` with no network access. Lint must pass under `ruff check`.

## `pyproject.toml`

Minimal, using setuptools. Dependencies pinned to minor versions:

```toml
[project]
name = "morning-bot"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "anthropic>=0.40,<1.0",
  "feedparser>=6.0,<7.0",
  "httpx>=0.27,<1.0",
  "pydantic>=2.6,<3.0",
  "pyyaml>=6.0,<7.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "respx>=0.21",
  "ruff>=0.6",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

## README.md

Cover:
- One-liner what this is
- Setup: clone, `pip install -e ".[dev]"`, set local env vars in a `.env` (loaded manually if developing), run `pytest`
- How to add a feed (edit `config/feeds.yml`, commit, push — next run picks it up)
- How to trigger manually: Actions tab → News Digest → Run workflow (with optional dry_run)
- How to add a new job in the future (link to "future jobs follow the same pattern: a file in `src/jobs/`, a workflow in `.github/workflows/`, optional libs in `src/lib/`")
- Secrets list and where to set them

Keep README under 150 lines. Aimed at a single developer who will revisit this in 3 months and need to remember how it works.

## Build Order

Build and verify in this order. Don't move on until the current step works.

1. Repo skeleton (`pyproject.toml`, package layout, empty modules) + one passing trivial test (`def test_smoke(): assert True`)
2. `lib/config.py` (`load_feeds` + `load_profile`) + `tests/test_config.py` with a fixture YAML and a fixture profile (incl. the missing-profile → `""` + warning case)
3. `lib/rss.py` + `tests/test_rss.py` with mocked feeds
4. `lib/claude.py` + `tests/test_claude.py` with mocked Anthropic
5. `lib/slack.py` + `tests/test_slack.py` with mocked webhook
6. `prompts/news.md` and `config/profile.md` (create both with the content above)
7. `jobs/news.py` glue + `tests/test_news_job.py` end-to-end dry-run test
8. Workflow file. Push to GitHub, set the two secrets, trigger via `workflow_dispatch` with `dry_run=true`, confirm stdout shows Block Kit JSON
9. Trigger again with `dry_run=false`, confirm message appears in Slack
10. Leave cron enabled; verify next morning that the scheduled run fires

## Coding Conventions

- Type hints everywhere; mypy-clean is not required but `from __future__ import annotations` at the top of each module
- Functions over classes unless state is genuinely needed
- No print/log libraries beyond stdlib `logging` for warnings/errors; plain `print` for normal progress (GitHub Actions captures stdout)
- No global mutable state; pass config and clients into functions
- Module-level constants in UPPER_SNAKE; env-overridable as shown above
- No `try/except` that silently swallows — either re-raise after logging, or handle a specific exception with a comment explaining why

## Out of Scope (Do Not Build)

- Telegram delivery
- Multiple Slack channels / routing
- Persistent storage of seen items (we accept some repetition; this is a digest, not a feed reader)
- HTML scraping beyond what `feedparser` returns — *except* the bounded full-text fetch for already-selected items in Phase 2 (one `httpx.get` + `trafilatura.extract`, blurb fallback). No crawling, no headless browser, no following links.
- Image handling
- User-specific personalization (one user, one config, one channel)
- Web UI, dashboard, status page
- Anything mentioned in the larger morning-bot spec that isn't in THIS document

When in doubt, build less. The next job will reuse libs and patterns from this one; keep them small and correct rather than feature-rich.