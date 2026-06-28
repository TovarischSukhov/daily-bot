from __future__ import annotations

import httpx
import pytest
import respx

from src.lib import slack

HOOK = "https://hooks.slack.com/services/T/B/xxx"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)


@respx.mock
def test_send_blocks_payload() -> None:
    route = respx.post(HOOK).mock(return_value=httpx.Response(200, text="ok"))
    slack.send_blocks([{"type": "divider"}], fallback_text="fb")
    body = route.calls[0].request.content
    assert b'"fb"' in body
    assert b"divider" in body


@respx.mock
def test_send_blocks_raises_on_non_2xx() -> None:
    respx.post(HOOK).mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(httpx.HTTPStatusError):
        slack.send_blocks([], fallback_text="fb")


@respx.mock
def test_send_error_includes_run_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    route = respx.post(HOOK).mock(return_value=httpx.Response(200, text="ok"))
    slack.send_error("news-digest", "boom")
    body = route.calls[0].request.content
    assert b"news-digest" in body
    assert b"github.com/me/repo/actions/runs/42" in body
