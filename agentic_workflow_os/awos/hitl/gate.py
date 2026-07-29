"""Human-in-the-Loop: точки контроля как состояние прогона.

ГЛАВНОЕ РЕШЕНИЕ: ПАУЗА — ЭТО СОСТОЯНИЕ В БАЗЕ, А НЕ ОЖИДАНИЕ В ПАМЯТИ.
Соблазнительно реализовать HITL блокирующим `input()` или ожиданием на
threading.Event — и всё работает, пока процесс жив. Реальность другая:
человек уходит на совещание, сервер перезапускают, вкладку закрывают.
Поэтому среда поступает иначе: создаёт запись `checkpoint`, переводит
прогон в статус `waiting_human` и ВЫХОДИТ. Ответ человека приходит
позже — из CLI, из HTTP API, из дашборда, хоть на следующий день, — и
любой процесс, у которого есть та же база, продолжает прогон с того же
места командой `resume`.

ЧТО СЧИТАЕТСЯ ТОЧКОЙ КОНТРОЛЯ. Три вида:
  * approval — утвердить результат шага (можно с правкой текста);
  * tool     — разрешить опасный вызов инструмента (shell и подобное);
  * input    — попросить у человека недостающие данные.

РЕЖИМЫ (config.hitl_mode):
  off      — среда не спрашивает никогда, даже при провале качества;
  critical — спрашивает там, где сама сомневается: Контролёр вернул
             работу и доработки исчерпаны, опасный инструмент, шаг с
             human="always" или "on_reject";
  always   — утверждение результата каждого шага.

ОЖИДАНИЕ. `hitl_wait_seconds` позволяет CLI подождать ответа несколько
секунд (удобно, когда человек сидит рядом с дашбордом), но это лишь
оптимизация: истёк таймаут — прогон корректно засыпает в базе, ничего
не теряется.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..kernel.store import Store

#: Решения человека, которые понимает среда.
HUMAN_DECISIONS = ("approved", "rejected", "edited", "cancelled")


@dataclass
class HumanResponse:
    status: str                       # approved | rejected | edited | cancelled
    response: str = ""                # правка или комментарий
    actor: str = "human"
    checkpoint_id: int = 0

    @property
    def approved(self) -> bool:
        return self.status in ("approved", "edited")


class Gate:
    """Единая точка принятия решения «нужен ли здесь человек».

    Вся политика HITL собрана в одном классе намеренно: разбросанная по
    движку, она неизбежно разъезжается — где-то забыли учесть режим,
    где-то спросили дважды. Движок задаёт вопросы («нужно ли утверждение
    для этого шага?»), Gate отвечает по конфигу и полю шага.
    """

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    # --- политика --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.cfg.hitl_mode != "off"

    def needs_approval(self, step_human: str) -> bool:
        """Нужно ли утверждение результата ЭТОГО шага человеком."""
        if self.cfg.hitl_mode == "off":
            return False
        if step_human == "never":
            return False
        if step_human == "always":
            return True
        if step_human == "on_reject":
            return False               # спросим только при отказе качества
        # step_human == "default": политика среды
        return self.cfg.hitl_mode == "always"

    def needs_escalation(self, step_human: str) -> bool:
        """Звать ли человека, когда качество не вытянуло."""
        if self.cfg.hitl_mode == "off":
            return False
        if step_human == "never":
            return False
        return True                     # critical и always — эскалируем

    def needs_tool_confirm(self, dangerous: bool) -> bool:
        """Подтверждать ли опасный вызов инструмента.

        Даже в режиме off? Нет: off означает «человека нет за пультом»,
        и остановка навсегда была бы хуже. Ответственность за включение
        shell при hitl_mode=off несёт администратор среды — это явное
        сочетание двух настроек, а не случайность.
        """
        return dangerous and self.cfg.hitl_mode != "off"

    # --- точки контроля ---------------------------------------------------
    def ask(self, run_id: int, step_id: int | None, kind: str, question: str,
            payload: Any = None) -> int:
        checkpoint_id = self.store.create_checkpoint(run_id, step_id, kind,
                                                     question, payload)
        self.store.log(run_id, "checkpoint_open", question, role="human",
                       step_id=step_id,
                       data={"checkpoint_id": checkpoint_id, "kind": kind})
        return checkpoint_id

    def wait(self, checkpoint_id: int,
             seconds: int | None = None) -> HumanResponse | None:
        """Подождать ответа. None — не дождались (прогон уснёт в базе).

        Опрос базы, а не подписка: ответить человек может из другого
        процесса (CLI, HTTP), и общая база — единственный надёжный канал
        между ними. Интервал 0.2 с — компромисс между отзывчивостью
        дашборда и нагрузкой на SQLite.
        """
        limit = self.cfg.hitl_wait_seconds if seconds is None else seconds
        if limit == 0:
            row = self.store.get_checkpoint(checkpoint_id)
            return self._response(row) if row and row["status"] != "pending" else None
        deadline = None if limit < 0 else time.time() + limit
        while True:
            row = self.store.get_checkpoint(checkpoint_id)
            if row is None:
                return None
            if row["status"] != "pending":
                return self._response(row)
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.2)

    @staticmethod
    def _response(row: dict[str, Any]) -> HumanResponse:
        return HumanResponse(status=row["status"],
                             response=row.get("response") or "",
                             actor=row.get("actor") or "human",
                             checkpoint_id=int(row["id"]))

    def resolve(self, checkpoint_id: int, status: str, response: str = "",
                actor: str = "human") -> HumanResponse:
        """Записать решение человека (вызывается из CLI/HTTP/дашборда)."""
        self.store.resolve_checkpoint(checkpoint_id, status, response, actor)
        row = self.store.get_checkpoint(checkpoint_id)
        assert row is not None
        self.store.log(row["run_id"], "checkpoint_resolved",
                       f"{status}: {response[:200]}" if response else status,
                       role="human", step_id=row.get("step_id"),
                       data={"checkpoint_id": checkpoint_id, "actor": actor})
        return self._response(row)

    def pending(self, run_id: int | None = None) -> list[dict[str, Any]]:
        return self.store.list_checkpoints(run_id, status="pending")
