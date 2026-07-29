"""Context Synchronization: общая доска прогона.

ПОЧЕМУ ДОСКА, А НЕ ПЕРЕДАЧА ТЕКСТА ПО ЦЕПОЧКЕ. Наивная схема «ответ
первого агента подставили в задачу второго» ломается на третьем шаге:
контекст растёт квадратично, в него попадают рассуждения и извинения
модели, а найти «откуда взялась эта цифра» через день невозможно.
Доска решает это тремя свойствами:

  1. АДРЕСУЕМОСТЬ. Данные лежат под именами (`research_notes`, `brief`),
     а не «предыдущим сообщением». Шаг объявляет в определении, что
     читает и что пишет, — контракт виден до запуска.
  2. ВЕРСИОННОСТЬ. Запись не затирает предыдущую: `ctx_put` создаёт
     новую версию. «Кто и когда изменил цифру» — обычный запрос к
     истории, а не расследование по логам.
  3. ИЗБИРАТЕЛЬНОСТЬ. В промпт уходит только то, что шаг объявил в
     `reads`, а не вся история прогона. Это прямая экономия токенов и
     защита от того, что модель зацепится за чужой черновик.

Класс `Blackboard` — тонкая обёртка над Store: рендеринг задач
(подстановка плейсхолдеров) и сборка блока контекста для промпта.
Хранение — в Store, потому что доска обязана пережить перезапуск
процесса: без этого пауза на человеке не имеет смысла.
"""
from __future__ import annotations

import json
from typing import Any

from ..kernel.store import Store
from ..kernel.workflow import PLACEHOLDER_RE, WorkflowError

#: Сколько символов одного значения доски пускаем в промпт. Доска может
#: хранить мегабайт — контекст модели столько не выдержит.
CTX_VALUE_LIMIT = 12_000


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _clip(text: str, limit: int = CTX_VALUE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[...обрезано, всего {len(text)} символов]"


class Blackboard:
    """Доска одного прогона: чтение, запись, подстановка, срез для промпта."""

    def __init__(self, store: Store, run_id: int, *, goal: str = "",
                 inputs: dict[str, Any] | None = None) -> None:
        self.store = store
        self.run_id = run_id
        self.goal = goal
        self.inputs = dict(inputs or {})

    # --- доступ ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self.store.ctx_get(self.run_id, key, default)

    def put(self, key: str, value: Any, author: str = "") -> int:
        return self.store.ctx_put(self.run_id, key, value, author=author)

    def keys(self) -> list[str]:
        return self.store.ctx_keys(self.run_id)

    def snapshot(self) -> dict[str, Any]:
        return self.store.ctx_all(self.run_id)

    def history(self, key: str = "") -> list[dict[str, Any]]:
        return self.store.ctx_history(self.run_id, key)

    # --- подстановка ------------------------------------------------------
    def render(self, template: str, step_outputs: dict[str, str] | None = None) -> str:
        """Подставить {goal}, {input.x}, {ctx.y}, {step.z}.

        Отсутствие значения — ОШИБКА, а не пустая строка. Задача с дырой
        («Сведи заметки: <пусто>») выглядит для модели осмысленной, и она
        добросовестно выдумает содержимое. Лучше остановиться.
        """
        outputs = step_outputs or {}

        def sub(m) -> str:
            kind, arg = m.group(1), m.group(2) or ""
            if kind == "goal":
                return self.goal
            if kind == "input":
                if arg not in self.inputs:
                    raise WorkflowError(
                        f"Не передан вход {arg!r}, требуемый задачей шага")
                return _as_text(self.inputs[arg])
            if kind == "ctx":
                value = self.get(arg)
                if value is None:
                    raise WorkflowError(
                        f"На доске нет ключа {arg!r} — шаг, который его пишет, "
                        "ещё не выполнен или завершился без результата")
                return _clip(_as_text(value))
            if kind == "step":
                if arg not in outputs:
                    raise WorkflowError(
                        f"Шаг {arg!r} ещё не дал результата")
                return _clip(outputs[arg])
            raise WorkflowError(f"Неизвестный плейсхолдер {{{kind}}}")

        return PLACEHOLDER_RE.sub(sub, template)

    def context_block(self, keys: list[str]) -> str:
        """Блок «Данные из общего контекста» для промпта роли.

        Отсутствующий ключ здесь НЕ ошибка (в отличие от render): `reads`
        описывает, что шагу полезно видеть, а не что обязано быть. Валить
        прогон из-за необязательной справки — перебор.
        """
        if not keys:
            return ""
        parts: list[str] = []
        for key in keys:
            value = self.get(key)
            if value is None:
                continue
            parts.append(f"### {key}\n{_clip(_as_text(value))}")
        if not parts:
            return ""
        return "Данные из общего контекста прогона:\n\n" + "\n\n".join(parts)

    def describe(self) -> list[dict[str, Any]]:
        """Компактная сводка доски — для дашборда и CLI."""
        out = []
        for key, value in sorted(self.snapshot().items()):
            text = _as_text(value)
            out.append({"key": key, "size": len(text),
                        "preview": text[:200] + ("…" if len(text) > 200 else "")})
        return out
