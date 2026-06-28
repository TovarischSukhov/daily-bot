from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Literal

import anthropic

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE = 2.0  # seconds; sleep = BACKOFF_BASE * 2**attempt
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")

JSON_INSTRUCTION = (
    "\n\nRespond with valid JSON only. No preamble, no commentary, no markdown code fences."
)


@dataclass(frozen=True)
class ClaudeResponse:
    text: str
    input_tokens: int
    output_tokens: int


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _retryable(err: anthropic.APIStatusError) -> bool:
    return err.status_code == 429 or err.status_code >= 500


def call(
    system: str,
    user: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
    response_format: Literal["text", "json"] = "text",
) -> ClaudeResponse:
    # max_retries=0: our own loop below owns retry/backoff, no double-retry.
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0)
    if response_format == "json":
        system = system + JSON_INSTRUCTION

    last_err: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            break
        except anthropic.APIStatusError as e:
            if not _retryable(e) or attempt == MAX_ATTEMPTS - 1:
                raise
            last_err = e
            sleep = BACKOFF_BASE * (2**attempt)
            logger.warning("claude %s; retry %d/%d in %.0fs", e.status_code, attempt + 1,
                           MAX_ATTEMPTS, sleep)
            time.sleep(sleep)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_err  # type: ignore[misc]

    text = "".join(block.text for block in msg.content if block.type == "text")
    if response_format == "json":
        text = _strip_fences(text)
        json.loads(text)  # validate; caller re-parses into its own model

    print(f"claude usage: input={msg.usage.input_tokens} output={msg.usage.output_tokens}")
    return ClaudeResponse(text, msg.usage.input_tokens, msg.usage.output_tokens)
