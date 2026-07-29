"""Движок среды: исполнение workflow с ролями, доской, HITL и инструментами.

ЭТО ЯДРО ПЛАТФОРМЫ. Всё остальное — интерфейсы к нему (CLI, HTTP,
дашборд) или строительные блоки (роли, доска, инструменты). Движок
отвечает ровно за одно: провести объявленный workflow от цели до
результата, не потеряв состояние и не приняв брак молча.

ЧТО ДВИЖОК ГАРАНТИРУЕТ.

1. ДЕТЕРМИНИРОВАННЫЙ ПОРЯДОК. Шаги исполняются в объявленном порядке.
   Ни одна модель не решает, какой шаг следующий, кто его выполнит и
   какие инструменты ему доступны. Модель решает только содержание
   ответа внутри шага.

2. ЦИКЛ КАЧЕСТВА НА КАЖДОМ ШАГЕ. Исполнитель -> Критик -> Контролёр.
   Возврат на доработку — это НОВЫЙ ход Исполнителя с замечаниями в
   задаче, а не «попробуй ещё раз» тем же промптом: без явных замечаний
   модель обычно выдаёт тот же текст другими словами.

3. ВОЗОБНОВЛЯЕМОСТЬ. Любая пауза на человеке — запись в базе плюс
   статус `waiting_human`. `resume()` продолжает прогон с того же шага
   в другом процессе, через час или через день.

4. АУДИТ. Каждое событие (ход роли, вердикт, решение, вызов
   инструмента, вопрос человеку и ответ) пишется в журнал прогона до
   того, как повлияет на состояние.

ЧЕГО ДВИЖОК НЕ ДЕЛАЕТ. Не планирует, не декомпозирует цель, не выбирает
исполнителя «по смыслу» — для семантического выбора агента есть
соседний проект MAOS. Здесь план приходит от человека в виде JSON, и
это осознанное ограничение: производственный цикл должен быть
воспроизводимым.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..context.blackboard import Blackboard
from ..hitl.gate import Gate, HumanResponse
from ..llm import build_llm
from ..llm.base import BaseLLM
from ..roles.critic import Verdict, review
from ..roles.profile import Profile, resolve_profile
from ..roles.supervisor import Decision, decide, decide_with_llm
from ..roles.worker import PauseForHuman, run_turn
from ..tools.base import Tool, ToolRegistry, Workspace
from ..tools.protocol import ToolCall
from ..tools.registry import build_registry
from .store import Store
from .workflow import StepSpec, Workflow, WorkflowError, load_workflow


class EngineError(Exception):
    """Ожидаемая ошибка исполнения (нет прогона, нечего возобновлять)."""


@dataclass
class RunOutcome:
    """Итог вызова start()/resume() — не обязательно итог всего прогона."""
    run_id: int
    status: str                        # done | waiting_human | failed | cancelled
    detail: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None

    @property
    def waiting(self) -> bool:
        return self.status == "waiting_human"

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "status": self.status,
                "detail": self.detail, "steps": self.steps,
                "outputs": self.outputs, "checkpoint": self.checkpoint}


class Engine:
    """Исполнитель workflow. Один экземпляр обслуживает много прогонов."""

    def __init__(self, cfg: Config, store: Store | None = None, *,
                 llm_factory: Callable[[Profile], BaseLLM] | None = None,
                 extra_tools: list[Tool] | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None
                 ) -> None:
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.gate = Gate(cfg, self.store)
        self.workspace = Workspace(cfg.workspace)
        self.extra_tools = list(extra_tools or [])
        self.on_event = on_event
        self._llm_factory = llm_factory or self._default_llm
        #: Кэш моделей по (провайдер, модель, температура): один прогон
        #: обращается к одному профилю десятки раз, пересоздавать драйвер
        #: на каждый ход бессмысленно.
        self._llm_cache: dict[tuple, BaseLLM] = {}

    # --- инфраструктура ---------------------------------------------------
    def _default_llm(self, profile: Profile) -> BaseLLM:
        provider = profile.provider or self.cfg.provider
        model = profile.model or self.cfg.model
        key = (provider, model, profile.temperature)
        cached = self._llm_cache.get(key)
        if cached is None:
            cached = build_llm(provider, model, **profile.llm_kwargs(self.cfg))
            self._llm_cache[key] = cached
        return cached

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event:
            try:
                self.on_event(kind, data)
            except Exception:            # noqa: BLE001 — слушатель не должен
                pass                     # ронять прогон

    def _log(self, run_id: int, kind: str, message: str = "", **kw: Any) -> None:
        self.store.log(run_id, kind, message, role=kw.pop("role", ""),
                       step_id=kw.pop("step_id", None), data=kw or None)
        self._emit(kind, run_id=run_id, message=message, **kw)

    def _profile(self, name: str, role: str, run_id: int) -> Profile:
        directory = self.cfg.resolved_profiles_dir()
        profile = resolve_profile(name, role, directory)
        if name and profile.name != name:
            # Подмена на встроенный профиль допустима, но человек обязан
            # узнать об этом из журнала, а не гадать, почему агент повёл
            # себя как generic-исполнитель.
            self._log(run_id, "profile_fallback",
                      f"Профиль {name!r} не найден — работает встроенный "
                      f"{profile.name!r}", role=role)
        return profile

    def _registry(self, board: Blackboard, step: StepSpec,
                  profile: Profile) -> ToolRegistry:
        reg = build_registry(
            self.cfg, workspace=self.workspace,
            ctx_read=lambda k: board.get(k),
            ctx_write=lambda k, v: board.put(k, v, author=f"tool:{step.name}"),
            ctx_keys=board.keys, extra=self.extra_tools)
        # Сужение: сначала профилем, потом шагом. Обратный порядок дал бы
        # шагу возможность вернуть инструмент, отобранный у профиля.
        if profile.tools:
            reg = reg.subset(profile.tools)
        if step.tools:
            reg = reg.subset(step.tools)
        return reg

    # --- запуск -----------------------------------------------------------
    def start(self, workflow: str | Workflow, *, goal: str = "",
              inputs: dict[str, Any] | None = None) -> RunOutcome:
        wf = (workflow if isinstance(workflow, Workflow)
              else load_workflow(workflow, self.cfg.resolved_workflows_dir()))

        missing = [k for k in wf.inputs if k not in (inputs or {})]
        if missing:
            # Ошибка ДО создания прогона: незачем плодить мёртвые записи.
            raise WorkflowError(
                f"Не переданы обязательные входы: {', '.join(missing)}. "
                f"Ожидаются: {', '.join(f'{k} — {v}' for k, v in wf.inputs.items())}")

        run_id = self.store.create_run(wf.name, goal)
        for i, step in enumerate(wf.steps):
            self.store.add_step(run_id, i, step.name, step.profile)
        board = Blackboard(self.store, run_id, goal=goal, inputs=inputs or {})
        # Определение и входы кладём на доску: прогон обязан быть
        # самодостаточным. Файл workflow могут отредактировать завтра,
        # а разбирать инцидент придётся по тому, что исполнялось реально.
        board.put("_workflow", wf.to_dict(), author="engine")
        board.put("_inputs", dict(inputs or {}), author="engine")
        board.put("_goal", goal, author="engine")

        self._log(run_id, "run_start", f"{wf.name}: {goal or '(без цели)'}",
                  workflow=wf.name, steps=len(wf.steps))
        return self._drive(run_id, wf, board)

    def resume(self, run_id: int) -> RunOutcome:
        """Продолжить прогон после ответа человека (возможно, в другом процессе)."""
        run = self.store.require_run(run_id)
        if run["status"] in ("done", "failed", "cancelled"):
            raise EngineError(
                f"Прогон #{run_id} уже завершён со статусом {run['status']!r}")

        pending = self.store.pending_checkpoint(run_id)
        if pending is not None:
            raise EngineError(
                f"Прогон #{run_id} ждёт решения человека по точке контроля "
                f"#{pending['id']}: {pending['question']}")

        board = self._restore_board(run_id, run)
        wf = self._restore_workflow(run_id, run)
        self.store.set_run_status(run_id, "running")
        self._log(run_id, "run_resume", "Прогон продолжен")
        return self._drive(run_id, wf, board)

    def cancel(self, run_id: int, reason: str = "отменён оператором") -> None:
        run = self.store.require_run(run_id)
        if run["status"] in ("done", "failed", "cancelled"):
            return
        for cp in self.store.list_checkpoints(run_id, status="pending"):
            self.store.resolve_checkpoint(int(cp["id"]), "cancelled", reason,
                                          actor="engine")
        self.store.set_run_status(run_id, "cancelled", reason)
        self._log(run_id, "run_cancel", reason)

    def _restore_board(self, run_id: int, run: dict[str, Any]) -> Blackboard:
        return Blackboard(self.store, run_id,
                          goal=self.store.ctx_get(run_id, "_goal", run["goal"]) or "",
                          inputs=self.store.ctx_get(run_id, "_inputs", {}) or {})

    def _restore_workflow(self, run_id: int, run: dict[str, Any]) -> Workflow:
        """Взять определение, с которым прогон СТАРТОВАЛ, а не файл с диска.

        Файл могли отредактировать между запуском и возобновлением;
        подхватить новую редакцию посреди прогона — верный способ
        получить несуществующие ключи доски и необъяснимое поведение.
        """
        from .workflow import parse_workflow
        saved = self.store.ctx_get(run_id, "_workflow")
        if isinstance(saved, dict):
            return parse_workflow(saved, source=f"run#{run_id}")
        return load_workflow(run["workflow"], self.cfg.resolved_workflows_dir())

    # --- основной цикл -------------------------------------------------
    def _drive(self, run_id: int, wf: Workflow, board: Blackboard) -> RunOutcome:
        step_outputs: dict[str, str] = {}
        for row in self.store.steps(run_id):
            if row["status"] == "done" and row["output"]:
                step_outputs[row["name"]] = row["output"]

        while True:
            row = self.store.next_pending_step(run_id)
            if row is None:
                self.store.set_run_status(run_id, "done")
                self._log(run_id, "run_done", "Все шаги выполнены")
                return self._outcome(run_id, board)

            spec = wf.step(row["name"])
            if spec is None:
                detail = (f"Шаг {row['name']!r} есть в прогоне, но отсутствует "
                          "в определении workflow")
                self.store.update_step(int(row["id"]), status="failed",
                                       detail=detail)
                self.store.set_run_status(run_id, "failed", detail)
                self._log(run_id, "run_failed", detail)
                return self._outcome(run_id, board)

            outcome = self._run_step(run_id, spec, board, step_outputs, row)
            if outcome is not None:
                return outcome

            done = self.store.get_step(int(row["id"]))
            if done and done["output"]:
                step_outputs[spec.name] = done["output"]

    def _outcome(self, run_id: int, board: Blackboard) -> RunOutcome:
        run = self.store.require_run(run_id)
        steps = [{"name": s["name"], "status": s["status"], "score": s["score"],
                  "revisions": s["revisions"], "output": s["output"],
                  "detail": s["detail"]}
                 for s in self.store.steps(run_id)]
        outputs = {k: v for k, v in board.snapshot().items()
                   if not k.startswith("_")}
        checkpoint = self.store.pending_checkpoint(run_id)
        return RunOutcome(run_id=run_id, status=run["status"],
                          detail=run["detail"] or "", steps=steps,
                          outputs=outputs, checkpoint=checkpoint)

    # --- один шаг ---------------------------------------------------------
    def _run_step(self, run_id: int, spec: StepSpec, board: Blackboard,
                  step_outputs: dict[str, str],
                  row: dict[str, Any]) -> RunOutcome | None:
        """Выполнить шаг целиком. None — идём дальше; RunOutcome — остановка."""
        step_id = int(row["id"])
        self.store.update_step(step_id, status="running")
        self._log(run_id, "step_start", spec.title or spec.name,
                  step_id=step_id, step=spec.name, profile=spec.profile)

        worker_profile = self._profile(spec.profile, "worker", run_id)
        worker_llm = self._llm_factory(worker_profile)

        try:
            task = board.render(spec.task, step_outputs)
        except WorkflowError as exc:
            return self._fail_step(run_id, step_id, spec, str(exc), board)

        ctx_block = board.context_block(spec.reads)
        if ctx_block:
            task = f"{ctx_block}\n\n---\n\nЗадача:\n{task}"

        registry = self._registry(board, spec, worker_profile)
        max_steps = (worker_profile.max_tool_steps
                     if worker_profile.max_tool_steps is not None
                     else self.cfg.max_tool_steps)

        max_revisions = (spec.review.max_revisions
                         if spec.review.max_revisions is not None
                         else self.cfg.max_revisions)
        min_score = (spec.review.min_score if spec.review.min_score is not None
                     else self.cfg.min_score)

        feedback = ""
        revision = int(row["revisions"] or 0)
        output = ""
        verdict: Verdict | None = None

        while True:
            prompt = task if not feedback else (
                f"{task}\n\n---\n\nПРЕДЫДУЩАЯ ВЕРСИЯ ВЕРНУЛАСЬ НА ДОРАБОТКУ.\n"
                f"Замечания проверяющего:\n{feedback}\n\n"
                f"Прошлый вариант:\n{output}\n\n"
                "Исправь именно эти замечания. Не переписывай то, что уже верно."
            )

            try:
                turn = run_turn(
                    worker_llm, worker_profile.system, prompt, tools=registry,
                    max_tool_steps=max_steps,
                    output_limit=self.cfg.tool_output_limit,
                    on_tool=lambda call, out, ok, elapsed: self._on_tool(
                        run_id, step_id, call, out, ok, elapsed),
                    confirm=lambda call: not self.gate.needs_tool_confirm(
                        bool(registry.get(call.tool) and
                             registry.get(call.tool).dangerous)))
            except PauseForHuman as pause:
                return self._pause_for_tool(run_id, step_id, spec, pause, board)

            self.store.bump_run(run_id, tokens_in=turn.usage.tokens_in,
                                tokens_out=turn.usage.tokens_out,
                                llm_calls=turn.llm_calls)

            if turn.stopped_by == "llm_error":
                return self._fail_step(run_id, step_id, spec,
                                       f"Модель Исполнителя недоступна: "
                                       f"{turn.detail}", board)

            output = turn.text.strip()
            self._log(run_id, "worker_output",
                      output[:400] + ("…" if len(output) > 400 else ""),
                      role="worker", step_id=step_id, revision=revision,
                      tool_calls=turn.tool_calls, stopped_by=turn.stopped_by)

            if not output:
                # Пустой ответ — не результат. Даём доработку, если есть.
                verdict = Verdict(score=0.0, verdict="reject",
                                  issues=["Исполнитель вернул пустой ответ."],
                                  summary="Пустой результат.")
            elif spec.review.critic:
                verdict = self._review(run_id, step_id, spec, task, output,
                                       board)
            else:
                # Критик не назначен — качество не проверяется. Это законный
                # режим для механических шагов (собрать файл, отправить),
                # и среда обязана сказать об этом в журнале честно.
                self._log(run_id, "review_skipped",
                          "Критик для шага не назначен — проверка не проводилась",
                          role="critic", step_id=step_id)
                verdict = Verdict(score=1.0, verdict="accept",
                                  summary="Проверка не назначена.")

            revisions_left = max_revisions - revision
            decision = self._decide(run_id, step_id, spec, task, output,
                                    verdict, revisions_left, min_score)

            self.store.update_step(step_id, score=verdict.score,
                                   revisions=revision)

            if decision.decision == "revise":
                revision += 1
                feedback = verdict.feedback()
                self.store.update_step(step_id, revisions=revision,
                                       detail=decision.reason)
                self._log(run_id, "revision",
                          f"Доработка #{revision}: {decision.reason}",
                          role="supervisor", step_id=step_id)
                continue

            if decision.decision == "fail":
                return self._fail_step(run_id, step_id, spec, decision.reason,
                                       board, output=output)

            if decision.decision == "escalate":
                return self._pause_for_approval(
                    run_id, step_id, spec, board, output, verdict,
                    question=("Качество ниже порога, доработки исчерпаны. "
                              "Утвердить результат, отклонить прогон или "
                              "прислать исправленный текст?"),
                    kind="approval")

            # accept — но, возможно, шаг всё равно требует утверждения.
            if self.gate.needs_approval(spec.human):
                return self._pause_for_approval(
                    run_id, step_id, spec, board, output, verdict,
                    question="Утвердите результат шага (можно прислать правку).",
                    kind="approval")

            self._accept(run_id, step_id, spec, board, output, decision.reason)
            return None

    # --- вспомогательные части шага ----------------------------------------
    def _review(self, run_id: int, step_id: int, spec: StepSpec, task: str,
                output: str, board: Blackboard) -> Verdict:
        profile = self._profile(spec.review.critic, "critic", run_id)
        llm = self._llm_factory(profile)
        verdict = review(llm, profile.system, task, output,
                         context=board.context_block(spec.reads))
        self.store.bump_run(run_id, tokens_in=verdict.usage.tokens_in,
                            tokens_out=verdict.usage.tokens_out, llm_calls=1)
        self._log(run_id, "critic_verdict",
                  f"score={verdict.score:.2f} verdict={verdict.verdict} "
                  f"issues={len(verdict.issues)}",
                  role="critic", step_id=step_id, **verdict.to_dict())
        return verdict

    def _decide(self, run_id: int, step_id: int, spec: StepSpec, task: str,
                output: str, verdict: Verdict, revisions_left: int,
                min_score: float) -> Decision:
        hitl = self.gate.needs_escalation(spec.human)
        if spec.review.supervisor:
            profile = self._profile(spec.review.supervisor, "supervisor", run_id)
            llm = self._llm_factory(profile)
            decision = decide_with_llm(llm, profile.system, task, output,
                                       verdict, revisions_left=revisions_left,
                                       min_score=min_score, hitl_enabled=hitl)
            self.store.bump_run(run_id, tokens_in=decision.usage.tokens_in,
                                tokens_out=decision.usage.tokens_out,
                                llm_calls=1)
        else:
            decision = decide(verdict, min_score=min_score,
                              revisions_left=revisions_left, hitl_enabled=hitl)
        self._log(run_id, "supervisor_decision",
                  f"{decision.decision}: {decision.reason}",
                  role="supervisor", step_id=step_id, by=decision.by)
        return decision

    def _on_tool(self, run_id: int, step_id: int, call: ToolCall, out: str,
                 ok: bool, elapsed: float) -> None:
        self.store.log_tool_call(run_id, step_id, call.tool, call.args, ok,
                                 out, elapsed)
        self.store.bump_run(run_id, tools=1)
        self._log(run_id, "tool_call", f"{call.tool} -> {'ok' if ok else 'ошибка'}",
                  role="worker", step_id=step_id, tool=call.tool, ok=ok,
                  args=call.args)

    def _accept(self, run_id: int, step_id: int, spec: StepSpec,
                board: Blackboard, output: str, reason: str) -> None:
        self.store.update_step(step_id, status="done", output=output,
                               detail=reason)
        self.store.bump_run(run_id, steps=1)
        if spec.writes:
            board.put(spec.writes, output, author=f"step:{spec.name}")
            self._log(run_id, "context_write", f"{spec.writes} <- {spec.name}",
                      step_id=step_id, key=spec.writes, size=len(output))
        self._log(run_id, "step_done", spec.title or spec.name, step_id=step_id,
                  step=spec.name)

    def _fail_step(self, run_id: int, step_id: int, spec: StepSpec,
                   reason: str, board: Blackboard,
                   output: str = "") -> RunOutcome:
        self.store.update_step(step_id, status="failed", detail=reason,
                               output=output)
        self.store.set_run_status(run_id, "failed", reason)
        self._log(run_id, "step_failed", reason, step_id=step_id, step=spec.name)
        self._log(run_id, "run_failed", reason)
        return self._outcome(run_id, board)

    # --- паузы на человеке -------------------------------------------------
    def _pause_for_approval(self, run_id: int, step_id: int, spec: StepSpec,
                            board: Blackboard, output: str,
                            verdict: Verdict | None, question: str,
                            kind: str) -> RunOutcome | None:
        payload = {"step": spec.name, "output": output,
                   "verdict": verdict.to_dict() if verdict else None,
                   "writes": spec.writes}
        cp_id = self.gate.ask(run_id, step_id, kind, question, payload)
        self.store.update_step(step_id, status="waiting_human", output=output)
        self.store.set_run_status(run_id, "waiting_human", question)

        answer = self.gate.wait(cp_id)
        if answer is None:
            # Человек не ответил сейчас — прогон засыпает В БАЗЕ. Это не
            # ошибка и не потеря: resume() поднимет его с этого же места.
            self._log(run_id, "run_waiting", question, step_id=step_id,
                      checkpoint_id=cp_id)
            return self._outcome(run_id, board)

        return self._apply_human(run_id, step_id, spec, board, output, answer)

    def _pause_for_tool(self, run_id: int, step_id: int, spec: StepSpec,
                        pause: PauseForHuman, board: Blackboard) -> RunOutcome:
        payload = {"step": spec.name, "tool": pause.call.tool,
                   "args": pause.call.args}
        cp_id = self.gate.ask(run_id, step_id, "tool", pause.question, payload)
        self.store.update_step(step_id, status="waiting_human")
        self.store.set_run_status(run_id, "waiting_human", pause.question)
        answer = self.gate.wait(cp_id)
        if answer is None:
            self._log(run_id, "run_waiting", pause.question, step_id=step_id,
                      checkpoint_id=cp_id)
            return self._outcome(run_id, board)

        if answer.approved:
            # Разрешение действует на ОДИН вызов и не сохраняется: иначе
            # одно «да» открывало бы shell до конца прогона.
            self.store.update_step(step_id, status="pending")
            self.store.set_run_status(run_id, "running")
            self._log(run_id, "tool_approved", pause.call.tool, step_id=step_id,
                      role="human")
            return self._outcome(run_id, board)

        reason = answer.response or "Человек запретил вызов инструмента"
        return self._fail_step(run_id, step_id, spec, reason, board)

    def _apply_human(self, run_id: int, step_id: int, spec: StepSpec,
                     board: Blackboard, output: str,
                     answer: HumanResponse) -> RunOutcome | None:
        """Применить решение человека к шагу.

        edited — человек прислал ИСПРАВЛЕННЫЙ текст: он и становится
        результатом шага. Это дешевле любой доработки моделью и
        единственный способ гарантированно получить нужный результат,
        когда модель раз за разом промахивается.
        """
        if answer.status == "cancelled":
            self.store.update_step(step_id, status="failed",
                                   detail="Отменено человеком")
            self.store.set_run_status(run_id, "cancelled", "Отменено человеком")
            self._log(run_id, "run_cancel", "Отменено человеком", step_id=step_id)
            return self._outcome(run_id, board)

        if answer.status == "rejected":
            reason = answer.response or "Человек отклонил результат шага"
            return self._fail_step(run_id, step_id, spec, reason, board,
                                   output=output)

        final = answer.response.strip() if answer.status == "edited" else output
        if answer.status == "edited" and not final:
            final = output
        self.store.set_run_status(run_id, "running")
        self._accept(run_id, step_id, spec, board, final,
                     f"Утверждено человеком ({answer.actor})")
        self._log(run_id, "human_approved",
                  "с правкой" if answer.status == "edited" else "без правок",
                  role="human", step_id=step_id)
        return None

    # --- ответ человека извне ------------------------------------------------
    def respond(self, checkpoint_id: int, status: str, response: str = "",
                actor: str = "human", *, auto_resume: bool = True) -> RunOutcome:
        """Ответить на точку контроля и (по умолчанию) сразу продолжить прогон.

        Это вход для CLI, HTTP API и дашборда. Порядок важен: сначала
        фиксируем решение человека в базе, и только потом продолжаем —
        если процесс умрёт между этими действиями, решение не потеряется,
        а прогон подхватит следующий `resume`.
        """
        row = self.store.get_checkpoint(checkpoint_id)
        if row is None:
            raise EngineError(f"Точка контроля #{checkpoint_id} не найдена")
        run_id = int(row["run_id"])
        self.gate.resolve(checkpoint_id, status, response, actor)

        if not auto_resume:
            return self._outcome(run_id, self._restore_board(
                run_id, self.store.require_run(run_id)))

        run = self.store.require_run(run_id)
        board = self._restore_board(run_id, run)
        wf = self._restore_workflow(run_id, run)
        step_row = self.store.get_step(int(row["step_id"])) if row["step_id"] else None

        if row["kind"] == "approval" and step_row is not None:
            spec = wf.step(step_row["name"])
            if spec is None:
                raise EngineError(
                    f"Шаг {step_row['name']!r} отсутствует в определении")
            answer = HumanResponse(status=status, response=response, actor=actor,
                                   checkpoint_id=checkpoint_id)
            stop = self._apply_human(run_id, int(step_row["id"]), spec, board,
                                     step_row["output"] or "", answer)
            if stop is not None:
                return stop
            return self._drive(run_id, wf, board)

        if row["kind"] == "tool" and step_row is not None:
            spec = wf.step(step_row["name"])
            if spec is None:
                raise EngineError(
                    f"Шаг {step_row['name']!r} отсутствует в определении")
            if status in ("approved", "edited"):
                self.store.update_step(int(step_row["id"]), status="pending")
                self.store.set_run_status(run_id, "running")
                self._log(run_id, "tool_approved", str(row["payload"].get("tool")),
                          step_id=int(step_row["id"]), role="human")
                return self._drive(run_id, wf, board)
            reason = response or "Человек запретил вызов инструмента"
            return self._fail_step(run_id, int(step_row["id"]), spec, reason,
                                   board)

        # input или точка без шага: просто продолжаем прогон.
        self.store.set_run_status(run_id, "running")
        return self._drive(run_id, wf, board)

    # --- сводка ---------------------------------------------------------
    def status(self, run_id: int) -> dict[str, Any]:
        run = self.store.require_run(run_id)
        return {
            "run": run,
            "steps": self.store.steps(run_id),
            "context": self.store.ctx_all(run_id),
            "checkpoint": self.store.pending_checkpoint(run_id),
            "events": self.store.events(run_id, limit=200),
        }
