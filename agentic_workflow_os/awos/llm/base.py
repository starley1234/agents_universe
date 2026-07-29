"""Контракт модели для среды: chat(messages) -> Reply. И ничего больше.

ПОЧЕМУ ИНТЕРФЕЙС ТАКОЙ УЗКИЙ. Среда не строит агентов — она их
запускает. Всё, что ей нужно от модели: отдать текст на набор сообщений
и честно сказать, сколько это стоило. Вызовы инструментов среда
разбирает САМА из текста ответа (см. awos/tools/protocol.py), а не
полагается на нативный tool-calling: платформа обязана работать с
локальной 7B-моделью в LM Studio ровно так же, как с облачной, а
поддержка tools там неровная и местами сломанная. Цена решения —
несколько процентов точности разбора; выгода — один код исполнения для
всех моделей и полный контроль над тем, что реально было вызвано.

Retry живёт здесь же: разовый обрыв сети не должен ронять прогон,
который человек ждёт полчаса. Неретраибельные ошибки (400, битый ключ)
пробрасываются сразу — повторять их бессмысленно.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """Ошибка обращения к модели.

    retryable=True — сбой, который имеет смысл повторить (сеть, 429, 5xx).
    """

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
    """Базовый класс: retry и учёт расхода — общие для всех провайдеров."""

    name = "base"

    def __init__(self, model: str, *, retries: int = 2,
                 retry_base: float = 1.0, **kwargs: Any) -> None:
        self.model = model
        self.retries = max(0, int(retries))
        self.retry_base = max(0.0, float(retry_base))
        self.calls = 0
        self.usage = Usage()

    # Реализуется провайдером.
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
                # Экспоненциальная пауза: 1с, 2с, 4с. Провайдеры лечат
                # всплески лимитов именно паузой, а не немедленным повтором.
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
