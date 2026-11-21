"""OpenAI-compatible chat-completions provider (HTTP, via httpx).

Works with any endpoint that implements ``POST {base_url}/chat/completions``:
OpenAI, DeepSeek, GLM, Moonshot, a local vLLM/Ollama gateway, or a relay.
The vendor is configuration, never code.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from .base import LLMError, LLMProvider, LLMResult


def _extract_text(message: Any) -> str:
    """Pull the assistant text out of a chat-completions ``message``.

    Some OpenAI-compatible endpoints return content as a list of parts rather
    than a plain string (``[{"type": "text", "text": "..."}]``). Treating that
    as empty would silently throw away a perfectly good answer.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _describe_empty(message: Any, data: Dict[str, Any], usage: Dict[str, Any]) -> str:
    """Explain *why* the reply had no text; never echoes memory content."""
    if not isinstance(message, dict):
        return "LLM returned a message that is not an object"
    finish = ""
    try:
        finish = str(data["choices"][0].get("finish_reason") or "")
    except (KeyError, IndexError, TypeError):
        finish = ""
    reasoning = message.get("reasoning_content")
    has_reasoning = isinstance(reasoning, str) and len(reasoning) > 0
    output_tokens = usage.get("completion_tokens")
    if finish == "length":
        return (
            "LLM hit the token limit before writing an answer "
            f"(completion_tokens={output_tokens}); raise JARVISMIND_LLM_MAX_TOKENS"
        )
    if has_reasoning:
        return "LLM returned reasoning content but no answer text"
    return "LLM returned empty answer text"


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        name: str = "openai_compatible",
        price_per_1k_input_cny: float = 0.0,
        price_per_1k_output_cny: float = 0.0,
        http_timeout: float = 20.0,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not base_url:
            raise LLMError("LLM base_url is not configured")
        if not model:
            raise LLMError("LLM model is not configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name
        self.price_input = price_per_1k_input_cny
        self.price_output = price_per_1k_output_cny
        self.http_timeout = http_timeout
        self.extra_body = extra_body or {}

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        timeout: float = 20.0,
        max_tokens: int = 800,
        temperature: float = 0.2,
    ) -> LLMResult:
        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **self.extra_body,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=min(timeout, self.http_timeout),
            )
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM request timed out after {timeout:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc

        if response.status_code == 401:
            raise LLMError("LLM rejected the API key (401)")
        if response.status_code == 429:
            raise LLMError("LLM rate limit exceeded (429)")
        if response.status_code >= 400:
            # Never echo the response body: it can echo the request, which may
            # contain conversation content.
            raise LLMError(f"LLM returned HTTP {response.status_code}")

        try:
            data = response.json()
            message = data["choices"][0]["message"]
            text = _extract_text(message)
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError("LLM returned a malformed response") from exc

        if not isinstance(text, str) or not text.strip():
            # Distinguish the common causes instead of reporting "empty".
            raise LLMError(_describe_empty(message, data, usage))

        return LLMResult(
            text=text,
            model=data.get("model", self.model),
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            raw={"id": data.get("id")},
        )

    def estimate_cost_cny(self, result: LLMResult) -> float:
        return (
            result.input_tokens / 1000.0 * self.price_input
            + result.output_tokens / 1000.0 * self.price_output
        )
