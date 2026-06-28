# morning-bot

Daily news digest: RSS → Claude summarize/classify → Slack Block Kit. MVP of a
larger personal-automation system. Full spec: [docs/SPEC.md](docs/SPEC.md).

## Tone
- Short and straight to the point. Lead with the answer, no preamble or wrap-up.

## Hard rules
- Build ONLY the news job. Do not scaffold future jobs (plan/health/leads) or
  unused libs. When in doubt, build less.
- Libs must be reusable by importing, not refactoring — keep them small.
- Python 3.11+, functional style, functions over classes, no global mutable state.
- `from __future__ import annotations` at top of every module.
- Type hints everywhere. mypy-clean not required.
- Logging: stdlib `logging` for warnings/errors, plain `print` for progress.
- No try/except that silently swallows — re-raise after logging, or comment why.
- Bot's Anthropic model is `claude-sonnet-4-6` (cheap, daily, simple summarize +
  classify task — Opus is overkill here). Change only if quality proves short.

## Workflow
- Build in the order in SPEC.md "Build Order"; verify each step before moving on.
- ALWAYS run `pytest -q` AND `ruff check` before declaring work done.
- Tests must pass with no network access. The HTTP client is `httpx`, so mock
  with `respx` (not `responses`, which only patches `requests`).

## Git
- Never add `Co-Authored-By` or any Claude/AI attribution to commit messages or
  PR bodies. Plain messages only.

## Secrets
- `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL` live in GitHub Actions secrets.
- Never commit `.env`. Never hardcode keys.
