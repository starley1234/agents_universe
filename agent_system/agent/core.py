"""Ядро агента: цикл «модель -> инструменты -> модель».

Инвариант цикла: пока модель просит инструменты — выполняем их и
возвращаем результаты; как только она отвечает текстом без вызовов —
задача считается завершённой.

Три вещи, ради которых здесь код, а не десять строк:
  1. Ошибка инструмента НЕ роняет агента. Текст ошибки уходит модели,
     и она получает шанс исправиться. Падать должен только сам процесс,
     если сломана модель или конфиг.
  2. Лимит шагов. Без него агент может крутиться бесконечно, сжигая
     токены. По исчерпании — честный отчёт, а не тихий обрыв.
  3. Обрезка истории. Длинные выводы инструментов вытесняют системный
     промпт и задачу; держим окно под контролем.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm.base import BaseLLM, LLMError
from .tools.base import ToolError, ToolRegistry

DEFAULT_SYSTEM = """Ты инженерный агент, работающий в изолированной рабочей папке.

Как работать:
- Действуй сам: читай файлы, правь их, запускай команды и проверки.
  Не проси пользователя сделать то, что можешь сделать инструментом.
- ПРОВЕРЯЙ РЕЗУЛЬТАТ ЧИСЛЕННО, а не на глаз. Утверждение «работает»
  должно опираться на вывод проверки, а не на предположение.
- Прежде чем доверять проверке, убедись, что она способна поймать
  ошибку. Тест, который всегда говорит «успех», бесполезен.
- Правь точечно. Не переписывай работающий код целиком ради стиля.
- Если сломал что-то — скажи об этом прямо и почини.
- Не выдавай непроверенное за проверенное. Если что-то не проверил,
  так и скажи.
- НЕ ВЫДУМЫВАЙ ПАМЯТЬ. Каждый запуск начинается с чистого листа: ты не
  помнишь прошлые разговоры. Спросили о прошлом — вызови recall (если
  инструмент есть) или посмотри файлы. Нет данных — скажи прямо:
  "не помню, история не сохранялась", а не делай вид, что помнишь.
- Если инструмент recall доступен, в конце работы записывай итог через
  remember — тогда следующий запуск сможет его найти.

