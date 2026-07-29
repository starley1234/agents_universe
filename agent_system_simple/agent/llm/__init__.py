"""Фабрика драйверов модели: одна строка в конфиге переключает провайдера."""
from __future__ import annotations

from typing import Any

from .anthropic import Anthropic
from .base import BaseLLM, LLMError, LLMReply, ToolCall
from .ollama import Ollama
from .openai_like import OpenAILike

# Алиасы: несколько привычных имён на один драйвер, чтобы конфиг
# не приходилось подгонять под наши внутренние названия.
_REGISTRY: dict[str, type[BaseLLM]] = {
    "openai": OpenAILike,
    "openai_like": OpenAILike,
    "openrouter": OpenAILike,
    "vllm": OpenAILike,
    "lmstudio": OpenAILike,
    "llamacpp": OpenAILike,
    "anthropic": Anthropic,
    "claude": Anthropic,
    "ollama": Ollama,
}


def available() -> list[str]:
    return sorted(_REGISTRY)


def build_llm(provider: str, model: str, **kwargs: Any) -> BaseLLM:
    key = (provider or "").strip().lower()
    if key not in _REGISTRY:
        raise LLMError(
            f"Неизвестный провайдер {provider!r}. Доступны: {', '.join(available())}"
        )
    cls = _REGISTRY[key]
    # Отсекаем None, чтобы не затирать значения по умолчанию в драйвере.
    clean = {k: v for k, v in kwargs.items() if v is not None}
    return cls(model=model, **clean)


__all__ = [
    "BaseLLM", "LLMError", "LLMReply", "ToolCall",
    "OpenAILike", "Anthropic", "Ollama",
    "build_llm", "available",
]
