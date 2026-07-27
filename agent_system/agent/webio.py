"""Вопросы агента человеку через веб.

Задача нетривиальная: агент работает внутри потока, обслуживающего
HTTP-запрос, и в момент вопроса ему надо ОСТАНОВИТЬСЯ и дождаться
ответа, который придёт ДРУГИМ запросом. В терминале это `input()`,
здесь — ожидание на threading.Event.

Как это работает:

    поток агента            поток ответа (другой HTTP-запрос)
    ─────────────           ─────────────────────────────────
    ask() -> событие в
    поток NDJSON, ждём
                            POST /answer {"id": 3, "text": "…"}
                            answer() кладёт текст, будит Event
    ask() возвращает
    ответ, работа идёт

Три вещи, без которых это опасно:

  ТАЙМАУТ. Человек ушёл пить чай или закрыл вкладку — агент не должен
  ждать вечно, занимая поток и деньги. По истечении возвращается пустая
  строка, а инструмент ask_user уже умеет её трактовать: «не ответил,
  действуй по умолчанию и назови допущение».

  ПРЕДЕЛ ОЧЕРЕДИ. Иначе зациклившийся агент накопит тысячи вопросов и
  съест память.

  ВОПРОСЫ ПЕРЕЖИВАЮТ ПЕРЕЗАГРУЗКУ СТРАНИЦЫ. Человек обновил вкладку —
  вопрос не должен пропасть, иначе агент молча простоит до таймаута.
  Поэтому список ожидающих доступен отдельным запросом.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: Сколько ждать человека. Больше — агент занимает поток впустую,
#: меньше — не успеть переключиться на вкладку.
DEFAULT_TIMEOUT = 600.0

#: Больше одновременных вопросов — признак зацикливания, а не работы.
MAX_PENDING = 20


@dataclass
class Question:
    id: int
    text: str
    options: list[str]
    created: float
    run_id: int = 0
    answer: str | None = None
    _event: threading.Event = field(default_factory=threading.Event,
                                    repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "question": self.text,
                "options": self.options, "run_id": self.run_id,
                "waiting": round(time.time() - self.created, 1)}


class QuestionBox:
    """Общий на весь сервер ящик вопросов, ждущих ответа."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        self._items: dict[int, Question] = {}
        self._next = 1
        #: Кому разослать событие о новом вопросе. Ставит сервер.
        self.on_new: Callable[[Question], None] | None = None

    # --------------------------------------------------- сторона агента
    def ask(self, text: str, options: list[str] | None = None,
            run_id: int = 0, timeout: float | None = None) -> str:
        """Задать вопрос и ДОЖДАТЬСЯ ответа. Пусто = не ответили."""
        with self._lock:
            if len(self._items) >= MAX_PENDING:
                # Не копим бесконечно: это уже не диалог, а зацикливание.
                return ""
            q = Question(self._next, text.strip(), list(options or []),
                         time.time(), run_id)
            self._items[q.id] = q
            self._next += 1

        if self.on_new:
            try:
                self.on_new(q)
            except Exception:      # наблюдатель не должен ломать агента
                pass

        got = q._event.wait(timeout if timeout is not None else self.timeout)
        with self._lock:
            self._items.pop(q.id, None)
        return (q.answer or "") if got else ""

    # -------------------------------------------------- сторона человека
    def answer(self, qid: int, text: str) -> bool:
        """Ответить на вопрос. False — такого вопроса нет (или истёк)."""
        with self._lock:
            q = self._items.get(qid)
            if q is None:
                return False
            q.answer = text
        q._event.set()
        return True

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda x: x.id)
        return [q.as_dict() for q in items]

    def drop(self, qid: int) -> bool:
        """Снять вопрос без ответа: агент получит пустую строку."""
        with self._lock:
            q = self._items.get(qid)
            if q is None:
                return False
            q.answer = ""
        q._event.set()
        return True

    def clear(self) -> int:
        """Разбудить всех: используется при остановке сервера."""
        with self._lock:
            items = list(self._items.values())
            for q in items:
                q.answer = ""
        for q in items:
            q._event.set()
        return len(items)
