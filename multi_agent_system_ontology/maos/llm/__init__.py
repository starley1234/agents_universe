"""LLM-слой MAOS: base-протокол, драйверы, реестр provider::model, эмбеддинги."""
from __future__ import annotations

from .base import BaseLLM, LLMError, LLMReply, Usage
from .registry import (build_from_ref, build_llm, format_model_ref,
                       known_providers, parse_model_ref, provider_billable,
                       provider_context_window)

__all__ = [
    "BaseLLM", "LLMError", "LLMReply", "Usage",
    "build_from_ref", "build_llm", "format_model_ref", "known_providers",
    "parse_model_ref", "provider_billable", "provider_context_window",
]
