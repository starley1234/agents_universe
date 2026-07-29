"""StubLLM — детерминированный офлайн-провайдер для тестов и демо.

Нужен по той же причине, что и hash-эмбеддер: систему надо показывать и
проверять там, где нет ни ключа, ни локальной модели, — в CI, при первом
запуске, при отладке конвейера. Настоящая модель в этих сценариях мешает:
она медленная, платная и недетерминированная, а проверять надо САПС, а
не качество формулировок.

Отвечает валидным JSON того вида, который ждут агенты, поэтому весь
цикл «разбор -> предложение -> diff -> принятие» проходит целиком.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .base import BaseLLM, Reply, Usage


def _last_user(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


class StubLLM(BaseLLM):
    name = "stub"

    def __init__(self, model: str = "stub", *, scripted: list[str] | None = None,
                 rule: Callable[[list[dict[str, Any]]], str] | None = None,
                 **kwargs: Any) -> None:
        super().__init__(model, retries=0)
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
        total = sum(len(str(m.get("content") or "")) for m in messages)
        return Reply(text=text, model=self.model,
                     usage=Usage(max(1, total // 4), max(1, len(text) // 4)))

    @staticmethod
    def _default(messages: list[dict[str, Any]]) -> str:
        task = _last_user(messages)
        # Маркеры шаблонов агентов — см. agents/prompts.py.
        if "ВЕРНИ JSON-РАЗБОР ФОРМУЛИРОВКИ" in task:
            return json.dumps({
                "score": 0.8,
                "issues": [],
                "improved": "",
                "comment": "Формулировка приемлема (ответ офлайн-заглушки).",
            }, ensure_ascii=False)
        if "ВЕРНИ JSON-СОПОСТАВЛЕНИЕ С ПУНКТАМИ" in task:
            return json.dumps({"matches": []}, ensure_ascii=False)
        return "[stub] Модель не настроена; это детерминированный ответ."
