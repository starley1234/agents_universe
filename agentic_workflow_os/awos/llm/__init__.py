"""Провайдеры моделей + фабрика build_llm(...).

Провайдеров ровно два, и это осознанно: `openai_like` покрывает весь
рынок OpenAI-совместимых эндпоинтов (OpenAI, OpenRouter, LM Studio,
llama.cpp, vLLM, Ollama /v1), `stub` — офлайн-детерминизм для тестов и
демо. Добавлять драйвер под каждый облачный API — задача агента, а не
среды: платформе достаточно контракта chat(messages) -> Reply.
"""
from __future__ import annotations

from typing import Any

from .base import BaseLLM, LLMError, Reply, Usage
from .openai_like import OpenAILike
from .stub import StubLLM

PROVIDERS = ("openai_like", "stub")


def build_llm(provider: str, model: str, **kwargs: Any) -> BaseLLM:
    """Собрать провайдера по имени. Неизвестное имя — понятная ошибка."""
    key = (provider or "").strip().lower()
    # Частые синонимы: человек пишет то, чем реально пользуется.
    if key in ("openai", "openrouter", "lmstudio", "llamacpp", "vllm",
               "ollama", "local", "openai_like", ""):
        return OpenAILike(model, **kwargs)
    if key in ("stub", "fake", "offline"):
        kwargs.pop("base_url", None)
        kwargs.pop("api_key", None)
        kwargs.pop("timeout", None)
        kwargs.pop("temperature", None)
        return StubLLM(model or "stub", **kwargs)
    raise LLMError(
        f"Неизвестный провайдер {provider!r}. Доступны: {', '.join(PROVIDERS)} "
        "(плюс синонимы openai/openrouter/lmstudio/llamacpp/vllm/ollama/local "
        "— все они OpenAI-совместимы и отличаются только base_url).")


__all__ = ["BaseLLM", "LLMError", "Reply", "Usage", "OpenAILike", "StubLLM",
           "build_llm", "PROVIDERS"]
