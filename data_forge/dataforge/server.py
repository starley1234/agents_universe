"""Точка входа сервера DataForge: `python3 -m dataforge.server`.

Поднимает FastAPI-приложение через uvicorn. Отказывается слушать
не-localhost без токена — тот же принцип защиты, что в остальных
проектах этого репозитория. Это ЕДИНСТВЕННЫЙ путь запуска, где эта
защита включена: прямой ASGI-запуск (`uvicorn dataforge.api.server:app`,
gunicorn с uvicorn-воркерами) обходит `serve()` и полагается только на
fail-fast конфигурации в `lifespan` (см. `dataforge/api/server.py`) —
поэтому в проде (Docker/systemd) рекомендуется именно этот entrypoint
(`python3 -m dataforge.server`), а не голый `uvicorn module:app`.

Поддержка нескольких воркеров (`--workers`/`FORGE_WORKERS`) — для
прод-нагрузки в один процесс с одним event loop может не хватать
пропускной способности. С `workers > 1` uvicorn требует ИМПОРТ-СТРОКУ
приложения (`"dataforge.api.server:app"`), а не объект — с одним
воркером передаётся объект `app` напрямую (быстрее к старту, тот же
процесс что и раньше, обратная совместимость с тестами, которые
запускают `serve()` в отдельном потоке текущего процесса — что
невозможно для `workers > 1`, т.к. uvicorn с несколькими воркерами
форкает процессы).
"""
from __future__ import annotations

import os


def serve(cfg=None, host: str | None = None, port: int | None = None,
         token: str | None = None, workers: int | None = None) -> None:
    import uvicorn

    from .api.server import app, configure, get_effective_token
    from .config import Config

    cfg = cfg or Config.load()
    host = host or cfg.host
    port = port or cfg.port
    workers = workers if workers is not None else int(os.getenv("FORGE_WORKERS", "1"))
    configure(cfg, token)
    effective_token = get_effective_token()
    if host not in ("127.0.0.1", "localhost") and not effective_token:
        raise SystemExit(
            "Отказ: сервер открыт наружу без токена. Задайте FORGE_API_TOKEN "
            "или слушайте 127.0.0.1."
        )
    print(f"DataForge: http://{host}:{port}/ (workers={workers}) "
         f"(токен: {'да' if effective_token else 'нет, только localhost'})")
    if workers > 1:
        # С несколькими воркерами uvicorn форкает процессы — configure()
        # из ЭТОГО процесса не унаследуется, каждый воркер сам выполнит
        # autoconfigure fail-fast логику в lifespan при импорте модуля
        # (читает те же переменные окружения — DB_DSN и т.п. одинаковы
        # для всех воркеров одного контейнера/пода).
        uvicorn.run("dataforge.api.server:app", host=host, port=port,
                   workers=workers, log_level="warning")
    else:
        uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import Config

    ap = argparse.ArgumentParser(prog="dataforge-server", description="HTTP API DataForge")
    ap.add_argument("-c", "--config")
    ap.add_argument("--host", default=os.getenv("FORGE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("FORGE_PORT", "8200")))
    ap.add_argument("--workers", type=int, default=int(os.getenv("FORGE_WORKERS", "1")))
    ap.add_argument("--token", default=None)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    serve(cfg, args.host, args.port, args.token, args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
