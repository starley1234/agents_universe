"""Контракт LLM для агентского слоя: chat(messages) -> Reply.

Узкий интерфейс намеренно: агенты САПС не вызывают инструменты через
модель и не ведут диалог — они задают один структурированный вопрос и
разбирают структурированный ответ. Всё, что нужно от провайдера, —
отдать текст и честно сказать, сколько это стоило.

temperature=0 по умолчанию в конфиге: в сертификации важна
воспроизводимость. Один и тот же разбор требования, запущенный дважды,
должен давать одинаковый результат, иначе инженер не сможет объяснить
регулятору, почему вчера агент считал формулировку измеримой, а сегодня
нет.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """Ошибка обращения к модели. retryable — имеет смысл повторить."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.tokens_in + other.tokens_in,
                     self.tokens_out + other.tokens_out)


@dataclass
class Reply:
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class BaseLLM:
    name = "base"

    def __init__(self, model: str, *, retries: int = 2,
                 retry_base: float = 1.0, **kwargs: Any) -> None:
        self.model = model
        self.retries = max(0, int(retries))
        self.retry_base = max(0.0, float(retry_base))
        self.calls = 0
        self.usage = Usage()

    def _chat_once(self, messages: list[dict[str, Any]]) -> Reply:
        raise NotImplementedError

    def chat(self, messages: list[dict[str, Any]]) -> Reply:
        last: LLMError | None = None
        for attempt in range(self.retries + 1):
            try:
                reply = self._chat_once(messages)
            except LLMError as exc:
                last = exc
                if not exc.retryable or attempt >= self.retries:
                    raise
                time.sleep(self.retry_base * (2 ** attempt))
                continue
            self.calls += 1
            self.usage = self.usage + reply.usage
            if not reply.model:
                reply.model = self.model
            return reply
        raise last or LLMError("Модель не ответила")

    def describe(self) -> str:
        return f"{self.name}:{self.model}"


class NoLLM(BaseLLM):
    """Заглушка «модель не настроена».

    Не молчаливая: агент, которому нужна модель, получит понятный отказ
    с указанием, что настроить. Тихая выдача пустого результата в
    системе сертификации недопустима — инженер решит, что замечаний нет.
    """

    name = "none"

    def __init__(self, model: str = "none", **kwargs: Any) -> None:
        super().__init__(model, retries=0)

    def _chat_once(self, messages: list[dict[str, Any]]) -> Reply:
        raise LLMError(
            "LLM не настроена (SAPS_LLM_PROVIDER=none). Агенты, которым нужна "
            "модель (Редактор, Классификатор в режиме LLM), не могут работать. "
            "Укажите SAPS_LLM_PROVIDER=openai_like и SAPS_LLM_API_KEY, либо "
            "используйте детерминированные проверки — они работают без модели.")
