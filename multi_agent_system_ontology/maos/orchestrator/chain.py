"""Ручной вызов детерминированной цепочки Agent_A -> Agent_B (ТЗ п.5).

Тот же архитектурный принцип, что в agent_system/agent/pipeline.py: нет
"мета-агента", руководящего другими агентами — последовательность агентов
задаётся ЗАРАНЕЕ явным списком slug'ов (человеком или конфигом), а не
придумывается моделью на лету. Каждый шаг — обычный AgentRuntime.respond,
результат которого подставляется в задачу следующего шага через
плейсхолдер {prev} (ответ непосредственно предыдущего шага) либо {step_N}
(ответ шага с порядковым номером N, считая с 0).
"""
from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any, Callable

from ..config import Config
from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..memory.store import Store

if TYPE_CHECKING:  # разрыв цикла: agents/runtime.py импортирует
    from ..agents.runtime import AgentRuntime  # orchestrator/context.py

_PLACEHOLDER_RE = re.compile(r"\{(prev|step_\d+|goal)\}")


class ChainError(Exception):
    """Ожидаемая ошибка построения/выполнения цепочки."""


def _fill(template: str, goal: str, answers: list[str]) -> str:
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "goal":
            return goal
        if key == "prev":
            return answers[-1] if answers else ""
        idx = int(key.split("_", 1)[1])
        if idx >= len(answers):
            raise ChainError(
                f"Плейсхолдер {{{key}}} ссылается на ещё не выполненный шаг")
        return answers[idx]
    return _PLACEHOLDER_RE.sub(_sub, template)


class ChainRunner:
    """Выполняет цепочку агентов пошагово, с трансляцией событий."""

    def __init__(self, cfg: Config, store: Store, embedder: BaseEmbedder,
                on_event: Callable[[str, dict[str, Any]], None] | None = None,
                stop_event: threading.Event | None = None,
                runtime: "AgentRuntime | None" = None) -> None:
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.on_event = on_event or (lambda kind, data: None)
        self.stop_event = stop_event
        if runtime is None:
            from ..agents.runtime import AgentRuntime as _AgentRuntime
            runtime = _AgentRuntime(cfg)
        self.runtime = runtime

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    def run(self, goal: str, agent_slugs: list[str],
           conversation_id: int | None = None) -> dict[str, Any]:
        if not agent_slugs:
            raise ChainError("Цепочка не может быть пустой")
        for slug in agent_slugs:
            if not self.store.get_agent(slug):
                raise ChainError(f"Агент {slug!r} не найден")

        chain_id = self.store.start_chain(goal, agent_slugs, conversation_id)
        self._emit("chain_start", chain_run_id=chain_id, goal=goal,
                   agents=agent_slugs)

        steps = self.store.chain_steps(chain_id)
        answers: list[str] = []
        stop_reason = "done"

        for step in steps:
            if self.stop_event is not None and self.stop_event.is_set():
                self.store.set_chain_step(step["id"], "skipped")
                self._emit("step_skipped", ord=step["ord"],
                           agent_slug=step["agent_slug"])
                stop_reason = "stopped"
                continue
            if stop_reason != "done":
                self.store.set_chain_step(step["id"], "skipped")
                self._emit("step_skipped", ord=step["ord"],
                           agent_slug=step["agent_slug"])
                continue

            template = ("{goal}" if not answers else
                       "Продолжи, используя предыдущий ответ:\n{prev}\n\n"
                       "Общая цель: {goal}")
            try:
                task = _fill(template, goal, answers)
            except ChainError as exc:
                self.store.set_chain_step(step["id"], "failed", error=str(exc))
                self._emit("step_failed", ord=step["ord"], error=str(exc))
                stop_reason = "failed"
                continue

            self.store.set_chain_step(step["id"], "running", task=task)
            self._emit("step_start", ord=step["ord"],
                       agent_slug=step["agent_slug"], task=task)

            agent_row = self.store.get_agent(step["agent_slug"])
            try:
                turn = self.runtime.respond(
                    agent_row, task, [], store=self.store,
                    embedder=self.embedder, conversation_id=conversation_id)
            except Exception as exc:
                self.store.set_chain_step(step["id"], "failed", error=str(exc))
                self._emit("step_failed", ord=step["ord"], error=str(exc))
                stop_reason = "failed"
                continue

            self.store.set_chain_step(
                step["id"], "done", answer=turn.text,
                provider_model=turn.provider_model)
            answers.append(turn.text)
            self._emit("step_done", ord=step["ord"],
                       agent_slug=step["agent_slug"], answer=turn.text,
                       provider_model=turn.provider_model)

            try:
                qvec = self.embedder.embed_one(task)
            except EmbeddingError:
                qvec = None
            self.store.add_memory_quantum(
                conversation_id, task, turn.text, agent_id=agent_row["id"],
                provider_model=turn.provider_model,
                tokens_used=turn.tokens_used, embedding=qvec)

        self.store.finish_chain(chain_id, stop_reason)
        self._emit("chain_finish", chain_run_id=chain_id, status=stop_reason)
        return {"chain_run_id": chain_id, "status": stop_reason,
               "answers": answers}
