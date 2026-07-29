"""CLI: каталог, запуск на своих фото, демо, сервер, самопроверка.

    vlm list
    vlm run pim-cards photo1.jpg photo2.jpg --param marketplace=ozon
    vlm demo retail-audit
    vlm serve --port 8081
    vlm doctor
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import settings
from .core import REGISTRY, ServiceError, get_service, load_registry
from .images import ImageError


def _cfg(args):
    cfg = settings()
    if getattr(args, "provider", None):
        cfg = replace(cfg, provider=args.provider)
    if getattr(args, "model", None):
        cfg = replace(cfg, model=args.model)
    return cfg


def _params(pairs: list[str] | None, params_json: str | None) -> dict[str, Any]:
    """Параметры из --param k=v и/или --params-json файла."""
    out: dict[str, Any] = {}
    if params_json:
        p = Path(params_json)
        raw = p.read_text(encoding="utf-8") if p.is_file() else params_json
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ServiceError("--params-json должен содержать объект JSON")
        out.update(loaded)
    for pair in pairs or []:
        if "=" not in pair:
            raise ServiceError(f"параметр «{pair}» без знака = (ожидается ключ=значение)")
        k, v = pair.split("=", 1)
        out[k.strip()] = _coerce(v.strip())
    return out


def _coerce(v: str) -> Any:
    low = v.lower()
    if low in ("true", "да"):
        return True
    if low in ("false", "нет"):
        return False
    if v.startswith(("{", "[")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def cmd_list(args) -> int:
    load_registry()
    for slug, cls in sorted(REGISTRY.items()):
        print(f"{slug:22} {cls.title}")
        print(f"{'':22} {cls.summary}")
        print(f"{'':22} фото: {cls.min_images}–{cls.max_images}, "
              f"теги: {', '.join(cls.tags)}\n")
    return 0


def _store(args):
    from .store import Store

    cfg = _cfg(args)
    return Store(getattr(args, "db", None) or cfg.db_path, cache_ttl_s=cfg.cache_ttl_s)


def cmd_run(args) -> int:
    from .runner import BudgetExceeded, Runner

    cfg = _cfg(args)
    params = _params(args.param, args.params_json)
    images: Any = args.images or None
    if images is None:
        print("(без файлов — использую демо-данные)", file=sys.stderr)

    store = None if getattr(args, "no_store", False) else _store(args)
    runner = Runner(store, cfg, max_retries=cfg.max_retries,
                    daily_budget_usd=cfg.daily_budget_usd,
                    use_cache=cfg.use_cache and not getattr(args, "no_cache", False))
    try:
        result = runner.run(args.slug, images, params,
                            no_cache=getattr(args, "no_cache", False))
    except BudgetExceeded as e:
        print(f"бюджет: {e}", file=sys.stderr)
        return 3
    finally:
        if store is not None:
            store.close()

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(result.report)
        for w in result.warnings:
            print(f"⚠ {w}", file=sys.stderr)
        cost = (f", ${result.cost_usd:.4f}" if result.cost_usd else "")
        cached = " (из кеша)" if result.cached else ""
        print(f"— {result.duration_s} с, модель {result.model}, "
              f"изображений {len(result.images)}{cost}{cached}", file=sys.stderr)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{args.slug}.report.md").write_text(result.report, encoding="utf-8")
        (out / f"{args.slug}.json").write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"записано в {out}/", file=sys.stderr)
    return 0


def cmd_demo(args) -> int:
    args.images, args.param, args.params_json = [], None, None
    args.no_cache = True  # демо всегда считаем заново, иначе не видно работы
    return cmd_run(args)


def cmd_demo_all(args) -> int:
    load_registry()
    cfg = _cfg(args)
    failed = 0
    for slug in sorted(REGISTRY):
        try:
            svc = get_service(slug, cfg)
            d = svc.demo()
            r = svc.run(d.get("images"), **d.get("params", {}))
            print(f"[ок] {slug:22} полей {len(r.data)}, предупреждений "
                  f"{len(r.warnings)}, {r.duration_s} с")
            if args.out:
                out = Path(args.out)
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{slug}.report.md").write_text(r.report, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[упал] {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def cmd_stats(args) -> int:
    store = _store(args)
    try:
        s = store.stats()
    finally:
        store.close()
    print(f"прогонов всего:    {s['total_runs']}")
    print(f"успешных:          {s['success_rate'] if s['success_rate'] is not None else '—'}")
    print(f"изображений:       {s['images_processed']}")
    print(f"среднее время:     {s['avg_duration_s']} с")
    print(f"потрачено:         ${s['total_cost_usd']}")
    print(f"из кеша:           {s['served_from_cache']} ({s['cache_hit_rate']:.0%})")
    print(f"сэкономлено кешем: ${s['cache']['cost_saved_usd']}")
    if s["by_service"]:
        print("\nпо сервисам:")
        for row in s["by_service"]:
            print(f"  {row['service']:22} {row['n']:>5} прогонов, "
                  f"${round(row['cost'], 4):>8}, {row['avg_s']:.2f} с")
    return 0


def cmd_runs(args) -> int:
    from datetime import datetime

    store = _store(args)
    try:
        rows = store.runs(service=args.service, status=args.status, limit=args.limit)
    finally:
        store.close()
    if not rows:
        print("прогонов не найдено")
        return 0
    print(f"{'ID':18}{'СЕРВИС':22}{'СТАТУС':9}{'ФОТО':>5}{'ВРЕМЯ':>8}"
          f"{'СТОИМОСТЬ':>11}{'КЕШ':>5}  КОГДА")
    for r in rows:
        when = datetime.fromtimestamp(r["created_at"]).strftime("%d.%m %H:%M:%S")
        cost = f"${r['cost_usd']:.4f}" if r["cost_usd"] else "—"
        print(f"{r['id']:18}{r['service']:22}{r['status']:9}{r['images_n']:>5}"
              f"{r['duration_s'] or 0:>7.2f}с{cost:>11}{'да' if r['cached'] else '—':>5}"
              f"  {when}")
        if r["error"]:
            print(f"{'':18}└ {r['error'][:100]}")
    return 0


def cmd_purge(args) -> int:
    store = _store(args)
    try:
        runs = store.purge_runs(args.older_than_days)
        cached = store.cache_purge(args.older_than_days * 86400)
    finally:
        store.close()
    print(f"удалено прогонов: {runs}, записей кеша: {cached}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    logging.basicConfig(level=args.log.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    from .api import API_TOKEN, app

    host = args.host or ("127.0.0.1" if not API_TOKEN else "0.0.0.0")
    if not API_TOKEN and host not in ("127.0.0.1", "localhost"):
        print(f"отказ: без VLM_API_TOKEN сервис нельзя публиковать на {host}",
              file=sys.stderr)
        return 2
    print(f"→ интерфейс: http://{host}:{args.port}/", file=sys.stderr)
    print(f"→ API-доки:  http://{host}:{args.port}/docs", file=sys.stderr)
    uvicorn.run(app, host=host, port=args.port, log_level=args.log.lower())
    return 0


def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    ok = True
    warns = 0

    def check(name: str, good: bool, detail: str = "", *, warn_only: bool = False) -> None:
        nonlocal ok, warns
        if not good and warn_only:
            warns += 1
            mark = "!"
        else:
            ok = ok and good
            mark = "✓" if good else "✗"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    print(f"vlm-services · провайдер {cfg.provider} · модель {cfg.resolved_model()}\n")
    try:
        reg = load_registry()
        check("сервисы загружены", len(reg) == 12, f"{len(reg)} из 12")
    except Exception as exc:  # noqa: BLE001
        check("сервисы загружены", False, str(exc))
        reg = {}

    for slug in sorted(reg):
        try:
            svc = get_service(slug, cfg)
            d = svc.demo()
            svc.run(d.get("images"), **d.get("params", {}))
            check(f"сервис {slug}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"сервис {slug}", False, f"{type(exc).__name__}: {exc}")

    from .images import HAVE_PILLOW

    check("Pillow установлен", HAVE_PILLOW,
          "без него картинки не уменьшаются — запросы к VLM дороже", warn_only=True)

    try:
        from .vlm import get_vlm

        check("VLM инициализирован", True, type(get_vlm(cfg)).__name__)
    except Exception as exc:  # noqa: BLE001
        check("VLM инициализирован", False, f"{type(exc).__name__}: {exc}")

    if cfg.provider in ("openai", "anthropic") and not cfg.api_key:
        check("ключ API задан", False, "VLM_API_KEY пуст")
    else:
        check("ключ API задан", True)

    try:
        store = _store(args)
        store.stats()
        store.close()
        check("хранилище доступно", True, cfg.db_path)
    except Exception as exc:  # noqa: BLE001
        check("хранилище доступно", False, str(exc))

    check("кеш включён", cfg.use_cache,
          "без кеша повторные запросы оплачиваются заново", warn_only=True)
    check("дневной лимит трат задан", bool(cfg.daily_budget_usd),
          "VLM_DAILY_BUDGET_USD=0 — расход ничем не ограничен", warn_only=True)
    check("API-токен задан", bool(os.getenv("VLM_API_TOKEN", "")),
          "без него serve поднимется только на localhost", warn_only=True)

    if not ok:
        print("\nесть проблемы, см. отметки ✗")
    elif warns:
        print(f"\nготово к работе; замечаний: {warns}")
    else:
        print("\nготово к работе")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("vlm", description="VLM-сервисы: 12 продуктов")
    ap.add_argument("--provider", help="fake | openai | anthropic | ollama")
    ap.add_argument("--model")
    ap.add_argument("--db", help="файл журнала и кеша")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="каталог сервисов").set_defaults(fn=cmd_list)

    r = sub.add_parser("run", help="запустить сервис на своих изображениях")
    r.add_argument("slug")
    r.add_argument("images", nargs="*", help="пути к файлам")
    r.add_argument("--param", "-p", action="append", help="ключ=значение")
    r.add_argument("--params-json", help="файл или строка JSON с параметрами")
    r.add_argument("--out", help="каталог для отчёта")
    r.add_argument("--json", action="store_true")
    r.add_argument("--no-cache", action="store_true", help="не брать ответ из кеша")
    r.add_argument("--no-store", action="store_true", help="не писать в журнал")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("demo", help="запустить на демо-данных")
    d.add_argument("slug")
    d.add_argument("--out")
    d.add_argument("--json", action="store_true")
    d.add_argument("--no-store", action="store_true")
    d.set_defaults(fn=cmd_demo)

    da = sub.add_parser("demo-all", help="прогнать все сервисы на демо-данных")
    da.add_argument("--out")
    da.set_defaults(fn=cmd_demo_all)

    s = sub.add_parser("serve", help="веб-интерфейс и API")
    s.add_argument("--host")
    s.add_argument("--port", type=int, default=8081)
    s.add_argument("--log", default="info")
    s.set_defaults(fn=cmd_serve)

    st = sub.add_parser("stats", help="сводка: прогоны, траты, кеш")
    st.set_defaults(fn=cmd_stats)

    rn = sub.add_parser("runs", help="журнал прогонов")
    rn.add_argument("--service")
    rn.add_argument("--status", choices=["ok", "error"])
    rn.add_argument("--limit", type=int, default=30)
    rn.set_defaults(fn=cmd_runs)

    pg = sub.add_parser("purge", help="удалить старые прогоны и кеш")
    pg.add_argument("--older-than-days", type=float, default=30.0)
    pg.set_defaults(fn=cmd_purge)

    doc = sub.add_parser("doctor", help="самопроверка окружения")
    doc.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ServiceError, ImageError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
