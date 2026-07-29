"""Определение workflow: декларативный JSON, проверяемый ДО запуска.

ГЛАВНОЕ АРХИТЕКТУРНОЕ РЕШЕНИЕ СРЕДЫ: последовательность шагов задаётся
ЧЕЛОВЕКОМ заранее, а не придумывается моделью на лету. Нет «агента,
который руководит агентами»: ядро читает объявленный список шагов и
исполняет его. Модель решает, ЧТО написать внутри шага; она не решает,
какой шаг будет следующим, кто его выполнит и какие инструменты ему
разрешены. Тот же принцип, что в agent_system/agent/pipeline.py и
maos/orchestrator/chain.py — здесь он доведён до отдельного формата с
ролями, контрактами доски и точками контроля.

ПОЧЕМУ ВАЛИДАЦИЯ ДО ЗАПУСКА, А НЕ ПО ХОДУ. Опечатка `{reserch}` в
задаче третьего шага не должна обнаруживаться через десять минут и
двести тысяч токенов. `parse_workflow()` проверяет: имена шагов
уникальны, плейсхолдеры ссылаются на существующее (цель, вход, ранее
объявленный ключ доски), `reads` не требует того, чего никто не пишет,
профили и роли известны. Отказ — до первого обращения к модели.

ФОРМАТ (awos/workflows/<name>.json):
{
  "name": "research_brief",
  "title": "Исследование -> бриф",
  "description": "Собрать материал и свести его в бриф",
  "inputs": {"topic": "тема исследования"},
  "steps": [
    {
      "name": "research",
      "title": "Сбор материала",
      "profile": "researcher",
      "task": "Изучи тему: {input.topic}. Собери факты.",
      "writes": "research_notes",
      "review": {"critic": "critic", "min_score": 0.7, "max_revisions": 2},
      "human": "never"
    },
    {
      "name": "brief",
      "profile": "writer",
      "task": "Сведи заметки в бриф:\\n\\n{ctx.research_notes}",
      "reads": ["research_notes"],
      "writes": "brief",
      "human": "always"
    }
  ]
}

Плейсхолдеры в "task":
  {goal}            — цель прогона (передаётся при запуске);
  {input.<имя>}     — значение из inputs, переданное при запуске;
  {ctx.<ключ>}      — последняя версия ключа доски контекста;
  {step.<имя>}      — принятый результат ранее выполненного шага.

Поле "human": never | always | on_reject (умолчание — из конфига среды).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Плейсхолдеры вида {goal}, {input.topic}, {ctx.notes}, {step.research}.
#:
#: \w, а НЕ [a-zA-Z_]: проект русскоязычный, и человек, написавший в
#: задаче {тема} или {ctx.заметки}, обязан получить внятный отказ при
#: проверке определения, а не молча исполняемый шаг с необработанной
#: строкой в промпте (модель добросовестно примет "{тема}" за часть
#: текста). Имена ключей при этом остаются ASCII (NAME_RE) — значит,
#: любой кириллический плейсхолдер заведомо неизвестен и будет отвергнут.
#: Скобки с не-словесным содержимым ({"tool": "x"}, {}) не трогаем —
#: примеры JSON внутри задач встречаются постоянно и ломаться не должны.
PLACEHOLDER_RE = re.compile(r"\{(\w+)(?:\.([\w.-]+))?\}")

#: Политика вызова человека на конкретном шаге.
HUMAN_POLICIES = ("default", "never", "always", "on_reject")

#: Имя ключа доски/шага — то же правило, что для идентификаторов, чтобы
#: ключ можно было безопасно подставлять в {ctx.<key>} и в JSON API.
NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$")


class WorkflowError(Exception):
    """Ошибка определения workflow. Ожидаемая — печатается как текст."""


@dataclass
class ReviewSpec:
    """Настройка цикла качества для шага (Role-Based Collaboration)."""
    critic: str = ""                 # профиль Критика; "" — критика выключена
    supervisor: str = ""             # профиль Контролёра; "" — решает среда
    min_score: float | None = None   # None -> берём из конфига среды
    max_revisions: int | None = None

    @classmethod
    def parse(cls, raw: Any, where: str) -> "ReviewSpec":
        if raw is None or raw is False:
            return cls()
        if raw is True:
            return cls(critic="critic")
        if isinstance(raw, str):
            return cls(critic=raw)
        if not isinstance(raw, dict):
            raise WorkflowError(
                f"{where}: review должен быть объектом, строкой или false")
        spec = cls(critic=str(raw.get("critic", "") or ""),
                   supervisor=str(raw.get("supervisor", "") or ""))
        if "min_score" in raw and raw["min_score"] is not None:
            try:
                spec.min_score = float(raw["min_score"])
            except (TypeError, ValueError) as exc:
                raise WorkflowError(f"{where}: review.min_score — не число") from exc
            if not 0.0 <= spec.min_score <= 1.0:
                raise WorkflowError(f"{where}: review.min_score вне [0..1]")
        if "max_revisions" in raw and raw["max_revisions"] is not None:
            try:
                spec.max_revisions = int(raw["max_revisions"])
            except (TypeError, ValueError) as exc:
                raise WorkflowError(
                    f"{where}: review.max_revisions — не целое") from exc
            if spec.max_revisions < 0:
                raise WorkflowError(f"{where}: review.max_revisions < 0")
        return spec

    def to_dict(self) -> dict[str, Any]:
        return {"critic": self.critic, "supervisor": self.supervisor,
                "min_score": self.min_score, "max_revisions": self.max_revisions}


@dataclass
class StepSpec:
    """Один шаг: кто, что делает, что читает и пишет, кто проверяет."""
    name: str
    task: str
    profile: str = ""
    title: str = ""
    reads: list[str] = field(default_factory=list)
    writes: str = ""
    review: ReviewSpec = field(default_factory=ReviewSpec)
    human: str = "default"
    #: Инструменты, разрешённые ИМЕННО на этом шаге. Пусто — берём из
    #: профиля агента. Список здесь может только СУЗИТЬ то, что уже
    #: разрешено средой (гранты выдаёт конфиг, см. tools/registry.py).
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "profile": self.profile,
                "task": self.task, "reads": list(self.reads),
                "writes": self.writes, "review": self.review.to_dict(),
                "human": self.human, "tools": list(self.tools)}


@dataclass
class Workflow:
    name: str
    steps: list[StepSpec]
    title: str = ""
    description: str = ""
    inputs: dict[str, str] = field(default_factory=dict)

    def step(self, name: str) -> StepSpec | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def outputs(self) -> list[str]:
        """Ключи доски, которые workflow обещает создать."""
        return [s.writes for s in self.steps if s.writes]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title,
                "description": self.description, "inputs": dict(self.inputs),
                "steps": [s.to_dict() for s in self.steps]}


def _placeholders(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2) or "") for m in PLACEHOLDER_RE.finditer(text)]


def parse_workflow(data: dict[str, Any], *, source: str = "<inline>") -> Workflow:
    """Разобрать и ПОЛНОСТЬЮ проверить определение. Ошибка -> WorkflowError."""
    if not isinstance(data, dict):
        raise WorkflowError(f"{source}: ожидался объект JSON верхнего уровня")

    name = str(data.get("name", "") or "").strip()
    if not name:
        raise WorkflowError(f"{source}: не задано поле 'name'")
    if not NAME_RE.match(name):
        raise WorkflowError(
            f"{source}: имя {name!r} — только латиница, цифры, _ и -, до 64 знаков")

    raw_inputs = data.get("inputs", {}) or {}
    if not isinstance(raw_inputs, dict):
        raise WorkflowError(f"{source}: 'inputs' должен быть объектом имя->описание")
    inputs = {str(k): str(v) for k, v in raw_inputs.items()}
    for key in inputs:
        if not NAME_RE.match(key):
            raise WorkflowError(f"{source}: недопустимое имя входа {key!r}")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError(f"{source}: 'steps' должен быть непустым списком")

    steps: list[StepSpec] = []
    seen_names: set[str] = set()
    produced: set[str] = set()          # ключи доски, доступные к этому шагу
    done_steps: set[str] = set()

    for i, raw in enumerate(raw_steps):
        where = f"{source}: шаг #{i}"
        if not isinstance(raw, dict):
            raise WorkflowError(f"{where}: должен быть объектом")
        s_name = str(raw.get("name", "") or "").strip()
        if not s_name:
            raise WorkflowError(f"{where}: не задано 'name'")
        if not NAME_RE.match(s_name):
            raise WorkflowError(f"{where}: недопустимое имя {s_name!r}")
        if s_name in seen_names:
            raise WorkflowError(
                f"{source}: имя шага {s_name!r} встречается дважды — "
                "плейсхолдер {step." + s_name + "} стал бы неоднозначным")
        seen_names.add(s_name)
        where = f"{source}: шаг {s_name!r}"

        task = str(raw.get("task", "") or "").strip()
        if not task:
            raise WorkflowError(f"{where}: пустое поле 'task'")

        raw_reads = raw.get("reads", []) or []
        if isinstance(raw_reads, str):
            raw_reads = [raw_reads]
        if not isinstance(raw_reads, list):
            raise WorkflowError(f"{where}: 'reads' — список ключей доски")
        reads = [str(r) for r in raw_reads]

        writes = str(raw.get("writes", "") or "").strip()
        if writes and not NAME_RE.match(writes):
            raise WorkflowError(f"{where}: недопустимый ключ 'writes' {writes!r}")

        human = str(raw.get("human", "default") or "default").strip()
        if human not in HUMAN_POLICIES:
            raise WorkflowError(
                f"{where}: human={human!r}, допустимо {', '.join(HUMAN_POLICIES)}")

        raw_tools = raw.get("tools", []) or []
        if isinstance(raw_tools, str):
            raw_tools = [raw_tools]
        if not isinstance(raw_tools, list):
            raise WorkflowError(f"{where}: 'tools' — список имён инструментов")

        # --- проверка плейсхолдеров -------------------------------------
        for kind, arg in _placeholders(task):
            if kind == "goal":
                continue
            if kind == "input":
                if arg not in inputs:
                    raise WorkflowError(
                        f"{where}: {{input.{arg}}} — такого входа нет в 'inputs' "
                        f"(объявлены: {', '.join(sorted(inputs)) or '—'})")
                continue
            if kind == "ctx":
                if arg not in produced:
                    raise WorkflowError(
                        f"{where}: {{ctx.{arg}}} — ключ доски ещё никем не "
                        f"записан к этому моменту "
                        f"(доступны: {', '.join(sorted(produced)) or '—'})")
                continue
            if kind == "step":
                if arg not in done_steps:
                    raise WorkflowError(
                        f"{where}: {{step.{arg}}} — шаг ещё не выполнен к этому "
                        f"моменту (доступны: {', '.join(sorted(done_steps)) or '—'})")
                continue
            raise WorkflowError(
                f"{where}: неизвестный плейсхолдер {{{kind}"
                f"{'.' + arg if arg else ''}}} — допустимы goal, input.*, "
                "ctx.*, step.*")

        for key in reads:
            if key not in produced:
                raise WorkflowError(
                    f"{where}: reads={key!r}, но такой ключ доски к этому шагу "
                    f"никто не пишет (доступны: {', '.join(sorted(produced)) or '—'})")

        review = ReviewSpec.parse(raw.get("review"), where)

        steps.append(StepSpec(
            name=s_name, task=task, profile=str(raw.get("profile", "") or ""),
            title=str(raw.get("title", "") or ""), reads=reads, writes=writes,
            review=review, human=human, tools=[str(t) for t in raw_tools]))

        done_steps.add(s_name)
        if writes:
            produced.add(writes)

    return Workflow(name=name, steps=steps,
                    title=str(data.get("title", "") or ""),
                    description=str(data.get("description", "") or ""),
                    inputs=inputs)


def load_workflow(name_or_path: str, directory: Path | None = None) -> Workflow:
    """Загрузить workflow по имени (в каталоге) или по пути к файлу."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".json" and candidate.exists():
        path = candidate
    else:
        base = directory or (Path(__file__).resolve().parent.parent / "workflows")
        path = base / f"{name_or_path}.json"
        if not path.exists():
            known = ", ".join(list_workflows(base)) or "—"
            raise WorkflowError(
                f"Workflow {name_or_path!r} не найден в {base}. Известные: {known}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"{path}: не разбирается как JSON — {exc}") from exc
    wf = parse_workflow(data, source=str(path))
    if wf.name != path.stem and path.parent.name == "workflows":
        # Расхождение имени файла и поля name делает `awos run <name>`
        # непредсказуемым: человек запускает по имени файла, а в журнале
        # видит другое имя. Лучше сразу сказать об этом.
        raise WorkflowError(
            f"{path}: поле name={wf.name!r} не совпадает с именем файла "
            f"{path.stem!r}")
    return wf


def list_workflows(directory: Path | None = None) -> list[str]:
    base = directory or (Path(__file__).resolve().parent.parent / "workflows")
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.json"))


def describe_workflows(directory: Path | None = None) -> list[dict[str, Any]]:
    """Список workflow с заголовками — для CLI и дашборда.

    Битое определение не роняет перечисление: среда обязана показать
    остальные и честно сказать, что именно сломано.
    """
    out: list[dict[str, Any]] = []
    base = directory or (Path(__file__).resolve().parent.parent / "workflows")
    for name in list_workflows(base):
        try:
            wf = load_workflow(name, base)
        except WorkflowError as exc:
            out.append({"name": name, "error": str(exc)})
            continue
        out.append({"name": wf.name, "title": wf.title,
                    "description": wf.description,
                    "steps": [s.name for s in wf.steps],
                    "inputs": wf.inputs})
    return out
