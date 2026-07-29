"""Точка входа сервера ERP AI: `python3 -m app.server`.

Поднимает FastAPI-приложение через uvicorn. Отказывается слушать
не-localhost без токена — тот же принцип защиты, что в agent_system и
multi_agent_system_ontology этого репозитория.
"""
from __future__ import annotations

import os


def serve(cfg=None, host: str | None = None, port: int | None = None,
         token: str | None = None) -> None:
    import uvicorn

    from .api.server import app, configure, get_effective_token
    from .config import Config

    cfg = cfg or Config.load()
    host = host or cfg.host
    port = port or cfg.port
    configure(cfg, token)
    effective_token = get_effective_token()
    if host not in ("127.0.0.1", "localhost") and not effective_token:
        raise SystemExit(
            "Отказ: сервер открыт наружу без токена. Задайте ERP_API_TOKEN "
            "или слушайте 127.0.0.1."
        )
    print(f"ERP AI: http://{host}:{port}/  "
         f"(токен: {'да' if effective_token else 'нет, только localhost'})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import Config

    ap = argparse.ArgumentParser(prog="erp-ai-server", description="HTTP API ERP AI")
    ap.add_argument("-c", "--config")
    ap.add_argument("--host", default=os.getenv("ERP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("ERP_PORT", "8100")))
    ap.add_argument("--token", default=None)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    serve(cfg, args.host, args.port, args.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
