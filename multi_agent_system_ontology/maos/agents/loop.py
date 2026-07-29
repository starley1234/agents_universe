"""Инструментальный цикл «модель -> инструмент -> модель» для MAOS-агентов.

Перенесено и адаптировано из agent_system/agent/core.py — тот же
инвариант: пока модель просит инструменты, выполняем их и возвращаем
результаты; как только модель отвечает текстом без вызовов — ход диалога
завершён. Три причины, по которым это отдельный класс, а не пять строк:

  1. Ошибка инструмента НЕ роняет ход диалога — текст ошибки уходит
     модели, она получает шанс исправиться.
  2. Лимит шагов (cfg.max_tool_steps) — ОДНОГО хода диалога, не всей
     сессии: без него агент со сломанной моделью мог бы звать
     инструменты бесконечно на каждое сообщение пользователя.
  3. Обрезка длинных результатов инструментов в истории
     (cfg.tool_result_limit) — то же решение, что в agent_system.

Отличие от agent_system: здесь модель выбирается ГИБРИДНЫМ роутером
(HybridLLM) — каждый вызов может уйти на другого провайдера (local/
cloud) в зависимости от исходной задачи и fallback при сбое, поэтому
provider_model может меняться от шага к шагу; итоговый ответ несёт
метку модели, которая ДАЛА ФИНАЛЬНЫЙ текстовый ответ.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config
from ..llm.base import LLMError
from ..orchestrator.hybrid import HybridLLM
from ..tools.base import ToolError, ToolRegistry


@dataclass
class ToolLoopResult:
    text: str
    provider_model: str
    used_fallback: bool
    tokens_used: int
    tool_calls: int
    stopped_by: str            # "done" | "max_steps" | "error"


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return (text[:head] + f"\n… [вырезано {len(text) - limit} символов] …\n"
           + text[-tail:])


def _exec_tool(registry: ToolRegistry, name: str, args: dict[str, Any]) -> str:
    try:
        tool = registry.get(name)
    except ToolError as exc:
        return f"ОШИБКА: {exc}"
    if not isinstance(args, dict):
        return f"ОШИБКА: аргументы {name} должны быть объектом JSON"
    try:
        return tool.fn(**args)
    except ToolError as exc:
        return f"ОШИБКА: {exc}"
    except TypeError as exc:
        return (f"ОШИБКА: неверные аргументы для {name}: {exc}. "
               f"Схема: {json.dumps(tool.parameters, ensure_ascii=False)}")
    except Exception as exc:  # непредвиденное — тоже не роняем цикл
        return f"ОШИБКА ({type(exc).__name__}): {exc}"


def run_tool_loop(hybrid: HybridLLM, cfg: Config, messages: list[dict[str, Any]],
                  task: str, agent_llm_ref: str, registry: ToolRegistry,
                  on_event: Callable[[str, dict[str, Any]], None] | None = None,
                  ) -> ToolLoopResult:
    """Выполняет цикл вызовов инструментов для одного хода диалога.

    messages — уже собранная история (system+mid-term+short-term+вопрос),
    task — исходный текст пользователя (нужен HybridLLM.choose_ref, чтобы
    выбор local/cloud не менялся от шага к шагу внутри одного хода).
    """
    def emit(kind: str, **data: Any) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, data)
        except Exception:  # наблюдатель не должен ломать цикл инструментов
            pass

    schemas = registry.schemas()
    messages = list(messages)
    tool_calls_total = 0
    total_tokens = 0
    last_provider_model = ""
    last_used_fallback = False

    for step_n in range(1, cfg.max_tool_steps + 1):
        try:
            result = hybrid.chat(messages, task, agent_llm_ref=agent_llm_ref,
                                 tools=schemas)
        except LLMError as exc:
            emit("tool_loop_error", message=str(exc))
            return ToolLoopResult(f"Ошибка обращения к модели: {exc}", "",
                                  False, total_tokens, tool_calls_total, "error")

        reply = result.reply
        last_provider_model = result.provider_model
        last_used_fallback = result.used_fallback
        total_tokens += reply.usage.total

        if not reply.wants_tools:
            text = reply.text or "(пустой ответ модели)"
            messages.append({"role": "assistant", "content": text})
            emit("answer", text=text, step=step_n)
            return ToolLoopResult(text, last_provider_model, last_used_fallback,
                                  total_tokens, tool_calls_total, "done")

        if reply.text:
            emit("thought", text=reply.text, step=step_n)

        messages.append({
            "role": "assistant",
            "content": reply.text or None,
            "tool_calls": [{
                "id": c.id, "type": "function",
                "function": {"name": c.name,
                             "arguments": json.dumps(c.arguments, ensure_ascii=False)},
            } for c in reply.tool_calls],
        })

        for call in reply.tool_calls:
            emit("tool_start", name=call.name, args=call.arguments, step=step_n)
            tool_result = _exec_tool(registry, call.name, call.arguments)
            tool_calls_total += 1
            emit("tool_end", name=call.name, result=tool_result, step=step_n)
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": _clip(tool_result, cfg.tool_result_limit),
            })

    emit("tool_loop_limit", steps=cfg.max_tool_steps)
    return ToolLoopResult(
        f"Достигнут предел в {cfg.max_tool_steps} шагов вызова инструментов — "
        "ход диалога не доведён до финального ответа.",
        last_provider_model, last_used_fallback, total_tokens,
        tool_calls_total, "max_steps")
