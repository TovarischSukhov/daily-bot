from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

POST_TIMEOUT = 15.0


def _run_url() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repo = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def send_blocks(blocks: list[dict], fallback_text: str) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]
    resp = httpx.post(
        url,
        json={"text": fallback_text, "blocks": blocks},
        timeout=POST_TIMEOUT,
    )
    resp.raise_for_status()  # fail loudly if Slack rejects the payload


def send_error(job_name: str, error: str) -> None:
    url = os.environ["SLACK_WEBHOOK_URL"]
    text = f":red_circle: *{job_name}* failed\n```{error[:500]}```"
    run_url = _run_url()
    if run_url:
        text += f"\n<{run_url}|View run>"
    resp = httpx.post(url, json={"text": text}, timeout=POST_TIMEOUT)
    resp.raise_for_status()
