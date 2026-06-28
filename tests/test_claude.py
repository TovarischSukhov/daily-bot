from __future__ import annotations

import httpx
import pytest
import respx

from src.lib import claude

API = "https://api.anthropic.com/v1/messages"


def _message(text: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


@pytest.fixture(autouse=True)
def _env_and_no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(claude.time, "sleep", lambda _s: None)


@respx.mock
def test_retry_on_429_then_success() -> None:
    route = respx.post(API).mock(
        side_effect=[
            httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit"}}),
            httpx.Response(200, json=_message("hi")),
        ]
    )
    resp = claude.call(system="s", user="u")
    assert resp.text == "hi"
    assert resp.input_tokens == 11
    assert route.call_count == 2


@respx.mock
def test_json_mode_strips_fences() -> None:
    respx.post(API).mock(
        return_value=httpx.Response(200, json=_message('```json\n{"a": 1}\n```'))
    )
    resp = claude.call(system="s", user="u", response_format="json")
    assert resp.text == '{"a": 1}'
