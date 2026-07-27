"""Дымовой тест: годится ли ЖИВАЯ модель для работы с инструментами.

Зачем отдельно от `--check`. Тот проверяет конфиг, песочницу и сборку
агента, но ни разу не обращается к модели. «Готово к работе» там
означает «настройки на месте», а не «модель умеет звать инструменты».
Разница выясняется на первой же реальной задаче, обычно ночью.

Здесь наоборот: настройки не проверяются вовсе, проверяется модель.
Задачи идут по нарастающей, каждая с ЧИСЛЕННО проверяемым результатом —
не «ответ выглядит разумно», а «в файле лежит 42».

Что ловим (всё это наблюдалось на живых моделях):

  НЕ ЗОВЁТ ИНСТРУМЕНТЫ. Модель описывает словами, что надо сделать,
      вместо вызова. Самый частый провал у слабых моделей.
  ТЕКСТ В reasoning_content. Qwen3.5 кладёт ответ в поле рассуждений,
      content пуст. Агент принимал НАМЕРЕНИЕ за результат.
  ЛОМАЕТСЯ НА ЦЕПОЧКЕ. Один вызов делает, три подряд — теряет нить.
  ПОРТИТ ПУТИ. `[main.py](http://main.py)` вместо `main.py`.
  НЕ ЧИТАЕТ РЕЗУЛЬТАТ. Инструмент вернул 42, модель отвечает «готово».
  ЗАЦИКЛИВАЕТСЯ. Один и тот же вызов до упора в лимит шагов.

Вердикт в конце — не «пройдено N из M», а решение: годится, годится с
оговорками или не тянет. С указанием, что именно сломалось.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .build import build_agent
from .config import Config
from .core import Result

#: Шагов на задачу. Больше — не проверка, а надежда на удачу.
MAX_STEPS = 8


@dataclass
class Case:
    """Одна проверка: что просим и как убеждаемся, что сделано."""
    name: str
    task: str
    #: check(результат, рабочая папка) -> (успех, что увидели)
    check: Callable[[Result, Path], tuple[bool, str]]
    #: без чего задача бессмысленна — пропускаем, а не проваливаем
    needs: tuple[str, ...] = ()
    weight: str = "важно"          # важно | желательно
    setup: Callable[[Path], None] | None = None


@dataclass
class Outcome:
    case: Case
    ok: bool
    detail: str
    seconds: float
    steps: int
    calls: int
    tokens: int
    stopped_by: str
    events: list[str] = field(default_factory=list)


# ─────────────────────────── проверки ───────────────────────────────
def _read(ws: Path, name: str) -> str:
    p = ws / name
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _has_number(text: str, value: str) -> bool:
    """Число в тексте, не как часть другого числа."""
    return re.search(rf"(?<![\d.]){re.escape(value)}(?![\d.])", text) is not None


def build_cases() -> list[Case]:
    def answers_42(res: Result, ws: Path) -> tuple[bool, str]:
        got = _has_number(res.answer, "42")
        return got, f"в ответе {'есть' if got else 'НЕТ'} числа 42"

    def file_written(res: Result, ws: Path) -> tuple[bool, str]:
        txt = _read(ws, "hello.txt")
        return ("привет" in txt.lower(),
                f"hello.txt: {txt[:40]!r}" if txt else "hello.txt не создан")

    def counted_lines(res: Result, ws: Path) -> tuple[bool, str]:
        # В файле ровно 7 строк — модель обязана назвать именно 7.
        return (_has_number(res.answer, "7"),
                f"ответ: {res.answer[:70]!r}")

    def fixed_code(res: Result, ws: Path) -> tuple[bool, str]:
        src = _read(ws, "calc.py")
        return ("*" in src and "w + h" not in src,
                f"calc.py: {src.strip()[:60]!r}")

    def chain_done(res: Result, ws: Path) -> tuple[bool, str]:
        out = _read(ws, "out.txt")
        return ("54" in out, f"out.txt: {out[:40]!r}" if out
                else "out.txt не создан")

    def md_path(res: Result, ws: Path) -> tuple[bool, str]:
        txt = _read(ws, "note.md")
        return ("готово" in txt.lower(), f"note.md: {txt[:40]!r}"
                if txt else "note.md не создан")

    def honest_about_missing(res: Result, ws: Path) -> tuple[bool, str]:
        low = res.answer.lower()
        # Признание отсутствия важнее формулировки: ищем смысл, не слово.
        honest = any(w in low for w in ("нет", "не найд", "отсутств",
                                        "не сущест", "no such", "not found"))
        invented = bool(re.search(r"\b(содержит|внутри|написано)\b", low))
        return (honest and not invented,
                f"ответ: {res.answer[:80]!r}")

    def remembered(res: Result, ws: Path) -> tuple[bool, str]:
        return (_has_number(res.answer, "83.875"),
                f"ответ: {res.answer[:70]!r}")

    # ---- подготовка файлов
    def mk_lines(ws: Path) -> None:
        (ws / "data.txt").write_text(
            "\n".join(f"строка {i}" for i in range(1, 8)) + "\n",
            encoding="utf-8")

    def mk_broken(ws: Path) -> None:
        (ws / "calc.py").write_text(
            "def area(w, h):\n    return w + h\n", encoding="utf-8")

    return [
        Case("отвечает вообще",
             "Сколько будет 6 умножить на 7? Ответь одним числом.",
             answers_42),
        Case("зовёт инструмент",
             "Создай файл hello.txt со словом «привет» внутри.",
             file_written, needs=("files",)),
        Case("читает файл и считает",
             "Сколько строк в файле data.txt? Ответь числом.",
             counted_lines, needs=("files",), setup=mk_lines),
        Case("читает результат инструмента",
             "Запусти python-код: print(6*7). Скажи, что он напечатал.",
             answers_42, needs=("python",)),
        Case("цепочка из трёх действий",
             "Сделай по порядку: 1) вычисли 6*9 через run_python; "
             "2) запиши полученное число в файл out.txt; "
             "3) прочитай out.txt и подтверди содержимое.",
             chain_done, needs=("files", "python")),
        Case("правит существующий код",
             "В файле calc.py функция area складывает вместо умножения. "
             "Исправь её.",
             fixed_code, needs=("files",), setup=mk_broken),
        Case("не портит имя файла",
             "Запиши слово «готово» в файл note.md",
             md_path, needs=("files",), weight="желательно"),
        Case("честен про отсутствующее",
             "Прочитай файл несуществующий-файл-12345.txt и скажи, "
             "что в нём.",
             honest_about_missing, needs=("files",)),
        Case("пользуется памятью",
             "Запомни через remember: диаметр вала 83.875 мм. "
             "Затем найди это в памяти через recall и назови число.",
             remembered, needs=("memory",), weight="желательно"),
    ]


# ─────────────────────────── прогон ─────────────────────────────────
def run_case(cfg: Config, case: Case, verbose: bool = False) -> Outcome:
    ws = Path(tempfile.mkdtemp(prefix="smoke-"))
    events: list[str] = []
    try:
        if case.setup:
            case.setup(ws)
        c = Config(**{**cfg.__dict__})
        c.sandbox = cfg.sandbox
        c.workspace = str(ws)
        c.db = str(ws / "smoke.db")
        c.max_steps = MAX_STEPS
        c.vcs_auto = False              # снимки тут только мешают
        c.system_prompt = (
            "Ты инженерный агент. Выполняй задачу ИНСТРУМЕНТАМИ, а не "
            "описанием действий словами. Сделав дело, коротко ответь "
            "текстом, что получилось, и приведи конкретные числа."
        )

        def on_event(kind: str, data: dict[str, Any]) -> None:
            if kind in ("tool_start", "empty", "error", "limit"):
                events.append(f"{kind}:{data.get('name', '')}".rstrip(":"))
            if verbose and kind == "tool_start":
                print(f"      → {data.get('name')} "
                      f"{str(data.get('args'))[:60]}")

        agent = build_agent(c, on_event=on_event)
        t0 = time.time()
        res = agent.run(case.task)
        took = time.time() - t0
        ok, detail = case.check(res, ws)
        return Outcome(case, ok, detail, took, len(res.steps),
                       res.tool_calls, res.tokens, res.stopped_by, events)
    except Exception as exc:
        return Outcome(case, False, f"сбой: {type(exc).__name__}: {exc}",
                       0.0, 0, 0, 0, "error", events)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def verdict(results: list[Outcome]) -> tuple[str, list[str]]:
    """Решение и список претензий. Не проценты, а годность к работе."""
    problems: list[str] = []
    important = [r for r in results if r.case.weight == "важно"]
    failed_imp = [r for r in important if not r.ok]

    by_name = {r.case.name: r for r in results}

    def bad(name: str) -> bool:
        r = by_name.get(name)
        return r is not None and not r.ok

    # Разбираем ХАРАКТЕР провалов: он важнее их числа.
    if bad("отвечает вообще"):
        problems.append("модель не отвечает или ответ не разобрать — "
                        "проверьте адрес и имя модели")
    if bad("зовёт инструмент"):
        problems.append("НЕ ВЫЗЫВАЕТ ИНСТРУМЕНТЫ: описывает действия "
                        "словами. Проверьте, поддерживает ли модель "
                        "tool calling и включён ли он в LM Studio")
    if bad("цепочка из трёх действий"):
        problems.append("теряет нить на цепочке из трёх шагов — "
                        "для автономного режима слаба")
    if bad("читает результат инструмента"):
        problems.append("не читает то, что вернул инструмент: "
                        "отвечает «готово» вместо числа")
    if bad("честен про отсутствующее"):
        problems.append("ВЫДУМЫВАЕТ содержимое несуществующего файла — "
                        "самый опасный дефект, ей нельзя верить на слово")
    if bad("правит существующий код"):
        problems.append("не справляется с точечной правкой файла")
    if bad("не портит имя файла"):
        problems.append("портит пути (markdown-оформление) — "
                        "мелочь, ядро это чинит само")

    empties = sum(r.events.count("empty") for r in results)
    if empties >= 3:
        problems.append(f"пустые ходы: {empties} раз модель возвращала "
                        "ответ без действия (типично для reasoning-моделей)")
    loops = [r for r in results if r.stopped_by == "max_steps"]
    if loops:
        problems.append(f"упирается в лимит шагов: {len(loops)} задач "
                        "из-за повторов одного действия")

    if not failed_imp and not problems:
        return "ГОДИТСЯ", []
    if bad("отвечает вообще") or bad("зовёт инструмент"):
        return "НЕ ТЯНЕТ", problems
    if len(failed_imp) >= 3:
        return "НЕ ТЯНЕТ", problems
    if failed_imp or problems:
        return "ГОДИТСЯ С ОГОВОРКАМИ", problems
    return "ГОДИТСЯ", problems


def smoke(cfg: Config, verbose: bool = False,
          only: str = "") -> tuple[str, list[Outcome]]:
    cases = build_cases()
    if only:
        cases = [c for c in cases if only.lower() in c.name.lower()]
    have = set(cfg.skills)
    results: list[Outcome] = []

    print("=" * 62)
    print("ДЫМОВОЙ ТЕСТ ЖИВОЙ МОДЕЛИ")
    print("=" * 62)
    print(f"модель   : {cfg.provider}/{cfg.model}")
    print(f"адрес    : {cfg.base_url or '—'}")
    print(f"навыки   : {', '.join(cfg.skills)}")
    print(f"шагов на задачу: {MAX_STEPS}")
    print("-" * 62)

    for i, case in enumerate(cases, 1):
        missing = [n for n in case.needs if n not in have]
        if missing:
            print(f"{i}. {case.name:28} ПРОПУЩЕНО "
                  f"(нужен навык: {', '.join(missing)})")
            continue
        print(f"{i}. {case.name:28} ", end="", flush=True)
        out = run_case(cfg, case, verbose)
        results.append(out)
        mark = "ок  " if out.ok else "ПЛОХО"
        print(f"{mark} {out.seconds:5.1f} с · шагов {out.steps} · "
              f"вызовов {out.calls} · {out.tokens:,} ток.")
        if not out.ok:
            print(f"      {out.detail}")
            if out.stopped_by != "done":
                print(f"      остановлено: {out.stopped_by}")

    print("-" * 62)
    if not results:
        print("Ни одна задача не выполнялась.")
        return "НЕТ ДАННЫХ", results

    ok = sum(1 for r in results if r.ok)
    total_tok = sum(r.tokens for r in results)
    total_sec = sum(r.seconds for r in results)
    speed = (total_tok / total_sec) if total_sec > 0 else 0
    print(f"выполнено : {ok} из {len(results)}")
    # Скорость показываем, только если прогон длился заметное время:
    # на секундном прогоне это число ничего не значит и вводит в
    # заблуждение при сравнении моделей.
    print(f"время     : {total_sec:.1f} с, токенов {total_tok:,}"
          + (f", ~{speed:.0f} ток./с" if total_sec >= 5 else ""))

    mark, problems = verdict(results)
    print()
    print(f"ВЕРДИКТ: {mark}")
    for p in problems:
        print(f"  · {p}")
    if mark == "ГОДИТСЯ":
        print("  Модель уверенно работает с инструментами. Можно "
              "запускать на реальных задачах.")
    elif mark == "ГОДИТСЯ С ОГОВОРКАМИ":
        print("  Годна для коротких задач под присмотром; для "
              "автономного прогона на часы — рискованно.")
    else:
        print("  Для работы с инструментами не подходит. Возьмите модель "
              "покрупнее либо явно включите tool calling.")
    print("=" * 62)
    return mark, results
