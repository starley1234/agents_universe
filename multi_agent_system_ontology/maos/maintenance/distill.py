"""Дистилляция диалога в компактный "квант памяти" (вопрос-ответ пара).

Без summarizer (LLM) — детерминированная эвристика: первое сообщение
пользователя как "вопрос", последний ответ агента как "ответ". Это
осознанное упрощение: настоящее качество распределения диалога на
тему/итог требует LLM-суммаризации, для которой summarizer можно
передать явно (обычно дешёвая локальная модель — экономия токенов,
раз уж вся идея дистилляции в том, чтобы не гонять большую модель на
обслуживание памяти, см. ТЗ п.3 "Local: приоритет для... обслуживания
памяти").
"""
from __future__ import annotations

from typing import Any, Callable


def distill_conversation(messages: list[dict[str, Any]],
                        summarizer: Callable[[str], str] | None = None,
                        ) -> tuple[str, str]:
    user_msgs = [m for m in messages if m["role"] == "user"]
    agent_msgs = [m for m in messages if m["role"] == "agent"]
    if not user_msgs or not agent_msgs:
        return "", ""

    question = user_msgs[0]["content"]
    if summarizer is not None and len(messages) > 4:
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        answer = summarizer(transcript)
    else:
        answer = agent_msgs[-1]["content"]
    return question, answer
