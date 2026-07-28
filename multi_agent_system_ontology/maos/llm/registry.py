"""Реестр LLM-провайдеров и разбор ссылок вида `provider::model_name`.

Синтаксис из ТЗ: `local::llama3`, `openrouter::anthropic/claude-3`. Модель
может содержать двоеточия и слэши (например openrouter-имена вида
`anthropic/claude-3` или `deepseek/deepseek-chat:free`) — поэтому
разделитель ссылки на провайдера ИМЕННО ДВОЙНОЕ двоеточие `::`, а не
одиночное, иначе имя модели с `:` (частое у OpenRouter) было бы
неотличимо от разделителя.
"""
from __future__ import annotations

import os
from typing import Any

from .base import BaseLLM, LLMError
from .openai_like import OpenAILike

# провайдер -> (класс драйвера, base_url по умолчанию, переменная окружения
# ключа, платный ли по умолчанию, окно контекста по умолчанию в токенах)
_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "local": {
        "base_url_env": "LOCAL_BASE_URL",
        "base_url_default": "http://localhost:11434/v1",
        "api_key_env": "LOCAL_API_KEY",
        "billable": False,
        "context_window": 8192,
    },
    "openrouter": {
        "base_url_env": "OPENROUTER_BASE_URL",
        "base_url_default": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "billable": True,
        "context_window": 128_000,
    },
    "openai": {
        "base_url_env": "OPENAI_BASE_URL",
        "base_url_default": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "billable": True,
        "context_window": 128_000,
    },
}

_ALIASES = {"lmstudio": "local", "llamacpp": "local", "vllm": "local",
            "ollama": "local"}

_DRIVER: dict[str, type[BaseLLM]] = {name: OpenAILike for name in _PROVIDER_DEFAULTS}


def known_providers() -> list[str]:
    return sorted(_PROVIDER_DEFAULTS)


def parse_model_ref(ref: str) -> tuple[str, str]:
    """`provider::model` -> (provider, model). Кидает LLMError на кривой формат.

    Разделитель — `::` (двойное двоеточие), а не одиночное: имена моделей
    OpenRouter часто сами содержат `:` (например
    `deepseek/deepseek-chat:free`), и одиночный разделитель был бы
    неоднозначен.
    """
    if not ref or "::" not in ref:
        raise LLMError(
            f"Ссылка на модель {ref!r} должна быть в формате "
            "'provider::model_name', например 'local::llama3'."
        )
    provider, _, model = ref.partition("::")
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise LLMError(f"Пустая часть в ссылке на модель {ref!r}")
    return _ALIASES.get(provider, provider), model


def format_model_ref(provider: str, model: str) -> str:
    return f"{provider}::{model}"


def provider_billable(provider: str) -> bool:
    key = _ALIASES.get(provider, provider)
    info = _PROVIDER_DEFAULTS.get(key)
    return bool(info["billable"]) if info else True


def provider_context_window(provider: str) -> int:
    key = _ALIASES.get(provider, provider)
    info = _PROVIDER_DEFAULTS.get(key)
    return int(info["context_window"]) if info else 8192


def build_llm(provider: str, model: str, **overrides: Any) -> BaseLLM:
    """Собрать драйвер по имени провайдера (без ::-парсинга, для build_from_ref)."""
    key = _ALIASES.get((provider or "").strip().lower(), (provider or "").strip().lower())
    info = _PROVIDER_DEFAULTS.get(key)
    if info is None:
        raise LLMError(
            f"Неизвестный провайдер {provider!r}. Доступны: "
            f"{', '.join(known_providers())} (и алиасы {', '.join(sorted(_ALIASES))})"
        )
    cls = _DRIVER[key]
    base_url = overrides.pop("base_url", None) or os.getenv(info["base_url_env"]) \
        or info["base_url_default"]
    api_key = overrides.pop("api_key", None)
    if api_key is None:
        api_key = os.getenv(info["api_key_env"])
    kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key,
                              "context_window": info["context_window"]}
    kwargs.update({k: v for k, v in overrides.items() if v is not None})
    llm = cls(model=model, **kwargs)
    llm.billable = info["billable"]
    return llm


def build_from_ref(ref: str, **overrides: Any) -> BaseLLM:
    provider, model = parse_model_ref(ref)
    return build_llm(provider, model, **overrides)
