"""CLI: разовая задача, интерактивный режим, самопроверка."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .autorun import AutoRunner
from .build import build_agent, known_skills
from .mcp import MCPPool
from .router import route_and_apply
from .store import Store
from .config import Config
from .llm import available as providers

# --- вывод -----------------------------------------------------------------
C_DIM, C_TOOL, C_OK, C_ERR, C_OFF = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[0m"
C_ACC = "\033[35m"


def _plain(s: str) -> str:
    return s


def make_printer(verbose: bool, color: bool):
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)

    def on_event(kind: str, data: dict[str, Any]) -> None:
        if kind == "thought" and verbose and data.get("text"):
            print(tint(C_DIM, f"  … {data['text'].strip()[:400]}"))
        elif kind == "tool_start":
            args = json.dumps(data.get("args", {}), ensure_ascii=False)
            if len(args) > 160:
                args = args[:160] + "…"
            print(tint(C_TOOL, f"  → {data['name']} {args}"))
        elif kind == "tool_end" and verbose:
            res = (data.get("result") or "").strip()
            head = res.splitlines()[:6]
            for ln in head:
                print(tint(C_DIM, f"    {ln[:200]}"))
            if len(res.splitlines()) > 6:
                print(tint(C_DIM, f"    … ещё {len(res.splitlines()) - 6} строк"))
        elif kind == "error":
            print(tint(C_ERR, f"  ! {data.get('message')}"))
        elif kind == "limit":
            print(tint(C_ERR, f"  ! исчерпан лимит шагов ({data.get('steps')})"))

    return on_event


def ask_confirm(command: str, reason: str) -> bool:
    print(f"\n{C_ERR}ОПАСНАЯ КОМАНДА{C_OFF} ({reason}):\n  {command}")
    try:
        return input("Выполнить? [y/N] ").strip().lower() in ("y", "yes", "д", "да")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# --- команды ---------------------------------------------------------------
def cmd_run(cfg: Config, task: str, verbose: bool, color: bool) -> int:
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    agent = build_agent(cfg, confirm=ask_confirm,
                        on_event=make_printer(verbose, color))
    print(f"{C_DIM}модель: {cfg.provider}/{cfg.model} · песочница: "
          f"{cfg.sandbox.mode} · роль: {cfg.profile or '—'} · "
          f"навыки: {', '.join(cfg.skills)}{C_OFF}\n")
    agent.llm.on_retry = lambda n, why, delay: print(
        tint(C_ERR, f"  ! сбой связи ({why[:70]}), повтор {n} через {delay:.0f} с"))
    res = agent.run(task)
    print("\n" + "─" * 60)
    print(res.answer)
    print("─" * 60)
    mark = C_OK if res.stopped_by == "done" else C_ERR
    print(f"{mark}шагов: {len(res.steps)} · вызовов инструментов: "
          f"{res.tool_calls} · итог: {res.stopped_by}{C_OFF}")
    print(tint(C_DIM, agent.llm.spend_report()))
    return 0 if res.stopped_by == "done" else 1


def cmd_chat(cfg: Config, verbose: bool, color: bool) -> int:
    agent = build_agent(cfg, confirm=ask_confirm,
                        on_event=make_printer(verbose, color))
    print(f"{C_DIM}Интерактивный режим. 'exit' — выход, 'reset' — забыть "
          f"историю.{C_OFF}")
    history: list[dict[str, Any]] = []
    while True:
        try:
            task = input(f"\n{C_OK}вы>{C_OFF} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nпока")
            return 0
        if not task:
            continue
        if task in ("exit", "quit", "выход"):
            return 0
        if task in ("reset", "сброс"):
            history.clear()
            print(f"{C_DIM}история очищена{C_OFF}")
            continue
        # переносим прошлые ответы как контекст
        prefix = ""
        if history:
            prev = "\n".join(f"- {h}" for h in history[-5:])
            prefix = f"Ранее в этой сессии сделано:\n{prev}\n\nНовая задача: "
        res = agent.run(prefix + task)
        print(f"\n{res.answer}")
        history.append(f"{task} -> {res.answer[:200]}")


def cmd_auto(cfg: Config, goal: str, hours: float, iters: int,
             resume: int | None, verbose: bool, color: bool) -> int:
    """Автономный прогон: часы работы без человека."""
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    store = Store(cfg.db)
    rid = {"v": 0}
    # Пул MCP поднимается ОДИН раз: иначе на каждой итерации заново
    # запускались бы подпроцессы серверов.
    pool = MCPPool(cfg.mcp) if ("mcp" in cfg.skills and cfg.mcp) else None
    if pool:
        print(tint(C_DIM, "MCP:\n  " + pool.status().replace("\n", "\n  ")))
    printer = make_printer(verbose, color)

    def on_event(kind, data):
        if kind == "start":
            print(tint(C_OK, f"\n▶ прогон #{data['run_id']}: {data['goal']}"))
        elif kind == "resume":
            print(tint(C_OK, f"\n▶ продолжаем прогон #{data['run_id']}"))
        elif kind == "plan":
            print(tint(C_OK, "\nПлан:"))
            for i, t in enumerate(data["items"], 1):
                print(f"  {i}. {t}")
        elif kind == "iteration":
            print(tint(C_ACC, f"\n── итерация {data['n']} · {data['left']//60} мин "
                              f"· #{data['task_id']} {data['task']}"))
        elif kind == "spend":
            c = data.get("cost", 0)
            print(tint(C_DIM, f"  расход: {data['tokens']:,} токенов"
                              + (f", ${c:.4f}" if c else "")))
        elif kind == "replan":
            print(tint(C_ERR, f"\n⟲ план пересмотрен: {data['reason']}"))
            for i, t in enumerate(data["items"], 1):
                print(f"  {i}. {t}")
        elif kind == "reflect":
            for f in data.get("learned", []):
                print(tint(C_DIM, f"  запомнил: {f[:150]}"))
            if data.get("next"):
                print(tint(C_DIM, f"  дальше: {data['next'][:150]}"))
        elif kind == "finish":
            print(tint(C_OK, "\n" + "─" * 60))
            print(data["summary"])
            print(tint(C_OK, "─" * 60))
        else:
            printer(kind, data)

    def factory():
        a = build_agent(cfg, confirm=ask_confirm, store=store,
                        run_id_getter=lambda: rid["v"], mcp_pool=pool)
        a.llm.on_retry = lambda n, why, delay: print(
            tint(C_ERR, f"  ! сбой связи ({why[:60]}), повтор {n} через {delay:.0f} с"))
        return a

    runner = AutoRunner(factory, store, max_hours=hours,
                        max_iterations=iters, on_event=on_event)

    # run_id появляется внутри runner — прокидываем его инструментам памяти
    _orig_start = store.start_run

    def _start(goal_: str, profile_=None) -> int:
        rid["v"] = _orig_start(goal_, profile_)
        return rid["v"]

    store.start_run = _start  # type: ignore[assignment]
    if resume:
        rid["v"] = resume

    print(tint(C_DIM, f"модель: {cfg.provider}/{cfg.model} · роль: "
                      f"{cfg.profile or '—'} · бюджет: {hours} ч / {iters} итераций"))
    print(tint(C_DIM, f"база: {cfg.db}"))
    try:
        if resume:
            rid["v"] = resume
        res = runner.run(goal, cfg.profile, resume=resume)
    except KeyboardInterrupt:
        if pool:
            pool.close()
        print(tint(C_ERR, "\nпрервано пользователем"))
        if rid["v"]:
            store.finish_run(rid["v"], "stopped")
            print(f"Продолжить: python3 -m agent --auto --resume {rid['v']}")
        return 130
    if pool:
        pool.close()
    print(f"\nПродолжить прогон: python3 -m agent --auto --resume {res.run_id}")
    return 0 if res.stopped_by in ("done", "time") else 1


def cmd_pipeline(cfg: Config, pipeline_name: str, goal: str,
                 verbose: bool, color: bool) -> int:
    """Оркестрация нескольких ролей: конвейер стадий из agent/pipelines/."""
    tint = (lambda c, s: f"{c}{s}{C_OFF}") if color else (lambda c, s: s)
    from .pipeline import PipelineRunner, PipelineError

    store = Store(cfg.db)
    printer = make_printer(verbose, color)

    def on_event(kind, data):
        if kind == "pipeline_start":
            print(tint(C_OK, f"\n▶ конвейер #{data['pipeline_run_id']} "
                            f"{data['name']}: {' → '.join(data['stages'])}"))
        elif kind == "stage_start":
            print(tint(C_ACC, f"\n── стадия {data['stage']!r} "
                              f"(профиль {data.get('profile') or '—'})"))
        elif kind == "stage_done":
            print(tint(C_OK, f"  ✓ стадия {data['stage']!r} завершена"))
        elif kind == "stage_failed":
            print(tint(C_ERR, f"  ✗ стадия {data['stage']!r} провалена: "
                              f"{data.get('error')}"))
        elif kind == "stage_skipped":
            print(tint(C_DIM, f"  … стадия {data['stage']!r} пропущена"))
        elif kind == "pipeline_finish":
            print(tint(C_OK if data["status"] == "done" else C_ERR,
                      f"\nКонвейер завершён: {data['status']}"))
        elif kind in ("tool_start", "tool_end", "thought", "error", "limit"):
            printer(kind, {k: v for k, v in data.items() if k != "stage"})

    runner = PipelineRunner(cfg, store, on_event=on_event)
    print(tint(C_DIM, f"модель: {cfg.provider}/{cfg.model} · конвейер: "
                      f"{pipeline_name}"))
    try:
        result = runner.run(pipeline_name, goal)
    except PipelineError as exc:
        print(f"{C_ERR}Ошибка конвейера: {exc}{C_OFF}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(tint(C_ERR, "\nпрервано пользователем"))
        return 130
    finally:
        store.close()
    return 0 if result["status"] == "done" else 1


def cmd_check(cfg: Config) -> int:
    """Самопроверка: конфиг, песочница, доступность модели."""
    from .tools.shell import effective_mode

    ok = True
    print("Проверка окружения\n" + "─" * 40)
    print(f"провайдер     : {cfg.provider}")
    print(f"модель        : {cfg.model}")
    print(f"base_url      : {cfg.base_url}")
    print(f"ключ          : {'задан' if cfg.api_key else 'НЕ ЗАДАН'}")
    if cfg.provider not in ("ollama",) and not cfg.api_key:
        print(f"  {C_ERR}! для этого провайдера обычно нужен ключ в "
              f"переменной окружения{C_OFF}")
    print(f"рабочая папка : {cfg.workspace}")
    eff, note = effective_mode(cfg.sandbox)
    print(f"песочница     : {cfg.sandbox.mode}"
          + (f" -> фактически {eff}" if eff != cfg.sandbox.mode else ""))
    if note:
        print(f"  {C_ERR}{note}{C_OFF}")
    elif eff == "docker":
        print(f"  {C_OK}docker доступен{C_OFF}")
    print(f"роль          : {cfg.profile or '— (без профиля)'}")
    print(f"навыки        : {', '.join(cfg.skills)}")
    if cfg.mcp:
        print("MCP-серверы   :")
        p = MCPPool(cfg.mcp)
        for line in p.status().splitlines():
            bad = "НЕДОСТУПЕН" in line
            print(f"  {C_ERR if bad else C_OK}{line}{C_OFF}")
        p.close()

    try:
        agent = build_agent(cfg)
        print(f"инструментов  : {len(agent.tools)} "
              f"({', '.join(agent.tools.names())})")
    except Exception as exc:
        print(f"  {C_ERR}! сборка агента не удалась: {exc}{C_OFF}")
        return 1

    print("─" * 40)
    print(f"{C_OK}готово к работе{C_OFF}" if ok else
          f"{C_ERR}есть замечания выше{C_OFF}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent", description="Универсальный агент с инструментами")
    ap.add_argument("task", nargs="*", help="задача; без неё — интерактив")
    ap.add_argument("-c", "--config", help="файл конфигурации JSON")
    ap.add_argument("-p", "--provider", choices=providers(), help="провайдер")
    ap.add_argument("-m", "--model", help="имя модели")
    ap.add_argument("-w", "--workspace", help="рабочая папка")
    ap.add_argument("-P", "--profile", help="роль: " +
                    ", ".join(Config.list_profiles()))
    ap.add_argument("--auto-route", action="store_true",
                    help="самому подобрать профиль под задачу (эвристика "
                         "+ LLM-фолбэк), не задавайте вместе с -P")
    ap.add_argument("-s", "--skills", help="навыки через запятую: "
                                           f"{', '.join(known_skills())}")
    ap.add_argument("--sandbox", dest="sandbox_mode",
                    choices=["auto", "docker", "confirm", "off"], help="режим запуска команд")
    ap.add_argument("--max-steps", type=int, dest="max_steps")
    ap.add_argument("--base-url", dest="base_url")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="показывать мысли и вывод инструментов")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--check", action="store_true", help="самопроверка и выход")
    ap.add_argument("--auto", action="store_true",
                    help="автономный режим: часы работы без человека")
    ap.add_argument("--hours", type=float, help="бюджет времени, часов")
    ap.add_argument("--iterations", type=int, help="предел итераций")
    ap.add_argument("--resume", type=int, help="продолжить прогон по номеру")
    ap.add_argument("--db", help="файл базы состояния")
    ap.add_argument("--pipeline", metavar="ИМЯ",
                    help="оркестрация: конвейер стадий из agent/pipelines/"
                         "<ИМЯ>.json, задача — общая цель конвейера ({goal})")
    args = ap.parse_args(argv)

    overrides: dict[str, Any] = {
        k: getattr(args, k) for k in
        ("provider", "model", "workspace", "max_steps", "base_url",
         "sandbox_mode", "profile")
    }
    if args.db:
        overrides["db"] = args.db
    if args.skills:
        overrides["skills"] = [s.strip() for s in args.skills.split(",") if s.strip()]

    try:
        cfg = Config.load(args.config, **overrides)
    except Exception as exc:
        print(f"{C_ERR}Ошибка конфигурации: {exc}{C_OFF}", file=sys.stderr)
        return 2

    color = not args.no_color and sys.stdout.isatty()
    if args.check:
        return cmd_check(cfg)

    if args.auto_route:
        if args.profile:
            print(f"{C_ERR}--auto-route и -P/--profile вместе не имеют "
                  f"смысла — уберите один из них{C_OFF}", file=sys.stderr)
            return 2
        goal_for_routing = " ".join(args.task)
        if not goal_for_routing:
            print(f"{C_ERR}--auto-route нужна задача текстом — иначе "
                  f"нечего анализировать{C_OFF}", file=sys.stderr)
            return 2
        decision = route_and_apply(cfg, goal_for_routing)
        print(f"{C_DIM}роль подобрана автоматически: {decision.profile} "
              f"({decision.method}; {decision.reason}){C_OFF}")

    if args.pipeline:
        if args.auto or args.resume or args.auto_route:
            print(f"{C_ERR}--pipeline нельзя сочетать с --auto/--resume/"
                  f"--auto-route{C_OFF}", file=sys.stderr)
            return 2
        goal = " ".join(args.task)
        if not goal:
            print(f"{C_ERR}Для --pipeline нужна цель текстом{C_OFF}",
                  file=sys.stderr)
            return 2
        return cmd_pipeline(cfg, args.pipeline, goal, args.verbose, color)

    if args.auto or args.resume:
        goal = " ".join(args.task)
        if not goal and not args.resume:
            print(f"{C_ERR}Для --auto нужна цель{C_OFF}", file=sys.stderr)
            return 2
        return cmd_auto(cfg, goal,
                        args.hours or cfg.max_hours,
                        args.iterations or cfg.max_iterations,
                        args.resume, args.verbose, color)
    try:
        if args.task:
            return cmd_run(cfg, " ".join(args.task), args.verbose, color)
        return cmd_chat(cfg, args.verbose, color)
    except KeyboardInterrupt:
        print("\nпрервано")
        return 130
    except Exception as exc:

        print(f"{C_ERR}Сбой: {type(exc).__name__}: {exc}{C_OFF}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
