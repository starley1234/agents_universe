"""Фабрика LLM.

Главная идея: пайплайны никогда не импортируют конкретного провайдера.
Они просят `get_llm()` и получают любой chat-model LangChain.
Провайдер `fake` — детерминированная заглушка: тесты и демо работают
без ключей и без сети.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .config import Settings, settings as default_settings


class EchoChatModel(BaseChatModel):
    """Оффлайн-модель: возвращает валидный JSON-скелет или эхо промпта.

    Нужна, чтобы граф целиком проходился в CI без внешних вызовов.
    Если в промпте есть маркер `JSON_SCHEMA_HINT:` — отдаёт этот скелет,
    поэтому парсеры пайплайнов получают ожидаемую структуру.
    """

    canned: dict[str, str] = {}

    @property
    def _llm_type(self) -> str:
        return "echo"

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = "\n".join(str(m.content) for m in messages)
        for key, value in self.canned.items():
            if key in text:
                return _result(value)
        hint = _extract_hint(text)
        if hint is not None:
            return _result(json.dumps(hint, ensure_ascii=False))
        return _result("[offline-llm] " + text.strip()[-500:])


def _result(content: str) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _extract_hint(text: str) -> Any | None:
    marker = "JSON_SCHEMA_HINT:"
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1].strip()
    depth, end = 0, None
    for i, ch in enumerate(tail):
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(tail[:end])
    except json.JSONDecodeError:
        return None


def get_llm(cfg: Settings | None = None, **overrides: Any) -> BaseChatModel:
    """Вернуть chat-model по настройкам среды."""
    cfg = cfg or default_settings()
    provider = overrides.pop("provider", cfg.provider).lower()
    model = overrides.pop("model", None) or cfg.resolved_model()

    if provider in ("fake", "echo", "offline"):
        return EchoChatModel(**overrides)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            temperature=cfg.temperature,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            **overrides,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            api_key=cfg.api_key,
            **overrides,
        )

    if provider == "ollama":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            temperature=cfg.temperature,
            base_url=cfg.base_url or "http://localhost:11434/v1",
            api_key=cfg.api_key or "ollama",
            **overrides,
        )

    raise ValueError(f"неизвестный провайдер LLM: {provider!r}")
