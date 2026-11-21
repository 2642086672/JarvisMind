"""Build the configured LLM provider.

When no provider is configured the extraction engine uses its deterministic
local strategy instead; the service never pretends a call was made.
"""
from __future__ import annotations

from typing import Optional

from ...config import Settings
from .base import LLMProvider
from .openai_compatible import OpenAICompatibleProvider


def is_configured(settings: Settings) -> bool:
    return bool(settings.llm_base_url and settings.llm_model)


def build_provider(settings: Settings) -> Optional[LLMProvider]:
    if not is_configured(settings):
        return None
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        name=settings.llm_provider_name or "openai_compatible",
        price_per_1k_input_cny=settings.llm_price_in,
        price_per_1k_output_cny=settings.llm_price_out,
        http_timeout=settings.llm_timeout,
    )
