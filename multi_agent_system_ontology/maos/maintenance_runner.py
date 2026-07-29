"""CLI: запуск фонового обслуживания (ТЗ п.6) как отдельного процесса.

Использование:
  python3 -m maos.maintenance_runner            # цикл раз в
                                                 # maintenance_interval_seconds
  python3 -m maos.maintenance_runner --once      # один цикл и выход

Отделено от HTTP-сервера намеренно: обслуживание — фоновая "Deep
Thinking" фаза, которая может жить своим процессом (и своим расписанием
рестартов/логов), не деля память с обработкой HTTP-запросов.
"""
from __future__ import annotations

import argparse
import signal
import threading

from .config import Config
from .llm.embeddings import build_embedder
from .maintenance.service import MaintenanceService
from .memory.store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="maos-maintenance",
                                 description="Фоновое обслуживание памяти MAOS")
    ap.add_argument("-c", "--config")
    ap.add_argument("--once", action="store_true",
                    help="один цикл и выход, вместо бесконечного цикла")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    store = Store(cfg.require_dsn(), dim=cfg.embedding_dim)
    provider, model, base_url, api_key, timeout = cfg.resolve_embedding()
    embedder = build_embedder(provider, model, dim=cfg.embedding_dim,
                              base_url=base_url, api_key=api_key, timeout=timeout)

    def on_event(kind: str, data: dict) -> None:
        print(f"[maintenance] {kind}: {data}")

    svc = MaintenanceService(cfg, store, embedder, on_event=on_event)

    if args.once:
        report = svc.run_once()
        print(f"дистиллировано: {report.distilled}, дубли удалены: "
             f"{report.deduped}, сущностей слито: {report.merged_entities}")
        if report.errors:
            print(f"ошибки: {report.errors}")
        store.close()
        return 1 if report.errors else 0

    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        print("\nостановка обслуживания…")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(f"MAOS maintenance: цикл каждые {cfg.maintenance_interval_seconds}с "
         "(Ctrl+C для остановки)")
    svc.run_forever(stop_event)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
