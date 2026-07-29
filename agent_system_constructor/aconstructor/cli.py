"""CLI: список пайплайнов, запуск, схема графа.

    python -m aconstructor list
    python -m aconstructor run patent-clearance
    python -m aconstructor run energy-hacker --task task.json --out out/
    python -m aconstructor graph doc-restorer
    python -m aconstructor run-all
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import settings
from .core import REGISTRY, get_pipeline, load_registry, mermaid, new_state


def _cfg(args) -> object:
    cfg = settings()
    if args.provider:
        cfg = replace(cfg, provider=args.provider)
    if args.model:
        cfg = replace(cfg, model=args.model)
    return cfg


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
    graph = p.build(cfg=_cfg(args))
    result = graph.invoke(new_state(task))
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
    for slug in sorted(REGISTRY):
        p = REGISTRY[slug]
        result = p.build(cfg=cfg).invoke(new_state(p.demo_task()))
        status = "ошибки" if result.get("errors") else "ок"
        print(f"[{status}] {slug}: узлов пройдено {len(result.get('trace', []))}, "
              f"находок {len(result.get('findings', []))}")
        if args.out:
            _dump(result, Path(args.out), slug)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("aconstructor", description="Среда агентов на LangGraph")
    ap.add_argument("--provider", help="fake | openai | anthropic | ollama")
    ap.add_argument("--model")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="список пайплайнов").set_defaults(fn=cmd_list)

    r = sub.add_parser("run", help="запустить пайплайн")
    r.add_argument("slug")
    r.add_argument("--task", help="JSON-файл с задачей (по умолчанию демо-данные)")
    r.add_argument("--out", help="каталог для отчёта и артефактов")
    r.add_argument("--json", action="store_true", help="вывести полное состояние")
    r.set_defaults(fn=cmd_run)

    g = sub.add_parser("graph", help="схема графа в Mermaid")
    g.add_argument("slug")
    g.set_defaults(fn=cmd_graph)

    a = sub.add_parser("run-all", help="прогнать все пайплайны на демо-данных")
    a.add_argument("--out")
    a.set_defaults(fn=cmd_run_all)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
