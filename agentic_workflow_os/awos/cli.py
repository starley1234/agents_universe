"""CLI среды: запуск, наблюдение, ответ на точки контроля.

ПОЧЕМУ КОМАНД ИМЕННО СТОЛЬКО. Среда не должна требовать веб-интерфейса,
чтобы ею пользоваться: конвейеры запускают из cron, из CI, из чужого
скрипта. Поэтому весь жизненный цикл прогона доступен из терминала:

    awos run <workflow> --goal ... --input k=v   запустить
    awos status [run_id]                          где остановились
    awos inbox                                    что ждёт человека
    awos approve|reject|edit <checkpoint_id>      ответить и продолжить
    awos resume <run_id>                          продолжить после ответа
    awos context <run_id> [key]                   доска: срез или история
    awos workflows | profiles | tools             что вообще доступно
    awos check                                    самопроверка окружения
    awos serve                                    HTTP API + дашборд

Отдельная команда `check` появилась потому, что первый вопрос при любой
проблеме — «а среда вообще настроена?». Пусть ответ будет одной командой,
а не чтением конфига глазами.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Config, ConfigError
from .kernel.engine import Engine, EngineError, RunOutcome
from .kernel.store import Store
from .kernel.workflow import (WorkflowError, describe_workflows, load_workflow)
from .roles.profile import describe_profiles
from .tools.registry import build_registry, granted_summary


def _parse_inputs(pairs: list[str]) -> dict[str, Any]:
    """--input key=value. Значение пробуем разобрать как JSON, иначе строка."""
    out: dict[str, Any] = {}
    for raw in pairs or []:
        if "=" not in raw:
            raise SystemExit(f"--input ожидает key=value, получено {raw!r}")
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith("@"):
            path = Path(value[1:]).expanduser()
            if not path.exists():
                raise SystemExit(f"--input {key}: файла {path} нет")
            out[key] = path.read_text(encoding="utf-8")
            continue
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def _print_outcome(outcome: RunOutcome, *, verbose: bool = False) -> None:
    icon = {"done": "✓", "waiting_human": "⏸", "failed": "✗",
            "cancelled": "⊘", "running": "…"}.get(outcome.status, "?")
    print(f"\n{icon} Прогон #{outcome.run_id}: {outcome.status}")
    if outcome.detail:
        print(f"  {outcome.detail}")

    for step in outcome.steps:
        mark = {"done": "✓", "failed": "✗", "waiting_human": "⏸",
                "pending": "·", "running": "…", "skipped": "–"}.get(
                    step["status"], "?")
        score = f" score={step['score']:.2f}" if step.get("score") is not None else ""
        rev = f" доработок={step['revisions']}" if step.get("revisions") else ""
        print(f"  {mark} {step['name']}{score}{rev}")
        if step.get("detail") and step["status"] in ("failed", "waiting_human"):
            print(f"      {step['detail']}")

    if outcome.checkpoint:
        cp = outcome.checkpoint
        print(f"\n⏸ Нужен человек — точка контроля #{cp['id']} ({cp['kind']})")
        print(f"  {cp['question']}")
        payload = cp.get("payload") or {}
        if payload.get("output"):
            preview = str(payload["output"])
            print("  --- результат на утверждение ---")
            print("  " + (preview[:1200].replace("\n", "\n  ")))
            if len(preview) > 1200:
                print(f"  [...ещё {len(preview) - 1200} символов]")
        if payload.get("tool"):
            print(f"  инструмент: {payload['tool']} args={payload.get('args')}")
        print(f"\n  awos approve {cp['id']}            — утвердить")
        print(f"  awos edit {cp['id']} 'текст'        — утвердить с правкой")
        print(f"  awos reject {cp['id']} 'причина'    — отклонить")

    if outcome.status == "done" and outcome.outputs:
        print("\nРезультаты на доске:")
        for key, value in outcome.outputs.items():
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False)
            if verbose:
                print(f"\n### {key}\n{text}")
            else:
                head = text[:300].replace("\n", " ")
                print(f"  {key}: {head}{'…' if len(text) > 300 else ''}")


# --- команды ---------------------------------------------------------------
def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    inputs = _parse_inputs(args.input)
    engine = Engine(cfg)
    try:
        outcome = engine.start(args.workflow, goal=args.goal, inputs=inputs)
    except WorkflowError as exc:
        print(f"Ошибка workflow: {exc}", file=sys.stderr)
        return 2
    _print_outcome(outcome, verbose=args.verbose)
    return 0 if outcome.status in ("done", "waiting_human") else 1


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    if args.run_id is None:
        runs = store.list_runs(limit=args.limit)
        if not runs:
            print("Прогонов пока нет. Запустите: awos run <workflow> --goal ...")
            return 0
        print(f"{'#':>5}  {'статус':<14} {'workflow':<22} цель")
        for r in runs:
            goal = (r["goal"] or "")[:48]
            print(f"{r['id']:>5}  {r['status']:<14} {r['workflow']:<22} {goal}")
        return 0

    try:
        info = Engine(cfg, store).status(args.run_id)
    except Exception as exc:                                  # noqa: BLE001
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    run = info["run"]
    print(f"Прогон #{run['id']}: {run['workflow']} — {run['status']}")
    if run["detail"]:
        print(f"  {run['detail']}")
    print(f"  цель: {run['goal'] or '—'}")
    print(f"  шагов выполнено: {run['steps_done']}, вызовов инструментов: "
          f"{run['tool_calls']}, обращений к моделям: {run['llm_calls']}, "
          f"токенов: {run['tokens_in']}/{run['tokens_out']}")
    print("\nШаги:")
    for s in info["steps"]:
        score = f" score={s['score']:.2f}" if s["score"] is not None else ""
        revisions = f" доработок={s['revisions']}" if s["revisions"] else ""
        print(f"  [{s['status']:<13}] {s['name']}{score}{revisions}")
        if s["detail"]:
            print(f"      {s['detail']}")
    ctx = {k: v for k, v in info["context"].items() if not k.startswith("_")}
    if ctx:
        print("\nДоска контекста:")
        for key, value in ctx.items():
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False)
            print(f"  {key} ({len(text)} симв.)")
    if info["checkpoint"]:
        cp = info["checkpoint"]
        print(f"\n⏸ Ждёт человека: точка контроля #{cp['id']} — {cp['question']}")
    if args.events:
        print("\nЖурнал:")
        for e in info["events"]:
            role = f"[{e['role']}] " if e["role"] else ""
            print(f"  {e['kind']:<20} {role}{(e['message'] or '')[:120]}")
    return 0


def cmd_inbox(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    pending = store.list_checkpoints(status="pending")
    if not pending:
        print("Точек контроля нет — среде не нужен человек прямо сейчас.")
        return 0
    print(f"Ждут решения: {len(pending)}\n")
    for cp in pending:
        print(f"#{cp['id']} (прогон #{cp['run_id']}, {cp['kind']}): {cp['question']}")
        payload = cp.get("payload") or {}
        if payload.get("step"):
            print(f"   шаг: {payload['step']}")
        if payload.get("output"):
            head = str(payload["output"])[:200].replace("\n", " ")
            print(f"   результат: {head}…")
        if payload.get("tool"):
            print(f"   инструмент: {payload['tool']} args={payload.get('args')}")
        print()
    print("Ответить: awos approve <id> | awos edit <id> 'текст' | "
          "awos reject <id> 'причина'")
    return 0


def _respond(cfg: Config, checkpoint_id: int, status: str, text: str,
             verbose: bool) -> int:
    engine = Engine(cfg)
    try:
        outcome = engine.respond(checkpoint_id, status, text, actor="cli")
    except (EngineError, WorkflowError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    _print_outcome(outcome, verbose=verbose)
    return 0 if outcome.status in ("done", "waiting_human") else 1


def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    return _respond(cfg, args.checkpoint_id, "approved", args.comment or "",
                    args.verbose)


def cmd_reject(cfg: Config, args: argparse.Namespace) -> int:
    return _respond(cfg, args.checkpoint_id, "rejected", args.reason or "",
                    args.verbose)


def cmd_edit(cfg: Config, args: argparse.Namespace) -> int:
    text = args.text
    if text.startswith("@"):
        path = Path(text[1:]).expanduser()
        if not path.exists():
            print(f"Файла {path} нет", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8")
    return _respond(cfg, args.checkpoint_id, "edited", text, args.verbose)


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    engine = Engine(cfg)
    try:
        outcome = engine.resume(args.run_id)
    except (EngineError, WorkflowError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    _print_outcome(outcome, verbose=args.verbose)
    return 0 if outcome.status in ("done", "waiting_human") else 1


def cmd_cancel(cfg: Config, args: argparse.Namespace) -> int:
    engine = Engine(cfg)
    engine.cancel(args.run_id, args.reason or "отменён оператором")
    print(f"Прогон #{args.run_id} отменён")
    return 0


def cmd_context(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    if args.key:
        history = store.ctx_history(args.run_id, args.key)
        if not history:
            print(f"Ключа {args.key!r} на доске прогона #{args.run_id} нет")
            return 1
        for row in history:
            value = row["value"]
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, indent=2)
            print(f"--- версия {row['version']} (автор: {row['author'] or '—'}) ---")
            print(text)
            print()
        return 0
    snapshot = store.ctx_all(args.run_id)
    if not snapshot:
        print(f"Доска прогона #{args.run_id} пуста")
        return 0
    for key, value in sorted(snapshot.items()):
        if key.startswith("_") and not args.all:
            continue
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False)
        head = text[:200].replace("\n", " ")
        print(f"{key} ({len(text)} симв.): {head}{'…' if len(text) > 200 else ''}")
    return 0


def cmd_workflows(cfg: Config, args: argparse.Namespace) -> int:
    items = describe_workflows(cfg.resolved_workflows_dir())
    if not items:
        print(f"В {cfg.resolved_workflows_dir()} нет ни одного определения")
        return 0
    for item in items:
        if "error" in item:
            print(f"✗ {item['name']}: {item['error']}")
            continue
        print(f"{item['name']} — {item['title'] or '(без заголовка)'}")
        if item["description"]:
            print(f"   {item['description']}")
        print(f"   шаги: {' → '.join(item['steps'])}")
        if item["inputs"]:
            print("   входы: " + ", ".join(
                f"{k} ({v})" for k, v in item["inputs"].items()))
        print()
    return 0


def cmd_profiles(cfg: Config, args: argparse.Namespace) -> int:
    items = describe_profiles(cfg.resolved_profiles_dir())
    if not items:
        print("Профилей нет — среда будет работать на встроенных ролях")
        return 0
    for item in items:
        if "error" in item:
            print(f"✗ {item['name']}: {item['error']}")
            continue
        tools = ", ".join(item["tools"]) if item["tools"] else "все разрешённые"
        print(f"{item['name']:<16} [{item['role']:<10}] {item['title']}")
        print(f"   модель: {item['model']}; инструменты: {tools}")
    return 0


def cmd_tools(cfg: Config, args: argparse.Namespace) -> int:
    reg = build_registry(cfg)
    print("Инструменты, выданные средой (гранты из конфига):\n")
    print(reg.prompt() or "  — ни одного")
    print("\nГранты:")
    for key, value in granted_summary(cfg).items():
        print(f"  {key}: {value if value else '—'}")
    return 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    print(cfg.describe())
    ok = True

    try:
        store = Store(cfg.db_path)
        stats = store.stats()
        print(f"\n✓ Хранилище: {cfg.db_path} — прогонов {stats['runs']}, "
              f"ждут человека {stats['runs_waiting_human']}, "
              f"открытых точек контроля {stats['checkpoints_pending']}")
    except Exception as exc:                                  # noqa: BLE001
        ok = False
        print(f"\n✗ Хранилище недоступно: {exc}")

    wfs = describe_workflows(cfg.resolved_workflows_dir())
    broken = [w for w in wfs if "error" in w]
    print(f"{'✓' if not broken else '✗'} Workflow: {len(wfs)} шт."
          + (f", битых {len(broken)}" if broken else ""))
    for w in broken:
        ok = False
        print(f"    ✗ {w['name']}: {w['error']}")

    profiles = describe_profiles(cfg.resolved_profiles_dir())
    broken_p = [p for p in profiles if "error" in p]
    print(f"{'✓' if not broken_p else '✗'} Профили: {len(profiles)} шт."
          + (f", битых {len(broken_p)}" if broken_p else ""))
    for p in broken_p:
        ok = False
        print(f"    ✗ {p['name']}: {p['error']}")

    reg = build_registry(cfg)
    print(f"✓ Инструменты: {', '.join(reg.names())}")

    if cfg.provider not in ("stub",) and not cfg.api_key and \
            "localhost" not in cfg.base_url and "127.0.0.1" not in cfg.base_url:
        print("⚠ Ключ модели не задан (AWOS_API_KEY/OPENAI_API_KEY), а base_url "
              "указывает на внешний сервис — вызовы будут отклонены.\n"
              "  Для проверки без модели: AWOS_PROVIDER=stub")

    print("\n" + ("Среда готова к работе." if ok else "Есть проблемы — см. выше."))
    return 0 if ok else 1


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .api.server import serve
    return serve(cfg)


def cmd_demo(cfg: Config, args: argparse.Namespace) -> int:
    """Показать среду в работе без единого обращения к внешней модели.

    Демо специально использует stub-провайдер: человек, который только
    что склонировал проект, должен увидеть работающий цикл до того, как
    заведёт ключи. Это же используется в CI как дымовой тест.
    """
    cfg.provider = "stub"
    cfg.model = "stub"
    cfg.hitl_mode = "off"
    engine = Engine(cfg)
    wf = load_workflow(args.workflow, cfg.resolved_workflows_dir())
    inputs = {k: f"(демо) {v}" for k, v in wf.inputs.items()}
    print(f"Демо-прогон {wf.name} на stub-модели (без сети и ключей)…")
    outcome = engine.start(wf, goal=args.goal or "демонстрация среды",
                           inputs=inputs)
    _print_outcome(outcome, verbose=args.verbose)
    return 0 if outcome.status == "done" else 1


# --- разбор аргументов -------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="awos",
        description="AWOS — Agentic Workflow OS: среда для автономных AI-команд",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Полный цикл: awos run … → awos inbox → awos approve <id>")
    p.add_argument("-c", "--config", help="путь к JSON-конфигу среды")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="печатать результаты целиком")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="запустить workflow")
    run.add_argument("workflow", help="имя определения или путь к .json")
    run.add_argument("--goal", default="", help="цель прогона")
    run.add_argument("--input", action="append", default=[],
                     metavar="KEY=VALUE",
                     help="вход workflow; @файл — прочитать значение из файла")
    run.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="состояние прогонов")
    st.add_argument("run_id", nargs="?", type=int)
    st.add_argument("--limit", type=int, default=20)
    st.add_argument("--events", action="store_true", help="показать журнал")
    st.set_defaults(func=cmd_status)

    ib = sub.add_parser("inbox", help="что ждёт решения человека")
    ib.set_defaults(func=cmd_inbox)

    ap = sub.add_parser("approve", help="утвердить точку контроля")
    ap.add_argument("checkpoint_id", type=int)
    ap.add_argument("comment", nargs="?", default="")
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="отклонить точку контроля")
    rj.add_argument("checkpoint_id", type=int)
    rj.add_argument("reason", nargs="?", default="")
    rj.set_defaults(func=cmd_reject)

    ed = sub.add_parser("edit", help="утвердить с правкой (текст или @файл)")
    ed.add_argument("checkpoint_id", type=int)
    ed.add_argument("text")
    ed.set_defaults(func=cmd_edit)

    rs = sub.add_parser("resume", help="продолжить прогон")
    rs.add_argument("run_id", type=int)
    rs.set_defaults(func=cmd_resume)

    cn = sub.add_parser("cancel", help="отменить прогон")
    cn.add_argument("run_id", type=int)
    cn.add_argument("reason", nargs="?", default="")
    cn.set_defaults(func=cmd_cancel)

    cx = sub.add_parser("context", help="доска контекста прогона")
    cx.add_argument("run_id", type=int)
    cx.add_argument("key", nargs="?", default="")
    cx.add_argument("--all", action="store_true", help="включая служебные ключи")
    cx.set_defaults(func=cmd_context)

    sub.add_parser("workflows", help="доступные workflow").set_defaults(
        func=cmd_workflows)
    sub.add_parser("profiles", help="доступные профили агентов").set_defaults(
        func=cmd_profiles)
    sub.add_parser("tools", help="инструменты и гранты среды").set_defaults(
        func=cmd_tools)
    sub.add_parser("check", help="самопроверка окружения").set_defaults(
        func=cmd_check)
    sub.add_parser("serve", help="HTTP API + дашборд").set_defaults(
        func=cmd_serve)

    dm = sub.add_parser("demo", help="демо-прогон на stub-модели, без сети")
    dm.add_argument("workflow", nargs="?", default="research_brief")
    dm.add_argument("--goal", default="")
    dm.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    try:
        return int(args.func(cfg, args))
    except KeyboardInterrupt:
        print("\nПрервано. Состояние прогона сохранено — продолжите "
              "командой awos resume <run_id>.", file=sys.stderr)
        return 130
    except (WorkflowError, EngineError, ConfigError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
