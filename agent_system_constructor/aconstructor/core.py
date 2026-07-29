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
    """Достать JSON из ответа модели, терпя типичные отклонения.

    Облачные модели почти всегда отдают чистый JSON. Локальные (Ollama,
    llama.cpp) — нет: одинарные кавычки, висящая запятая, `True`/`None`
    из Python, комментарии, преамбула «Sure! Here is...», обрыв ответа по
    лимиту токенов. Каждый такой случай без починки означает, что сервис
    молча вернёт пустой каркас схемы, и пользователь получит отчёт ни о
    чём.

    Порядок попыток — от самой честной к самой рискованной, чтобы
    корректный JSON никогда не пострадал от эвристик.
    """
    text = (raw or "").strip()
    if not text:
        return None

    fence = re.search(r"```(?:json|JSON)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    for candidate in _json_candidates(text):
        for attempt in (candidate, _relax(candidate), _close_truncated(candidate)):
            if not attempt:
                continue
            try:
                return json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _json_candidates(text: str) -> list[str]:
    """Сам текст и самые внешние {...} / [...] из него."""
    out = [text]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            out.append(text[start : end + 1])
    return out


_COMMENT = re.compile(r"(?<!:)//[^\n\r]*|/\*.*?\*/", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _relax(text: str) -> str:
    """Привести питоньи и JS-вольности к валидному JSON."""
    out = _COMMENT.sub("", text)
    out = re.sub(r"\bTrue\b", "true", out)
    out = re.sub(r"\bFalse\b", "false", out)
    out = re.sub(r"\b(?:None|undefined|NaN)\b", "null", out)
    out = _TRAILING_COMMA.sub(r"\1", out)
    if '"' not in out and "'" in out:
        # Одинарные кавычки заменяем только если двойных нет вовсе, иначе
        # апостроф внутри строки («Ivan's») сломал бы корректный ответ.
        out = out.replace("'", '"')
    return out


def _close_truncated(text: str) -> str | None:
    """Закрыть скобки у ответа, обрезанного лимитом токенов.

    Режем по последней позиции, где все открытые контейнеры были целыми
    (запятая между элементами массива или объекта верхнего уровня).
    Резать по любой запятой нельзя: запятая внутри незакрытого объекта
    оставит его половину, и скобки закроются в неверном порядке.

    Последний неполный элемент отбрасываем: половина объекта — не данные.
    Лучше вернуть два распознанных товара из трёх, чем ничего.
    """
    stack: list[str] = []
    in_str = esc = False
    cut = None          # позиция среза
    cut_stack: list[str] | None = None   # каким был стек на этот момент

    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            # Элемент закрылся целиком — сюда можно безопасно обрезать.
            cut, cut_stack = i + 1, list(stack)
        elif ch == ",":
            # Запятая после целого элемента: срез до неё тоже безопасен.
            if cut is not None and text[cut:i].strip() == "":
                cut, cut_stack = i, list(stack)

    if not stack:
        return None            # текст сбалансирован, чинить нечего
    if cut is not None and cut_stack is not None:
        return text[:cut].rstrip().rstrip(",") + "".join(reversed(cut_stack))

    # Ни один вложенный элемент не закрылся: `{"a": 1, "b": 2`.
    # Отбрасываем хвост после последней запятой верхнего уровня — он и есть
    # оборванная пара «ключ: значение».
    head = text.rstrip().rstrip(",")
    if in_str:
        head = head[: head.rfind('"')] if '"' in head else head
    tail = head.rfind(",")
    if tail != -1 and ":" in head[tail:]:
        # Хвост после запятой — целая пара? Тогда оставляем как есть.
        if head[tail:].count('"') % 2 == 0 and not head[tail:].rstrip().endswith(":"):
            return head + "".join(reversed(stack))
        head = head[:tail]
    elif tail != -1:
        head = head[:tail]
    head = head.rstrip().rstrip(",").rstrip()
    if head.rstrip().endswith(":"):
        head = head[: head.rfind(",")] if "," in head else None
    # Пустой каркас вроде «{» данными не является: молча вернуть {} хуже,
    # чем признать ответ неразобранным и предупредить пользователя.
    if not head or head.strip() in ("{", "["):
        return None
    return head + "".join(reversed(stack))


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
