"""Ядро среды: общее состояние, базовый агент, реестр пайплайнов.

Одна модель на всех: пайплайн — это LangGraph `StateGraph`, узлы которого
либо агенты (LLM + промпт + разбор ответа), либо детерминированные
инструменты (расчёты, парсеры, геометрия). Состояние всегда наследует
`BaseState`, поэтому трассировка, стоимость и артефакты собираются
одинаково для всех семи продуктов.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .config import Settings, settings as default_settings
from .llm import get_llm


# --------------------------------------------------------------------------
# состояние
# --------------------------------------------------------------------------
def merge_lists(left: list, right: list) -> list:
    return (left or []) + (right or [])


def merge_dicts(left: dict, right: dict) -> dict:
    out = dict(left or {})
    out.update(right or {})
    return out


class BaseState(TypedDict, total=False):
    """Общий контракт состояния для всех пайплайнов."""

    task: dict[str, Any]
    trace: Annotated[list[dict[str, Any]], merge_lists]
    artifacts: Annotated[dict[str, Any], merge_dicts]
    findings: Annotated[list[dict[str, Any]], merge_lists]
    errors: Annotated[list[str], merge_lists]
    report: str


def new_state(task: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "task": task or {},
        "trace": [],
        "artifacts": {},
        "findings": [],
        "errors": [],
        "report": "",
    }


# --------------------------------------------------------------------------
# агент
# --------------------------------------------------------------------------
@dataclass
class Agent:
    """Роль = системный промпт + необязательная JSON-схема ответа.

    `schema_hint` служит двум целям: подсказывает модели форму ответа и
    даёт оффлайн-провайдеру скелет, который тот вернёт как есть.
    """

    name: str
    system: str
    schema_hint: dict | list | None = None
    llm: BaseChatModel | None = None
    cfg: Settings | None = None

    def _model(self) -> BaseChatModel:
        if self.llm is None:
            self.llm = get_llm(self.cfg or default_settings())
        return self.llm

    def prompt(self, user: str) -> list:
        sys = self.system
        if self.schema_hint is not None:
            sys += (
                "\n\nОтветь ТОЛЬКО валидным JSON по схеме ниже, без пояснений.\n"
                "JSON_SCHEMA_HINT: "
                + json.dumps(self.schema_hint, ensure_ascii=False)
            )
        return [SystemMessage(content=sys), HumanMessage(content=user)]

    def run_text(self, user: str) -> str:
        msg = self._model().invoke(self.prompt(user))
        return str(msg.content)

    def run_json(self, user: str, default: Any = None) -> Any:
        raw = self.run_text(user)
        parsed = parse_json(raw)
        if parsed is None:
            return default if default is not None else self.schema_hint
        return parsed


def parse_json(raw: str) -> Any | None:
    """Достать JSON из ответа модели, терпя ```-обёртки и болтовню вокруг."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


_MISSING = object()


def task_input(task: dict, key: str, demo: Callable[[], Any]) -> Any:
    """Взять вход из задачи, подставив демо-данные только если ключа нет.

    Именно `key not in task`, а не `task.get(key) or demo()`: пустой список
    от клиента — это осмысленный вход («участков не найдено»), и подменять
    его демо-данными значит вернуть счёт за чужую выдумку.
    """
    value = task.get(key, _MISSING)
    return demo() if value is _MISSING else value


def step(name: str, **payload: Any) -> dict[str, Any]:
    """Запись в трассу — единый формат для логов и отчётов."""
    return {"node": name, "ts": round(time.time(), 3), **payload}


# --------------------------------------------------------------------------
# реестр
# --------------------------------------------------------------------------
@dataclass
class Pipeline:
    """Описание продукта: как собрать граф и чем его накормить в демо."""

    slug: str
    title: str
    summary: str
    build: Callable[..., Any]
    demo_task: Callable[[], dict[str, Any]]
    agents: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


REGISTRY: dict[str, Pipeline] = {}


def register(p: Pipeline) -> Pipeline:
    REGISTRY[p.slug] = p
    return p


def load_registry() -> dict[str, Pipeline]:
    """Импортировать все пайплайны (побочный эффект — регистрация)."""
    from . import pipelines  # noqa: F401

    pipelines.load_all()
    return REGISTRY


def get_pipeline(slug: str) -> Pipeline:
    reg = load_registry()
    if slug not in reg:
        raise KeyError(f"пайплайн {slug!r} не найден. Есть: {', '.join(sorted(reg))}")
    return reg[slug]


def run_pipeline(
    slug: str,
    task: dict[str, Any] | None = None,
    cfg: Settings | None = None,
) -> dict[str, Any]:
    """Собрать граф и прогнать одну задачу до конца."""
    p = get_pipeline(slug)
    graph = p.build(cfg=cfg or default_settings())
    payload = task if task is not None else p.demo_task()
    return graph.invoke(new_state(payload))


def mermaid(slug: str, cfg: Settings | None = None) -> str:
    p = get_pipeline(slug)
    return p.build(cfg=cfg or default_settings()).get_graph().draw_mermaid()
