"""Вопрос человеку.

Зачем: без этого инструмента агент, столкнувшись с развилкой, ДОГАДЫВАЕТСЯ.
Догадка выглядит как работа, но приводит к переделке всего сделанного после.

Два положения дел, и оба должны быть честными:

  человек рядом (run, chat) — вопрос идёт в терминал, ответ возвращается
      модели и попадает в память: следующий запуск не спросит то же самое;

  человека нет (--auto, сервер) — выдумывать ответ нельзя. Текущий пункт
      плана помечается «blocked» с текстом вопроса, агент переходит к
      следующему. Пункт НЕ закрывается как выполненный и виден в итоге.

Лимит: ASK_LIMIT вопросов на агента. Дальше инструмент отвечает отказом и
требует действовать по разумному умолчанию, назвав допущение вслух.
Иначе модель заменяет работу опросом.
"""
from __future__ import annotations

from typing import Callable

from ..store import Store
from .base import Tool, ToolError

#: больше — уже не работа, а анкета
ASK_LIMIT = 3


def build(
    store: Store,
    run_id_getter: Callable[[], int],
    ask: Callable[[str, list[str]], str] | None = None,
) -> list[Tool]:
    """ask(вопрос, варианты) -> строка ответа; пустая строка = не ответил.

    ask=None означает «человека рядом нет» — режим блокировки пункта.
    """
    asked: list[str] = []

    def rid() -> int:
        return run_id_getter()

    def _block_current(question: str) -> str:
        """Пометить текущий пункт плана как заблокированный вопросом."""
        doing = [t for t in store.tasks(rid()) if t["status"] == "doing"]
        if not doing:
            return ""
        task = doing[0]
        store.set_task(task["id"], "blocked", f"ВОПРОС: {question}")
        return f"#{task['id']} {task['title']}"

    def ask_user(question: str, options: str = "") -> str:
        q = question.strip()
        if not q:
            raise ToolError("Пустой вопрос задавать нечего")
        opts = [o.strip() for o in options.split("|") if o.strip()]

        if len(asked) >= ASK_LIMIT:
            return (
                f"Лимит вопросов исчерпан ({ASK_LIMIT} за запуск). "
                "Больше не спрашивай: выбери разумное умолчание, работай "
                "дальше и в итоге прямо назови сделанное допущение."
            )
        asked.append(q)

        if ask is not None:
            try:
                answer = (ask(q, opts) or "").strip()
            except Exception as exc:          # ввод не должен ронять агента
                return f"Спросить не удалось ({exc}). Действуй по умолчанию."
            if answer:
                store.remember(f"Ответ человека на «{q}»: {answer}",
                               tags="ask", source="human", run_id=rid())
                return f"Ответ человека: {answer}"
            return ("Человек не ответил. Выбери разумное умолчание, "
                    "работай дальше и назови допущение в итоге.")

        # Человека рядом нет: молчание — не согласие.
        store.remember(f"Вопрос без ответа: {q}", tags="ask,open",
                       source="agent", run_id=rid())
        blocked = _block_current(q)
        head = ("Рядом никого нет, ответить некому. Вопрос записан, "
                "человек увидит его в итоге прогона.")
        if blocked:
            return (f"{head}\nПункт {blocked} помечен как заблокированный — "
                    "он НЕ выполнен. Возьми следующий пункт плана. Не "
                    "закрывай заблокированный пункт через plan_done.")
        return (f"{head}\nПродолжай с разумным умолчанием и явно напиши "
                "в итоге, какое допущение ты сделал.")

    return [
        Tool("ask_user",
             "Задать человеку уточняющий вопрос, когда без ответа работа "
             "пойдёт наугад: неясны требования, несколько равных вариантов, "
             "нужно разрешение. Не спрашивай о том, что можно выяснить "
             "инструментом самому. Если человека нет, вопрос сохранится, "
             "а текущий пункт плана будет помечен как заблокированный.",
             {"type": "object",
              "properties": {
                  "question": {"type": "string",
                               "description": "Один конкретный вопрос"},
                  "options": {"type": "string",
                              "description": "Варианты ответа через | "
                                             "(необязательно)"}},
              "required": ["question"]},
             ask_user),
    ]
