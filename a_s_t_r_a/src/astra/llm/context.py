"""Context-local LLM override — for per-request provider/model switching."""

from __future__ import annotations

import contextvars
from typing import Optional

# Context vars to allow per-request override (e.g. from UI cookie)
llm_provider_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("llm_provider_override", default=None)
llm_model_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("llm_model_override", default=None)
llm_url_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("llm_url_override", default=None)


def set_llm_override(provider: Optional[str] = None, model: Optional[str] = None, url: Optional[str] = None):
    """Set override for current context, returns tokens to reset."""
    tokens = []
    if provider is not None:
        tokens.append((llm_provider_override, llm_provider_override.set(provider)))
    if model is not None:
        tokens.append((llm_model_override, llm_model_override.set(model)))
    if url is not None:
        tokens.append((llm_url_override, llm_url_override.set(url)))
    return tokens


def reset_llm_override(tokens):
    for var, token in tokens:
        var.reset(token)


def get_overrides():
    return {
        "provider": llm_provider_override.get(),
        "model": llm_model_override.get(),
        "url": llm_url_override.get(),
    }
