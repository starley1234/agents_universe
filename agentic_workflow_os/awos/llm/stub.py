"""StubLLM — детерминированный офлайн-провайдер: демо, тесты, отладка.

ЗАЧЕМ ОН НУЖЕН СРЕДЕ, А НЕ ТОЛЬКО ТЕСТАМ. Платформу приходится
показывать и проверять там, где нет ни ключа, ни локальной модели: CI,
первый запуск («поставил и хочу увидеть, что оно живое»), отладка
самого workflow — правильно ли расставлены плейсхолдеры, срабатывает
ли точка контроля. Настоящая модель в этих сценариях только мешает:
она медленная, платная и НЕДЕТЕРМИНИРОВАННАЯ, а проверять надо среду,
а не качество текста.

Stub отвечает по простым правилам, зависящим от роли в системном
промпте: Исполнитель выдаёт заготовку с эхом задачи, Критик — валидный
JSON-вердикт, Контролёр — решение. Этого достаточно, чтобы прогнать
весь цикл качества, включая доработку и эскалацию к человеку.

Сценарии (scripted) позволяют тесту заранее задать точные ответы —
тогда провайдер отдаёт их по очереди, а закончившись, возвращается к
правилам вместо падения.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from ..contracts import CRITIC_MARKER, SUPERVISOR_MARKER
from .base import BaseLLM, Reply, Usage


def _last_user(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _system(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "system":
            return str(m.get("content") or "")
    return ""


class StubLLM(BaseLLM):
    """Отвечает по правилам. Никакой сети, никакой недетерминированности."""

    name = "stub"

    def __init__(self, model: str = "stub", *,
                 scripted: list[str] | None = None,
                 rule: Callable[[list[dict[str, Any]]], str] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.scripted = list(scripted or [])
        self.rule = rule
        self.seen: list[list[dict[str, Any]]] = []

    def _chat_once(self, messages: list[dict[str, Any]]) -> Reply:
        self.seen.append(messages)
        if self.scripted:
            text = self.scripted.pop(0)
        elif self.rule is not None:
            text = self.rule(messages)
        else:
            text = self._default(messages)
        return Reply(text=text, model=self.model,
                     usage=Usage(tokens_in=_approx_tokens(messages),
                                 tokens_out=max(1, len(text) // 4)))

    # --- правила по умолчанию -------------------------------------------
    @staticmethod
    def _default(messages: list[dict[str, Any]]) -> str:
        task = _last_user(messages).strip()

        # Роль определяется по маркеру ШАБЛОНА СРЕДЫ в тексте запроса, а
        # НЕ по словам системного промпта: промпт пишет пользователь, и
        # слово «Критик» законно встречается в промпте Исполнителя
        # («твою работу проверит Критик»). Опознание по промпту молча
        # превращало ответ Исполнителя в JSON-вердикт.
        if CRITIC_MARKER in task:
            # Валидный JSON-вердикт: цикл качества обязан его разобрать.
            return json.dumps({
                "score": 0.85,
                "verdict": "accept",
                "issues": [],
                "summary": "Замечаний нет: работа отвечает задаче.",
            }, ensure_ascii=False)

        if SUPERVISOR_MARKER in task:
            return json.dumps({
                "decision": "accept",
                "reason": "Критик не нашёл блокирующих дефектов.",
            }, ensure_ascii=False)

        # Исполнитель: эхо задачи — этого хватает, чтобы проверить
        # прохождение данных через доску контекста и плейсхолдеры.
        head = re.sub(r"\s+", " ", task)[:300]
        return f"[stub] Выполнено. Задача была: {head}"


def _approx_tokens(messages: list[dict[str, Any]]) -> int:
    """Грубая оценка: 4 символа на токен. Точность здесь не нужна —
    нужно, чтобы счётчики среды не были нулями в демо и тестах."""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    return max(1, total // 4)
