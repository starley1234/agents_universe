"""CLI: каталог, запуск, история, сервер, обслуживание.

    aconstructor list
    aconstructor run patent-clearance --out out/
    aconstructor serve --port 8080
    aconstructor history --pipeline energy-hacker
    aconstructor stats
    aconstructor doctor
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .config import settings
from .core import REGISTRY, get_pipeline, load_registry, mermaid, new_state

DB_DEFAULT = os.getenv("ACONSTRUCTOR_DB", "data/aconstructor.db")


def _cfg(args):
    cfg = settings()
    if getattr(args, "provider", None):
        cfg = replace(cfg, provider=args.provider)
    if getattr(args, "model", None):
        cfg = replace(cfg, model=args.model)
    return cfg


def _store(args):
    from .store import RunStore

    return RunStore(getattr(args, "db", None) or DB_DEFAULT)


def _dump(result: dict, out: Path, slug: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = [out / f"{slug}.report.md", out / f"{slug}.state.json"]
    written[0].write_text(result.get("report", ""), encoding="utf-8")
    written[1].write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")
    for key, value in (result.get("artifacts") or {}).items():
        if isinstance(value, str) and key.endswith(("_script", "_md")):
            ext = {"revit_script": "py", "autolisp_script": "lsp"}.get(key, "md")
            p = out / f"{slug}.{key}.{ext}"
            p.write_text(value, encoding="utf-8")
            written.append(p)
    return written


def _ts(v: float | None) -> str:
    return datetime.fromtimestamp(v).strftime("%d.%m %H:%M:%S") if v else "—"


# --- команды ---------------------------------------------------------------
def cmd_list(args) -> int:
    load_registry()
    for slug, p in sorted(REGISTRY.items()):
        print(f"{slug:20} {p.title}")
        print(f"{'':20} {p.summary}")
        print(f"{'':20} агенты: {', '.join(p.agents)}\n")
    return 0


def cmd_run(args) -> int:
    p = get_pipeline(args.slug)
    task = json.loads(Path(args.task).read_text(encoding="utf-8")) if args.task else p.demo_task()
    cfg = _cfg(args)

    if args.no_store:
        from .runner import execute

        result, usage = execute(args.slug, task, cfg)
    else:
        from .runner import Runner

        runner = Runner(_store(args), cfg, workers=1, timeout_s=args.timeout)
        run = runner.run_sync(args.slug, task, cfg.provider, cfg.resolved_model())
        if run.status == "failed":
            print(f"прогон упал: {run.error}", file=sys.stderr)
            return 1
        result = run.result or {}
        usage = {"tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
                 "cost_usd": run.cost_usd}
        print(f"прогон {run.id} · {run.duration_s:.2f} с · ${run.cost_usd:.4f}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result.get("report", "(отчёт не сформирован)"))
        if result.get("errors"):
            print("\nПредупреждения:", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e}", file=sys.stderr)
    if args.out:
        for f in _dump(result, Path(args.out), args.slug):
            print(f"записано: {f}", file=sys.stderr)
    return 0


def cmd_graph(args) -> int:
    print(mermaid(args.slug, cfg=_cfg(args)))
    return 0


def cmd_run_all(args) -> int:
    load_registry()
    cfg = _cfg(args)
    from .runner import execute

    failed = 0
    for slug in sorted(REGISTRY):
        p = REGISTRY[slug]
        try:
            result, _ = execute(slug, p.demo_task(), cfg)
            status = "ошибки" if result.get("errors") else "ок"
            print(f"[{status}] {slug}: узлов {len(result.get('trace', []))}, "
                  f"находок {len(result.get('findings', []))}")
            if args.out:
                _dump(result, Path(args.out), slug)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[упал] {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def cmd_serve(args) -> int:
    import uvicorn

    os.environ.setdefault("ACONSTRUCTOR_DB", args.db or DB_DEFAULT)
    if args.workers:
        os.environ["ACONSTRUCTOR_WORKERS"] = str(args.workers)
    logging.basicConfig(level=args.log.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    from .api import API_TOKEN, app

    host = args.host or ("127.0.0.1" if not API_TOKEN else "0.0.0.0")
    if not API_TOKEN and host not in ("127.0.0.1", "localhost"):
        print("отказ: без ACONSTRUCTOR_API_TOKEN сервис нельзя публиковать "
              f"на {host}", file=sys.stderr)
        return 2
    print(f"→ интерфейс:  http://{host}:{args.port}/", file=sys.stderr)
    print(f"→ API-доки:   http://{host}:{args.port}/docs", file=sys.stderr)
    uvicorn.run(app, host=host, port=args.port, log_level=args.log.lower())
    return 0


def cmd_history(args) -> int:
    store = _store(args)
    runs = store.list(pipeline=args.pipeline, status=args.status, limit=args.limit)
    if not runs:
        print("прогонов не найдено")
        return 0
    print(f"{'ID':18}{'ПАЙПЛАЙН':20}{'СТАТУС':11}{'НАХОДОК':>8}"
          f"{'ВРЕМЯ':>9}{'СТОИМОСТЬ':>11}  СОЗДАН")
    for r in runs:
        dur = f"{r.duration_s:.2f}с" if r.duration_s else "—"
        cost = f"${r.cost_usd:.4f}" if r.cost_usd else "—"
        print(f"{r.id:18}{r.pipeline:20}{r.status:11}{r.findings_n:>8}"
              f"{dur:>9}{cost:>11}  {_ts(r.created_at)}")
    return 0


def cmd_show(args) -> int:
    store = _store(args)
    run = store.get(args.run_id)
    if run is None:
        print(f"прогон {args.run_id} не найден", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(run.result, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"# {run.pipeline} · {run.status} · {_ts(run.created_at)}")
    if run.error:
        print(f"ошибка: {run.error}", file=sys.stderr)
        return 1
    print()
    print(run.report)
    arts = store.artifacts(args.run_id)
    if arts:
        print("\nАртефакты: " + ", ".join(f"{a['name']} ({a['size']} б)" for a in arts))
    return 0


def cmd_stats(args) -> int:
    s = _store(args).stats()
    print(f"прогонов всего:   {s['total']}")
    print(f"успешных:         {s['success_rate'] if s['success_rate'] is not None else '—'}")
    print(f"среднее время:    {s['avg_duration_s']} с")
    print(f"суммарно стоило:  ${s['total_cost_usd']}")
    print(f"находок:          {s['total_findings']}")
    if s["by_status"]:
        print("\nпо статусам: " + ", ".join(f"{k}={v}" for k, v in s["by_status"].items()))
    if s["by_pipeline"]:
        print("\nпо пайплайнам:")
        for r in s["by_pipeline"]:
            print(f"  {r['pipeline']:20} {r['n']:>5} прогонов, {r['avg_s']:.2f} с в среднем")
    return 0


def cmd_purge(args) -> int:
    n = _store(args).purge(args.older_than_days)
    print(f"удалено прогонов: {n}")
    return 0


def cmd_doctor(args) -> int:
    """Самопроверка окружения перед запуском в бой."""
    cfg = _cfg(args)
    ok = True
    warns = 0

    def check(name: str, good: bool, detail: str = "", *, warn_only: bool = False) -> None:
        """warn_only — замечание, а не отказ: локальной разработке оно не мешает."""
        nonlocal ok, warns
        if not good and warn_only:
            warns += 1
            mark = "!"
        else:
            ok = ok and good
            mark = "✓" if good else "✗"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    print(f"aconstructor · провайдер {cfg.provider} · модель {cfg.resolved_model()}\n")
    try:
        reg = load_registry()
        check("пайплайны загружены", len(reg) == 7, f"{len(reg)} шт")
    except Exception as exc:  # noqa: BLE001
        check("пайплайны загружены", False, str(exc))
        reg = {}

    for slug in sorted(reg):
        try:
            reg[slug].build(cfg=cfg)
            check(f"граф {slug}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"граф {slug}", False, f"{type(exc).__name__}: {exc}")

    try:
        from .llm import get_llm

        llm = get_llm(cfg)
        check("LLM инициализирован", True, type(llm).__name__)
        if cfg.provider != "fake" and args.probe:
            llm.invoke("ping")
            check("LLM отвечает", True)
    except Exception as exc:  # noqa: BLE001
        check("LLM инициализирован", False, f"{type(exc).__name__}: {exc}")

    if cfg.provider in ("openai", "anthropic") and not cfg.api_key:
        check("ключ API задан", False, "ACONSTRUCTOR_API_KEY пуст")
    else:
        check("ключ API задан", True)

    try:
        st = _store(args)
        st.stats()
        st.close()
        check("хранилище доступно", True, args.db or DB_DEFAULT)
    except Exception as exc:  # noqa: BLE001
        check("хранилище доступно", False, str(exc))

    token = os.getenv("ACONSTRUCTOR_API_TOKEN", "")
    check("API-токен задан", bool(token),
          "без него serve поднимется только на localhost" if not token else "",
          warn_only=True)

    if not ok:
        print("\nесть проблемы, см. отметки ✗")
    elif warns:
        print(f"\nготово к работе; замечаний: {warns}")
    else:
        print("\nготово к работе")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("aconstructor", description="Среда агентов на LangGraph")
    ap.add_argument("--provider", help="fake | openai | anthropic | ollama")
    ap.add_argument("--model")
    ap.add_argument("--db", help=f"файл базы прогонов (по умолчанию {DB_DEFAULT})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="список пайплайнов").set_defaults(fn=cmd_list)

    r = sub.add_parser("run", help="запустить пайплайн")
    r.add_argument("slug")
    r.add_argument("--task", help="JSON-файл с задачей (по умолчанию демо-данные)")
    r.add_argument("--out", help="каталог для отчёта и артефактов")
    r.add_argument("--json", action="store_true", help="вывести полное состояние")
    r.add_argument("--no-store", action="store_true", help="не писать в журнал прогонов")
    r.add_argument("--timeout", type=float, default=600.0)
    r.set_defaults(fn=cmd_run)

    g = sub.add_parser("graph", help="схема графа в Mermaid")
    g.add_argument("slug")
    g.set_defaults(fn=cmd_graph)

    a = sub.add_parser("run-all", help="прогнать все пайплайны на демо-данных")
    a.add_argument("--out")
    a.set_defaults(fn=cmd_run_all)

    s = sub.add_parser("serve", help="поднять веб-интерфейс и API")
    s.add_argument("--host")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--workers", type=int, help="воркеров очереди")
    s.add_argument("--log", default="info")
    s.set_defaults(fn=cmd_serve)

    h = sub.add_parser("history", help="журнал прогонов")
    h.add_argument("--pipeline")
    h.add_argument("--status", choices=["queued", "running", "done", "failed", "cancelled"])
    h.add_argument("--limit", type=int, default=30)
    h.set_defaults(fn=cmd_history)

    sh = sub.add_parser("show", help="показать прогон по id")
    sh.add_argument("run_id")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(fn=cmd_show)

    sub.add_parser("stats", help="сводка по прогонам").set_defaults(fn=cmd_stats)

    pg = sub.add_parser("purge", help="удалить старые прогоны")
    pg.add_argument("--older-than-days", type=float, default=30.0)
    pg.set_defaults(fn=cmd_purge)

    d = sub.add_parser("doctor", help="самопроверка окружения")
    d.add_argument("--probe", action="store_true", help="сделать пробный вызов LLM")
    d.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyError as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