Отвечай кратко и по делу. Когда задача выполнена — коротко перечисли,
что сделано и чем это подтверждается."""


@dataclass
class Step:
    """Один шаг цикла — для логов и разбора."""
    n: int
    text: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    elapsed: float = 0.0


@dataclass
class Result:
    answer: str
    steps: list[Step]
    stopped_by: str            # "done" | "max_steps" | "error"
    messages: list[dict[str, Any]]
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tool_calls(self) -> int:
        return sum(len(s.calls) for s in self.steps)


class Agent:
    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM,
        max_steps: int = 30,
        max_history_chars: int = 120_000,
        tool_result_limit: int = 4000,
        keep_last_results: int = 3,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        before_step: Callable[[int], None] | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_history_chars = max_history_chars
        self.tool_result_limit = tool_result_limit
        self.keep_last_results = keep_last_results
        self.on_event = on_event or (lambda kind, data: None)
        # Крючок перед шагом: сюда вешается автоснимок рабочей папки.
        # Сбой крючка не должен ломать работу — он страховка, а не цель.
        self.before_step = before_step

    # ------------------------------------------------------------ helpers
    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:  # наблюдатель не должен ломать агента
            pass

    def _trim(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Держим историю в пределах окна.

        Системный промпт и первая задача — неприкосновенны: без них агент
        забудет, что вообще делает. Вырезаем самые старые середины.
        """
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        if total <= self.max_history_chars:
            return messages
        head, tail = messages[:2], messages[2:]
        while tail and total > self.max_history_chars:
            drop = tail.pop(0)
            total -= len(json.dumps(drop, ensure_ascii=False))
        note = {"role": "system",
                "content": "[Часть ранней истории отброшена из-за длины. "
                           "Если нужны детали — перечитайте файлы.]"}
        return [*head, note, *tail]

    def _clip(self, text: str) -> str:
        """Обрезка результата инструмента для истории.

        Токены жрёт не модель, а мы сами, складывая в контекст полные
        выводы. Голова и хвост информативны, середина обычно — нет.
        """
        lim = self.tool_result_limit
        if lim <= 0 or len(text) <= lim:
            return text
        head, tail = int(lim * 0.6), int(lim * 0.4)
        return (text[:head]
                + f"\n… [вырезано {len(text) - lim} символов] …\n"
                + text[-tail:])

    def _compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """ЭКОНОМИЯ: старые результаты инструментов сворачиваем в сводку.

        Модели нужен полный текст последних результатов — с ними она
        работает прямо сейчас. Всё, что старше N вызовов, можно сжать до
        одной строки: факт вызова важен, простыня вывода — нет.
        """
        if self.keep_last_results <= 0:
            return messages
        idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        old = idx[:-self.keep_last_results] if len(idx) > self.keep_last_results else []
        if not old:
            return messages
        out = list(messages)
        for i in old:
            body = out[i].get("content") or ""
            if len(body) <= 200:
                continue
            first = body.strip().splitlines()[0][:120] if body.strip() else ""
            out[i] = {**out[i],
                      "content": f"[свёрнуто, {len(body)} символов] {first}"}
        return out

    def _exec_tool(self, name: str, args: dict[str, Any]) -> str:
        try:
            tool = self.tools.get(name)
        except ToolError as exc:
            return f"ОШИБКА: {exc}"
        if not isinstance(args, dict):
            return f"ОШИБКА: аргументы {name} должны быть объектом JSON"
        try:
            return tool.fn(**args)
        except ToolError as exc:
            # ожидаемая ошибка — модель может исправиться
            return f"ОШИБКА: {exc}"
        except TypeError as exc:
            return (f"ОШИБКА: неверные аргументы для {name}: {exc}. "
                    f"Схема: {json.dumps(tool.parameters, ensure_ascii=False)}")
        except Exception as exc:  # непредвиденное — тоже не роняем цикл
            return f"ОШИБКА ({type(exc).__name__}): {exc}"

    # --------------------------------------------------------------- main
    def run(self, task: str) -> Result:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        steps: list[Step] = []
        schemas = self.tools.schemas()
        empty_streak = 0
        NUDGE_LIMIT = 3          # больше — уже препирательство вместо работы
        u0 = self.llm.usage          # расход до запуска — вычтем в конце

        for n in range(1, self.max_steps + 1):
            t0 = time.time()
            if self.before_step is not None:
                try:
                    self.before_step(n)
                except Exception as exc:      # страховка не важнее работы
                    self._emit("warn", message=f"снимок не сделан: {exc}")
            try:
                reply = self.llm.chat(
                    self._trim(self._compact(messages)), schemas)
            except LLMError as exc:
                self._emit("error", message=str(exc))
                return Result(f"Ошибка обращения к модели: {exc}",
                              steps, "error", messages,
                              self.llm.usage.prompt - u0.prompt,
                              self.llm.usage.completion - u0.completion)

            step = Step(n=n, text=reply.text, elapsed=time.time() - t0)

            if not reply.wants_tools:
                """ХОД БЕЗ ДЕЙСТВИЯ — не признак выполненной задачи.

                Два случая, оба наблюдались на unsloth/qwen3.5-9b:
                  * content пуст и вызовов нет;
                  * content ПУСТ, а весь текст пришёл из reasoning_content —
                    то есть модель рассуждала («Покажу возможности: 1…»),
                    но ни инструмента не вызвала, ни ответа не дала.

                Второй случай коварнее: текст непустой, и агент принимал
                НАМЕРЕНИЕ за результат, завершая работу на втором шаге.
                Различаем по флагу from_reasoning."""
                stalled = (not reply.text.strip()) or reply.from_reasoning
                if stalled and empty_streak < NUDGE_LIMIT:
                    empty_streak += 1
                    steps.append(step)
                    self._emit("empty", step=n, attempt=empty_streak)
                    if reply.text.strip():
                        # рассуждение сохраняем: в нём обычно готовый план
                        messages.append({"role": "assistant",
                                         "content": reply.text})
                        nudge = ("Это рассуждение, а не действие. ВЫПОЛНИ "
                                 "намеченное: вызови инструмент. Если всё "
                                 "уже сделано — напиши итог обычным текстом.")
                    else:
                        nudge = ("Ты вернул пустой ответ. Либо вызови "
                                 "инструмент, либо напиши текстом, что "
                                 "сделано и каков итог.")
                    messages.append({"role": "user", "content": nudge})
                    continue
                steps.append(step)
                self._emit("answer", text=reply.text, step=n)
                messages.append({"role": "assistant", "content": reply.text})
                return Result(reply.text, steps, "done", messages,
                              self.llm.usage.prompt - u0.prompt,
                              self.llm.usage.completion - u0.completion)

            empty_streak = 0
            if reply.text:
                self._emit("thought", text=reply.text, step=n)

            messages.append({
                "role": "assistant",
                "content": reply.text or None,
                "tool_calls": [{
                    "id": c.id, "type": "function",
                    "function": {"name": c.name,
                                 "arguments": json.dumps(c.arguments,
                                                         ensure_ascii=False)},
                } for c in reply.tool_calls],
            })

            for call in reply.tool_calls:
                self._emit("tool_start", name=call.name,
                           args=call.arguments, step=n)
                result = self._exec_tool(call.name, call.arguments)
                step.calls.append({"name": call.name, "args": call.arguments,
                                   "result": result})
                self._emit("tool_end", name=call.name, result=result, step=n)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": self._clip(result)})

            steps.append(step)

        # шаги исчерпаны — сообщаем честно, а не делаем вид, что всё готово
        self._emit("limit", steps=self.max_steps)
        return Result(
            f"Достигнут предел в {self.max_steps} шагов, задача не доведена "
            "до конца. Уточните задачу или увеличьте max_steps.",
            steps, "max_steps", messages,
            self.llm.usage.prompt - u0.prompt,
            self.llm.usage.completion - u0.completion)
