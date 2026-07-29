"""Режим быстрого старта: одна команда, без ручной установки PostgreSQL.

`python3 -m maos.quickstart` — поднимает embedded PostgreSQL+pgvector
(через опциональный пакет `pgserver`), создаёт схему, сеет демо-агентов
(см. maos/demo_seed.py) и запускает HTTP API/дашборд — всё в одном
процессе, без Docker и без предварительной настройки внешней СУБД.

ЭТО РЕЖИМ ДЛЯ ЗНАКОМСТВА И ЛОКАЛЬНОЙ РАЗРАБОТКИ, а не для продакшена:
embedded Postgres живёт в подпапке рабочей директории и не переживает
специально написанный для него процесс так же надёжно, как отдельно
администрируемый сервер (без репликации, без отдельного бэкапа). Для
продакшена по-прежнему используйте настоящий DB_DSN — quickstart лишь
экономит время первого запуска, ничего не меняя в архитектуре MAOS
("PostgreSQL+pgvector обязателен" остаётся в силе, просто сервер поднят
внутри того же процесса).

pgserver — опциональная зависимость (как pymupdf/psycopg[binary] в
agent_system): импортируется лениво, отсутствие пакета даёт понятную
ошибку с командой для установки, а не трейсбек.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import Config
from .demo_seed import seed_demo_agents


class QuickstartError(RuntimeError):
    """Ошибка режима быстрого старта: нет pgserver, не удалось поднять БД."""


#: подпапка рабочей директории для embedded-кластера. Отдельная от
#: обычных pgdata, чтобы не спутать с продакшен-данными случайно.
DEFAULT_PGDATA_DIR = ".maos_quickstart_pgdata"


def _require_pgserver():
    try:
        import pgserver  # type: ignore
    except ImportError as exc:
        raise QuickstartError(
            "Режим быстрого старта требует пакет pgserver (embedded "
            "PostgreSQL). Установите: pip install pgserver\n"
            "Либо используйте обычный запуск с настоящим DB_DSN: "
            "см. .env.example и README.md."
        ) from exc
    return pgserver


class QuickstartEnvironment:
    """Держит embedded Postgres живым, пока существует объект.

    Использование:
        env = QuickstartEnvironment.start(pgdata_dir)
        cfg = env.build_config()
        ...
        env.stop()
    """

    def __init__(self, server: Any, dsn: str, pgdata: Path) -> None:
        self._server = server
        self.dsn = dsn
        self.pgdata = pgdata

    @classmethod
    def start(cls, pgdata_dir: str | Path = DEFAULT_PGDATA_DIR,
             embedding_dim: int = 256) -> "QuickstartEnvironment":
        pgserver = _require_pgserver()
        pgdata = Path(pgdata_dir).expanduser().resolve()
        pgdata.mkdir(parents=True, exist_ok=True)
        try:
            server = pgserver.get_server(pgdata)
        except Exception as exc:  # платформа без embedded Postgres и т.п.
            raise QuickstartError(
                f"Не удалось поднять embedded PostgreSQL в {pgdata}: {exc}"
            ) from exc
        dsn = server.get_uri()
        return cls(server, dsn, pgdata)

    def build_config(self, **overrides: Any) -> Config:
        cfg = Config.load(db_dsn=self.dsn, **overrides)
        return cfg

    def stop(self) -> None:
        self._server.cleanup()


def run_quickstart(host: str = "127.0.0.1", port: int = 8090,
                   pgdata_dir: str | Path = DEFAULT_PGDATA_DIR,
                   seed: bool = True, open_message: bool = True) -> int:
    """Полный цикл: поднять БД -> посеять демо-агентов -> запустить сервер.

    Блокирует поток (serve_forever внутри), как обычный `make serve` —
    возвращает управление только после Ctrl+C/остановки сервера.
    """
    from .api.server import serve
    from .llm.embeddings import build_embedder
    from .memory.store import Store

    env = QuickstartEnvironment.start(pgdata_dir)
    try:
        cfg = env.build_config(host=host, port=port)
        store = Store(cfg.require_dsn(), dim=cfg.embedding_dim)
        try:
            if seed:
                provider, model, base_url, api_key, timeout = cfg.resolve_embedding()
                try:
                    embedder = build_embedder(
                        provider, model, dim=cfg.embedding_dim,
                        base_url=base_url, api_key=api_key, timeout=timeout)
                except Exception:
                    embedder = None
                created = seed_demo_agents(store, cfg, embedder)
                if open_message:
                    if created:
                        print(f"MAOS quickstart: созданы демо-агенты: "
                             f"{', '.join(created)}")
                    else:
                        print("MAOS quickstart: демо-агенты уже существуют, "
                             "посев пропущен")
        finally:
            store.close()

        if open_message:
            print(f"MAOS quickstart: embedded PostgreSQL в {env.pgdata}")
            print(f"MAOS quickstart: {env.dsn}")
            print(f"MAOS quickstart: откройте http://{host}:{port}/dashboard")

        os.environ["DB_DSN"] = env.dsn
        serve(cfg, host, port, token=cfg.api_token or None)
        return 0
    finally:
        env.stop()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="maos-quickstart",
        description="MAOS одной командой: embedded Postgres + демо-агенты + сервер")
    ap.add_argument("--host", default=os.getenv("MAOS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("MAOS_PORT", "8090")))
    ap.add_argument("--pgdata", default=DEFAULT_PGDATA_DIR,
                    help=f"папка embedded-кластера (по умолчанию {DEFAULT_PGDATA_DIR})")
    ap.add_argument("--no-seed", action="store_true",
                    help="не создавать демо-агентов")
    args = ap.parse_args(argv)
    try:
        return run_quickstart(args.host, args.port, args.pgdata,
                              seed=not args.no_seed)
    except QuickstartError as exc:
        print(f"Ошибка: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
