"""Behaviour of the OpenAI-compatible LLM provider.

These tests never touch the network: ``httpx.post`` is replaced. They exist
because a real provider returned HTTP 200 with an empty ``message.content`` and
the old code reported it as a generic failure, which made the cause impossible
to find. Each empty shape now reports itself.
"""
from __future__ import annotations

import httpx
import pytest

from jarvismind.services.llm.base import LLMError
from jarvismind.services.llm.openai_compatible import OpenAICompatibleProvider


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="https://example.invalid",
        api_key="sk-test",
        model="test-model",
    )


@pytest.fixture
def respond(monkeypatch):
    def install(data, status_code: int = 200):
        class _Response:
            def __init__(self) -> None:
                self.status_code = status_code

            def json(self):
                return data

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response())

    return install


def _reply(message: dict, finish: str = "stop", usage: dict | None = None) -> dict:
    return {
        "model": "test-model",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": usage or {},
    }


def test_plain_string_content(respond):
    respond(_reply({"content": "你好"}))
    result = _provider().complete([{"role": "user", "content": "hi"}])
    assert result.text == "你好"


def test_content_parts_are_joined(respond):
    respond(
        _reply(
            {
                "content": [
                    {"type": "text", "text": "分片一"},
                    {"type": "text", "text": "分片二"},
                ]
            }
        )
    )
    result = _provider().complete([{"role": "user", "content": "hi"}])
    assert result.text == "分片一分片二"


def test_truncated_reply_names_the_token_limit(respond):
    respond(_reply({"content": ""}, finish="length", usage={"completion_tokens": 800}))
    with pytest.raises(LLMError) as caught:
        _provider().complete([{"role": "user", "content": "hi"}])
    assert "token limit" in str(caught.value)
    assert "JARVISMIND_LLM_MAX_TOKENS" in str(caught.value)


def test_reasoning_only_reply_is_named(respond):
    respond(_reply({"content": "", "reasoning_content": "思考过程"}))
    with pytest.raises(LLMError) as caught:
        _provider().complete([{"role": "user", "content": "hi"}])
    assert "reasoning" in str(caught.value)


def test_plain_empty_reply_is_named(respond):
    respond(_reply({"content": ""}))
    with pytest.raises(LLMError) as caught:
        _provider().complete([{"role": "user", "content": "hi"}])
    assert "empty answer text" in str(caught.value)


def test_missing_message_is_malformed(respond):
    respond({"choices": [{"finish_reason": "stop"}], "usage": {}})
    with pytest.raises(LLMError) as caught:
        _provider().complete([{"role": "user", "content": "hi"}])
    assert "malformed" in str(caught.value)


def test_error_messages_never_echo_the_reply_body(respond):
    respond(_reply({"content": ""}), status_code=500)
    with pytest.raises(LLMError) as caught:
        _provider().complete([{"role": "user", "content": "secret memory text"}])
    assert "secret memory text" not in str(caught.value)
