"""Слой моделей: чат-LLM для агентов и эмбеддинги для pgvector."""
from __future__ import annotations

from typing import Any

from .base import BaseLLM, LLMError, NoLLM, Reply, Usage
from .embeddings import (BaseEmbedder, EmbeddingError, HashEmbedder,
                         OpenAIEmbedder, build_embedder, cosine)
from .openai_like import OpenAILike


def build_llm(provider: str, model: str, **kwargs: Any) -> BaseLLM:
    """Фабрика провайдера. `none` — явная заглушка, а не тихая пустышка."""
    key = (provider or "none").strip().lower()
    if key in ("none", "off", "disabled", ""):
        return NoLLM()
    if key in ("openai_like", "openai", "openrouter", "lmstudio", "llamacpp",
               "vllm", "ollama", "local"):
        return OpenAILike(model, **kwargs)
    if key in ("stub", "fake", "offline"):
        from .stub import StubLLM
        for name in ("base_url", "api_key", "timeout", "temperature"):
            kwargs.pop(name, None)
        return StubLLM(model or "stub", **kwargs)
    raise LLMError(
        f"Неизвестный провайдер LLM {provider!r}. Доступны: none, openai_like "
        "(плюс синонимы openai/openrouter/lmstudio/llamacpp/vllm/ollama), stub.")


__all__ = ["BaseLLM", "LLMError", "Reply", "Usage", "NoLLM", "OpenAILike",
           "build_llm", "BaseEmbedder", "HashEmbedder", "OpenAIEmbedder",
           "EmbeddingError", "build_embedder", "cosine"]
