"""Оркестрация НЕСКОЛЬКИХ ролей одной задачей: конвейер профильных прогонов.

ГЛАВНОЕ ОГРАНИЧЕНИЕ АРХИТЕКТУРЫ, КОТОРОЕ ЭТО НЕ НАРУШАЕТ (см.
ARCHITECTURE.md §4, README.md «Это НЕ мультиагентный оркестратор»):
здесь нет «агента, который руководит агентами» — ни один прогон не
решает, что делать дальше, не вызывает другой прогон и не видит его
внутренние рассуждения. Последовательность стадий, их профили и то,
какой текст куда подставить, — задаётся ЗАРАНЕЕ в JSON-определении
конвейера, а не придумывается моделью на лету. Каждая стадия — это
ОБЫЧНЫЙ `Agent.run()`, тот же самый, что для одиночной задачи; конвейер
лишь прогоняет их по очереди и передаёт текст ответа предыдущей стадии
в задачу следующей. Это тот же принцип, что уже описан в
ARCHITECTURE.md для HTTP API («конвейер строится снаружи — обычным
скриптом или cron»), просто оформленный как повторно используемый
JSON-файл с состоянием в Store вместо разового bash-скрипта.

ФОРМАТ ОПРЕДЕЛЕНИЯ (agent/pipelines/<name>.json):
{
  "name": "research_then_report",
  "description": "Изучить материалы, затем собрать отчёт",
  "stages": [
    {"name": "research", "profile": "research",
     "task": "Изучи файлы в рабочей папке и сделай выжимку по теме: {goal}"},
    {"name": "report", "profile": "reporter",
     "task": "На основе этой выжимки собери презентацию:\\n\\n{research}"}
  ]
}

Плейсхолдеры в "task" каждой стадии:
  {goal}       — общая цель конвейера, задаётся при запуске;
  {<имя_стадии>} — ПОЛНЫЙ текст ответа стадии с этим именем, если она
                 уже выполнена (порядок стадий в определении — это и
                 есть порядок выполнения, более раннее имя всегда
                 доступно более поздним стадиям).
Неизвестный плейсхолдер — ошибка ДО запуска (см. validate_pipeline) —
лучше отказ сразу, чем недописанная строка "{опечатка}" в задаче агента.

ОСТАНОВ ПРИ ОШИБКЕ СТАДИИ: если прогон стадии завершился с
stopped_by != "done" (модель не справилась/исчерпала лимит шагов/сбой
модели), конвейер ОСТАНАВЛИВАЕТСЯ — не потому что не может продолжить
технически (было бы можно передать пустую/ошибочную строку дальше), а
потому что тихое продолжение с необработанным сбоем на предыдущей
стадии обычно портит все последующие результаты незаметно для
человека. Явная остановка с понятной причиной лучше.

ПОЧЕМУ КАЖДАЯ СТАДИЯ — ОТДЕЛЬНЫЙ `run` В STORE, А НЕ ПРОСТО СТРОКА:
дашборд уже умеет показывать историю `run`/`event`/трассировку по
run_id (см. agent/webui.py, вкладка «Прогоны») — конвейер переиспользует
этот же механизм для каждой стадии вместо изобретения параллельного
способа просмотра, что вызывалось внутри.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

from .build import build_agent
from .config import Config
from .store import Store

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class PipelineError(Exception):
    """Ожидаемая ошибка конвейера (валидация определения, конфликт) —

    не трейсбек."""


def pipelines_dir() -> Path:
    return Path(__file__).resolve().parent / "pipelines"


def list_pipelines() -> list[str]:
    d = pipelines_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def load_pipeline_def(name: str) -> dict[str, Any]:
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$", name):
        raise PipelineError(f"недопустимое имя конвейера: {name!r}")
    f = pipelines_dir() / f"{name}.json"
    if not f.exists():
        raise PipelineError(
            f"конвейер {name!r} не найден. Доступны: "
            f"{', '.join(list_pipelines()) or '—'}"
        )
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"конвейер {name!r} повреждён: {exc}") from exc
    validate_pipeline(data)
    return data


def validate_pipeline(data: dict[str, Any]) -> None:
    """Проверить определение ДО запуска — отказ сразу лучше, чем сюрприз

    на середине многочасового конвейера. Проверяем: есть хотя бы одна
    стадия, у каждой стадии есть name/task, имена стадий уникальны (иначе
    неоднозначно, какой {ответ} подставлять), а каждый плейсхолдер в task
    ссылается либо на "goal", либо на имя УЖЕ ОПРЕДЕЛЁННОЙ РАНЕЕ стадии
    (стадия не может ссылаться на саму себя или на будущую стадию — это
    был бы цикл или ответ, которого ещё не существует).
    """
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        raise PipelineError("определение конвейера должно содержать stages (список)")

    seen_names: set[str] = set()
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise PipelineError(f"стадия #{i} должна быть объектом")
        name = stage.get("name")
        if not name or not isinstance(name, str):
            raise PipelineError(f"стадия #{i} должна иметь непустое строковое name")
        if name in seen_names:
            raise PipelineError(f"имя стадии {name!r} повторяется — имена должны быть уникальны")
        task = stage.get("task")
        if not task or not isinstance(task, str):
            raise PipelineError(f"стадия {name!r} должна иметь непустое строковое task")
        for placeholder in PLACEHOLDER_RE.findall(task):
            if placeholder == "goal":
                continue
            if placeholder not in seen_names:
                raise PipelineError(
                    f"стадия {name!r} ссылается на {{{placeholder}}}, но это "
                    "не 'goal' и не имя УЖЕ определённой ранее стадии — "
                    "стадия не может ссылаться на себя или на будущую стадию"
                )
        seen_names.add(name)


def _fill_task(template: str, goal: str, answers: dict[str, str]) -> str:
    def repl(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key == "goal":
            return goal
        return answers.get(key, "")
    return PLACEHOLDER_RE.sub(repl, template)


class PipelineRunner:
    """Прогоняет стадии конвейера ПО ОЧЕРЕДИ, каждую — обычным

    build_agent(...).run(task) под своим профилем. Останавливается на
    первой стадии, не завершившейся успехом (см. пояснение в шапке
    модуля), либо по кооперативному stop_event — та же идея, что
    AutoRunner.stop_event в agent/autorun.py: проверяется МЕЖДУ
    стадиями, не убивает поток на середине записи в Store.
    """

    def __init__(self, cfg: Config, store: Store,
                on_event: Callable[[str, dict[str, Any]], None] | None = None,
                stop_event: "threading.Event | None" = None) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event or (lambda k, d: None)
        self.stop_event = stop_event
        self.pipeline_run_id = 0

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    def run(self, pipeline_name: str, goal: str) -> dict[str, Any]:
        definition = load_pipeline_def(pipeline_name)
        stages_def = definition["stages"]
        self.pipeline_run_id = self.store.start_pipeline(
            pipeline_name, goal, stages_def)
        self._emit("pipeline_start", pipeline_run_id=self.pipeline_run_id,
                   name=pipeline_name, goal=goal,
                   stages=[s["name"] for s in stages_def])

        answers: dict[str, str] = {}
        stop_reason = "done"
        stages = self.store.pipeline_stages(self.pipeline_run_id)
        for stage_def, stage_row in zip(stages_def, stages):
            if self.stop_event is not None and self.stop_event.is_set():
                self.store.set_pipeline_stage(stage_row["id"], "skipped")
                stop_reason = "stopped"
                self._emit("stage_skipped", stage=stage_def["name"])
                continue
            if stop_reason != "done":
                # предыдущая стадия провалилась — остальные не запускаем,
                # но отмечаем как skipped, а не оставляем "pending" молча
                self.store.set_pipeline_stage(stage_row["id"], "skipped")
                self._emit("stage_skipped", stage=stage_def["name"])
                continue

            task_text = _fill_task(stage_def["task"], goal, answers)
            profile = stage_def.get("profile") or ""
            self.store.set_pipeline_stage(stage_row["id"], "running", task=task_text)
            self._emit("stage_start", stage=stage_def["name"], profile=profile,
                       task=task_text)

            stage_cfg = Config(**{**self.cfg.__dict__})
            if profile:
                stage_cfg.apply_profile(profile)
            run_id = self.store.start_run(task_text, stage_cfg.profile)

            def watch(kind: str, data: dict[str, Any],
                     _stage=stage_def["name"]) -> None:
                self._emit(kind, stage=_stage, **data)

            try:
                agent = build_agent(stage_cfg, store=self.store,
                                   run_id_getter=lambda rid=run_id: rid,
                                   on_event=watch)
                result = agent.run(task_text)
            except Exception as exc:
                self.store.finish_run(run_id, "failed")
                self.store.set_pipeline_stage(
                    stage_row["id"], "failed", error=str(exc), run_id=run_id)
                self._emit("stage_failed", stage=stage_def["name"], error=str(exc))
                stop_reason = "failed"
                continue

            self.store.bump_run(run_id, steps=len(result.steps),
                                calls=result.tool_calls,
                                tok_in=result.prompt_tokens,
                                tok_out=result.completion_tokens)
            if result.stopped_by != "done":
                self.store.finish_run(run_id, "failed")
                self.store.set_pipeline_stage(
                    stage_row["id"], "failed", answer=result.answer,
                    run_id=run_id,
                    error=f"стадия не завершилась успешно: {result.stopped_by}")
                self._emit("stage_failed", stage=stage_def["name"],
                          error=result.stopped_by)
                stop_reason = "failed"
                continue

            self.store.finish_run(run_id, "done")
            self.store.set_pipeline_stage(
                stage_row["id"], "done", answer=result.answer, run_id=run_id)
            answers[stage_def["name"]] = result.answer
            self._emit("stage_done", stage=stage_def["name"], answer=result.answer)

        self.store.finish_pipeline(self.pipeline_run_id, stop_reason)
        self._emit("pipeline_finish", pipeline_run_id=self.pipeline_run_id,
                   status=stop_reason)
        return {"pipeline_run_id": self.pipeline_run_id, "status": stop_reason,
               "answers": answers}
