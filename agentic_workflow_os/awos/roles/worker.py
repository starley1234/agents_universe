"""Роль Исполнитель: один ход агента с инструментами.

ЦИКЛ ХОДА: модель -> (вызов инструмента -> результат) * N -> текст.
Пока модель просит инструмент — среда выполняет и возвращает результат;
как только модель отвечает текстом без блока ```tool — работа готова.

Три вещи, ради которых здесь код, а не десять строк:

  1. ОШИБКА ИНСТРУМЕНТА НЕ РОНЯЕТ ШАГ. Текст ошибки уходит модели как
     результат вызова: «файла нет», «хост запрещён», «сломан JSON». В
     подавляющем большинстве случаев модель исправляется сама.
  2. ЛИМИТ ВЫЗОВОВ. Без него модель может уйти в бесконечный цикл
     «прочитал — прочитал — прочитал», сжигая токены. По исчерпании —
     честный отчёт и запрос финального ответа, а не тихий обрыв.
  3. ОПАСНЫЕ ВЫЗОВЫ ПРОХОДЯТ ЧЕРЕЗ ЧЕЛОВЕКА. Если инструмент помечен
     dangerous и включён HITL, среда обязана СНАЧАЛА спросить. Здесь это
     оформлено как исключение PauseForHuman, которое перехватывает
     движок: роль не знает ни про HTTP, ни про базу, ни про то, как
     именно спрашивают человека.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..llm.base import BaseLLM, LLMError, Usage
from ..tools.base import ToolError, ToolRegistry
from ..tools.protocol import (ProtocolError, ToolCall, extract_call,
                              protocol_prompt, strip_calls)


class PauseForHuman(Exception):
    """Ход остановлен: требуется решение человека по опасному вызову.

    Не ошибка — управляющее исключение. Роль не умеет ждать человека и
    не должна: этим занимается движок, у которого есть хранилище и
    понятие «прогон в статусе waiting_human».
    """

    def __init__(self, call: ToolCall, question: str) -> None:
        super().__init__(question)
        self.call = call
        self.question = question


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0
    #: Почему ход завершился: done | tool_limit | llm_error
    stopped_by: str = "done"
    detail: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)


def run_turn(llm: BaseLLM, system: str, task: str, *,
             tools: ToolRegistry | None = None,
             max_tool_steps: int = 8,
             output_limit: int = 4000,
             on_tool: Callable[[ToolCall, str, bool, float], None] | None = None,
             confirm: Callable[[ToolCall], bool] | None = None,
             on_message: Callable[[str, str], None] | None = None,
             ) -> TurnResult:
    """Выполнить один ход роли.

    confirm(call) -> True/False: спросить среду, можно ли выполнять
    опасный вызов. Возврат False означает «нужен человек» — роль кидает
    PauseForHuman, движок превращает это в точку контроля.
    """
    registry = tools or ToolRegistry()
    known = set(registry.names())
    sys_prompt = system.strip()
    if known:
        sys_prompt += "\n\n" + protocol_prompt(registry.prompt())

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": task},
    ]
    result = TurnResult()

    for step in range(max_tool_steps + 1):
        try:
            reply = llm.chat(messages)
        except LLMError as exc:
            result.stopped_by = "llm_error"
            result.detail = str(exc)
            return result

        result.llm_calls += 1
        result.usage = result.usage + reply.usage
        text = reply.text or ""
        messages.append({"role": "assistant", "content": text})
        result.transcript.append({"role": "assistant", "content": text})
        if on_message:
            on_message("assistant", text)

        try:
            call = extract_call(text, known if known else set())
        except ProtocolError as exc:
            # Формат нарушен — объясняем модели и даём ещё попытку. Это
            # дешевле, чем валить шаг: слабые модели путают формат на
            # первом вызове и исправляются на втором.
            feedback = f"Ошибка формата вызова: {exc}"
            messages.append({"role": "user", "content": feedback})
            result.transcript.append({"role": "tool", "content": feedback})
            if on_message:
                on_message("tool", feedback)
            continue

        if call is None:
            result.text = strip_calls(text) or text.strip()
            result.stopped_by = "done"
            return result

        if step >= max_tool_steps:
            break

        tool = registry.get(call.tool)
        if tool is None:                      # extract_call уже проверил, но
            feedback = f"Инструмент {call.tool!r} недоступен"   # защита от гонок
            messages.append({"role": "user", "content": feedback})
            continue

        if tool.dangerous and confirm is not None and not confirm(call):
            raise PauseForHuman(
                call,
                f"Агент хочет выполнить опасный инструмент {call.tool!r}. "
                "Подтвердите или отклоните.")

        started = time.time()
        try:
            output = tool.fn(**call.args)
            ok = True
        except ToolError as exc:
            output = f"Ошибка инструмента: {exc}"
            ok = False
        except TypeError as exc:
            # Модель прислала лишний/недостающий аргумент. Это ожидаемая
            # ситуация, а не баг среды — объясняем и продолжаем.
            expected = ", ".join(tool.args) or "без аргументов"
            output = (f"Неверные аргументы для {call.tool!r}: {exc}. "
                      f"Ожидаются: {expected}")
            ok = False
        except Exception as exc:              # noqa: BLE001 — граница среды
            output = f"Инструмент {call.tool!r} упал: {type(exc).__name__}: {exc}"
            ok = False
        elapsed = time.time() - started

        result.tool_calls += 1
        text_out = str(output)
        if len(text_out) > output_limit:
            text_out = (text_out[:output_limit]
                        + f"\n[...обрезано, всего {len(str(output))} символов]")
        if on_tool:
            on_tool(call, text_out, ok, elapsed)

        feedback = f"Результат {call.tool}:\n{text_out}"
        messages.append({"role": "user", "content": feedback})
        result.transcript.append({"role": "tool", "content": feedback})
        if on_message:
            on_message("tool", feedback)

    # Лимит вызовов исчерпан: просим модель ответить тем, что уже есть.
    messages.append({
        "role": "user",
        "content": ("Лимит обращений к инструментам исчерпан. Дай финальный "
                    "ответ по задаче на основании уже собранных данных, без "
                    "новых вызовов."),
    })
    try:
        reply = llm.chat(messages)
        result.llm_calls += 1
        result.usage = result.usage + reply.usage
        result.text = strip_calls(reply.text) or reply.text.strip()
        result.stopped_by = "tool_limit"
        result.detail = f"исчерпан лимит вызовов инструментов ({max_tool_steps})"
    except LLMError as exc:
        result.stopped_by = "llm_error"
        result.detail = str(exc)
    return result
