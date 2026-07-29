"""Авто-выбор профиля агента под задачу: гибрид эвристика + LLM-фолбэк.

Зачем: сейчас профиль (роль) выбирает человек флагом -P/--profile или
полем "profile" в конфиге/запросе. Этот модуль даёт альтернативу — по
тексту задачи подобрать подходящий профиль автоматически, чтобы
пользователь просто сформулировал, что нужно, а систему не заботил
вопрос "каким агентом это делать".

Два уровня, от дешёвого к дорогому:
  1. ЭВРИСТИКА по ключевым словам — та же грубая стемминг-эвристика, что
     Store использует для поиска по памяти (_stem), применённая к полю
     "description" и опциональному "keywords" каждого профиля. Бесплатно,
     мгновенно, детерминированно и легко тестируется: типичный случай
     ("почини баг в тестах" -> coder, "спроектируй редуктор" -> cad)
     разруливается без единого обращения к модели.
  2. LLM-ФОЛБЭК — если эвристика не даёт уверенного лидера (нет явных
     совпадений или несколько профилей набрали сопоставимый счёт),
     один короткий вызов модели: выбрать ОДНО имя из СПИСКА РЕАЛЬНО
     СУЩЕСТВУЮЩИХ профилей (не выдумывать своё). Ответ, не совпавший ни
     с одним именем, игнорируется — тогда решение падает на лучший
     эвристический вариант либо профиль по умолчанию.

Никогда не роняет вызывающий код: сетевая ошибка LLM ловится здесь же
и трактуется как "фолбэк недоступен", а не как сбой всего решения.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .llm.base import BaseLLM, LLMError

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MIN_WORD = 3

#: если ни один профиль не найден и не подобран моделью — по этой роли
#: нет специфичных допущений, только files/shell, безопасный минимум
DEFAULT_PROFILE = "coder"


def _stem(word: str) -> str:
    """Та же грубая эвристика, что Store._stem — общий принцип поиска по
    основе слова между памятью и подбором профиля, без второй реализации
    морфологии."""
    w = word.lower()
    if len(w) <= 3:
        return w
    return w[:3] if len(w) <= 5 else w[:max(4, int(len(w) * 0.75))]


def _terms(text: str) -> set[str]:
    words = _WORD_RE.findall(text)
    return {_stem(w) for w in words if len(w) >= _MIN_WORD}


@dataclass
class ProfileInfo:
    """То, что нужно роутеру от профиля: имя, описание и ключевые слова.

    keywords — необязательное поле профиля (agent/profiles/*.json),
    расширяющее description явными терминами предметной области; без
    него профиль всё равно участвует в подборе, но менее уверенно.
    """
    name: str
    description: str
    keywords: list[str]


@dataclass
class RouteDecision:
    profile: str
    method: str          # "heuristic" | "llm" | "default" | "explicit"
    score: float = 0.0
    reason: str = ""


ROUTER_SYSTEM = (
    "Ты выбираешь наиболее подходящий профиль (роль) агента под задачу "
    "пользователя. ПРАВИЛА: ответь РОВНО ОДНИМ именем из списка, дословно "
    "как оно указано, без кавычек и пояснений; не выдумывай новых имён; "
    "если ни один профиль явно не подходит — ответь словом none."
)


@dataclass
class _Candidate:
    name: str
    hits: int          # сколько разных термов задачи узнал профиль
    precision: float    # hits / размер словаря профиля — специфичность


def _heuristic_rank(task: str, profiles: list[ProfileInfo]) -> list[_Candidate]:
    """Ранжирует профили по числу совпавших термов задачи.

    ПОЧЕМУ не hits/pool_size как основной критерий: это несправедливо
    наказывает профили с длинным списком ключевых слов — одно точное
    совпадение (например "накладная" у intake) получало бы более низкий
    счёт, чем случайное совпадение у профиля с коротким описанием, просто
    из-за размера словаря. Основной критерий — RAW-число совпадений
    (сколько разных понятий задачи узнал профиль); precision (доля от
    размера словаря) используется только как tie-break при равном числе
    совпадений, чтобы более специфичный профиль выигрывал у более общего
    при прочих равных.
    """
    task_terms = _terms(task)
    if not task_terms:
        return []
    scored: list[_Candidate] = []
    for p in profiles:
        pool = _terms(p.description) | {_stem(k) for k in p.keywords}
        if not pool:
            continue
        hits = len(task_terms & pool)
        if hits:
            scored.append(_Candidate(p.name, hits, hits / len(pool)))
    scored.sort(key=lambda c: (-c.hits, -c.precision))
    return scored


def _llm_pick(task: str, profiles: list[ProfileInfo], llm: BaseLLM) -> str | None:
    listing = "\n".join(f"- {p.name}: {p.description}" for p in profiles)
    prompt = (f"Задача пользователя:\n{task}\n\n"
             f"Доступные профили:\n{listing}\n\n"
             "Ответь ОДНИМ словом — именем профиля.")
    try:
        reply = llm.chat([
            {"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": prompt},
        ], tools=None)
    except LLMError:
        return None
    guess = (reply.text or "").strip().strip("\"'.,!").lower()
    for p in profiles:
        if p.name.lower() == guess:
            return p.name
    return None


def pick_profile(
    task: str,
    profiles: list[ProfileInfo],
    llm: BaseLLM | None = None,
    min_hits: int = 1,
    default: str = DEFAULT_PROFILE,
) -> RouteDecision:
    """Выбрать профиль под задачу: эвристика -> LLM-фолбэк -> дефолт.

    Эвристике доверяем БЕЗ обращения к модели, когда есть однозначный
    лидер: либо единственный кандидат, либо кандидат с БОЛЬШИМ числом
    совпадений, чем у второго места (не долями словаря — см.
    _heuristic_rank). min_hits — минимум совпавших термов даже для
    единственного кандидата: одно случайное совпадение короткого слова
    не должно решать выбор роли без подтверждения моделью.
    """
    if not profiles:
        return RouteDecision(default, "default", reason="нет доступных профилей")

    ranked = _heuristic_rank(task.strip(), profiles)
    if ranked:
        top = ranked[0]
        second_hits = ranked[1].hits if len(ranked) > 1 else 0
        confident = top.hits >= min_hits and (
            len(ranked) == 1 or top.hits > second_hits)
        if confident:
            return RouteDecision(
                top.name, "heuristic", top.precision,
                f"явный лидер: {top.hits} совпадений ключевых слов "
                f"против {second_hits} у следующего")

    if llm is not None:
        picked = _llm_pick(task, profiles, llm)
        if picked:
            return RouteDecision(picked, "llm", reason="выбрано моделью")

    if ranked:
        top = ranked[0]
        return RouteDecision(
            top.name, "default",
            f"эвристика неуверена ({top.hits} совпадений, без явного "
            "отрыва), LLM не помогла — взят лучший эвристический вариант")
    return RouteDecision(
        default, "default",
        "ни ключевые слова, ни LLM не дали результата — профиль по умолчанию")


def route_and_apply(cfg, task: str, use_llm: bool = True) -> RouteDecision:
    """Удобная обёртка для CLI/HTTP: подобрать профиль под task и сразу
    применить его к cfg (in-place), как обычный apply_profile.

    LLM-фолбэк использует ОСНОВНУЮ диалоговую модель из cfg — она уже
    оплачивается и настроена, поднимать для роутинга отдельный драйвер
    было бы лишней сложностью и лишним ключом ради одного короткого
    вызова. Если use_llm=False (например, роутинг заведомо дешёвого
    прогона без сети) — работает только эвристика.

    Явно заданный cfg.profile НЕ перебивается: роутинг имеет смысл,
    только когда профиль не указан пользователем явно.
    """
    from .config import Config as _Config     # локальный импорт: разрыв
    from .llm import build_llm                 # цикла config<->router<->llm

    if cfg.profile:
        return RouteDecision(cfg.profile, "explicit",
                             reason="профиль указан явно, роутинг пропущен")

    profiles = _Config.profile_infos()
    llm = None
    if use_llm:
        try:
            llm = build_llm(cfg.provider, cfg.model, base_url=cfg.base_url,
                            api_key=cfg.api_key, temperature=cfg.temperature)
        except LLMError:
            llm = None
    decision = pick_profile(task, profiles, llm=llm)
    cfg.apply_profile(decision.profile)
    return decision


