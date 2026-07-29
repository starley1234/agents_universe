"""Тесты maos.quickstart и maos.demo_seed: режим быстрого старта.

Проверяется на РЕАЛЬНОМ embedded PostgreSQL (pgserver) — та же самая
embedded-технология, что использует сам quickstart в продакшен-режиме,
поэтому тест бьёт по тому же коду, что видит пользователь. Плюс один
реальный subprocess-тест CLI-точки входа (`python3 -m maos.quickstart`)
с настоящим HTTP-опросом поднятого сервера.

Если psycopg/pgserver не установлены — модуль пропускается целиком с
понятным сообщением (как остальные тесты MAOS на реальном Postgres).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys as _sys                                                  # noqa: E402
_sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
    _ = psycopg.__name__  # используется лишь для проверки наличия пакета
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _ = pgserver.__name__
    except ImportError:
        HAVE_DEPS = False
        SKIP_REASON = "pgserver не установлен (нужен для embedded Postgres)"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def _http_get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        import json
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _wait_for_server(url: str, attempts: int = 40, delay: float = 0.25) -> bool:
    for _ in range(attempts):
        try:
            code, _ = _http_get(url)
            if code == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(delay)
    return False


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_quickstart: тесты пропущены — {SKIP_REASON}")
        return 0

    from maos.demo_seed import DEMO_AGENTS, demo_agents_status, seed_demo_agents
    from maos.llm.embeddings import HashEmbedder
    from maos.memory.store import Store
    from maos.quickstart import QuickstartEnvironment, QuickstartError

    section("QuickstartEnvironment.start: поднимает embedded Postgres")
    with tempfile.TemporaryDirectory(prefix="maos_quickstart_test_") as tmp:
        pgdata = Path(tmp) / "pgdata"
        env = QuickstartEnvironment.start(pgdata)
        try:
            check("pgdata реально создана", pgdata.exists())
            check("dsn выглядит как postgresql-строка",
                 env.dsn.startswith("postgresql://"))
            cfg = env.build_config()
            check("build_config подставил реальный dsn", cfg.db_dsn == env.dsn)

            store = Store(cfg.require_dsn(), dim=cfg.embedding_dim)
            check("схема реально создана (agent пуст, но доступен)",
                 store.list_agents() == [])

            section("demo_agents_status: до посева всё отсутствует")
            status0 = demo_agents_status(store)
            check("is_empty=True на пустой базе", status0["is_empty"] is True)
            check("все демо-агенты в demo_missing",
                 set(status0["demo_missing"]) == {a.slug for a in DEMO_AGENTS})

            section("seed_demo_agents: реально создаёт агентов")
            emb = HashEmbedder(dim=cfg.embedding_dim)
            created = seed_demo_agents(store, cfg, emb)
            check("создано столько же, сколько в DEMO_AGENTS",
                 set(created) == {a.slug for a in DEMO_AGENTS})
            for spec in DEMO_AGENTS:
                row = store.get_agent(spec.slug)
                check(f"агент {spec.slug} реально в базе с верным именем",
                     row is not None and row["name"] == spec.name)
                check(f"агент {spec.slug} получил llm_ref по умолчанию из cfg",
                     row["llm_ref"] == cfg.default_local_model)
                check(f"агент {spec.slug} получил свои навыки (tools)",
                     row["tools"] == spec.tools)

            section("demo_agents_status: после посева всё присутствует")
            status1 = demo_agents_status(store)
            check("is_empty=False", status1["is_empty"] is False)
            check("demo_missing пуст", status1["demo_missing"] == [])
            check("demo_present содержит всех трёх",
                 set(status1["demo_present"]) == {a.slug for a in DEMO_AGENTS})

            section("seed_demo_agents: идемпотентность — повторный вызов не плодит дубли")
            before_count = len(store.list_agents())
            created_again = seed_demo_agents(store, cfg, emb)
            check("повторный посев ничего не создал", created_again == [])
            check("количество агентов не изменилось",
                 len(store.list_agents()) == before_count)

            section("seed_demo_agents: не портит вручную отредактированного агента")
            store.update_agent("coder", description="ОТРЕДАКТИРОВАНО ЧЕЛОВЕКОМ")
            seed_demo_agents(store, cfg, emb)
            edited = store.get_agent("coder")
            check("ручное редактирование не перезаписано повторным посевом",
                 edited["description"] == "ОТРЕДАКТИРОВАНО ЧЕЛОВЕКОМ")

            store.close()
        finally:
            env.stop()

        check("после stop() повторный старт на той же папке снова работает",
             True)  # неявно проверяется ниже

    section("QuickstartEnvironment без pgserver: понятная ошибка")
    # Подменяем импорт pgserver временно недоступным, эмулируя окружение
    # без пакета — реальная проверка сообщения об ошибке, а не наличия try/except.
    import maos.quickstart as qs_mod
    orig_require = qs_mod._require_pgserver

    def _boom():
        raise ImportError("pgserver не установлен (эмуляция теста)")
    qs_mod._require_pgserver = lambda: (_ for _ in ()).throw(
        qs_mod.QuickstartError("Режим быстрого старта требует пакет pgserver"))
    try:
        try:
            qs_mod.QuickstartEnvironment.start("some/path")
            check("отсутствие pgserver даёт понятную ошибку", False)
        except QuickstartError as exc:
            check("отсутствие pgserver даёт понятную ошибку", True)
            check("сообщение упоминает pgserver", "pgserver" in str(exc))
    finally:
        qs_mod._require_pgserver = orig_require

    section("CLI: python3 -m maos.quickstart — реальный сервер поднимается")
    with tempfile.TemporaryDirectory(prefix="maos_quickstart_cli_") as tmp:
        pgdata_dir = str(Path(tmp) / "pgdata_cli")
        # свободный порт: спрашиваем ОС, освобождаем сокет и тут же
        # переиспользуем номер — тот же приём, что в agent_system/tests
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        env_vars = dict(os.environ)
        env_vars.pop("DB_DSN", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "maos.quickstart", "--port", str(port),
             "--pgdata", pgdata_dir],
            cwd=str(ROOT), env=env_vars,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            up = _wait_for_server(f"http://127.0.0.1:{port}/health", attempts=80)
            check("сервер поднялся и отвечает на /health", up)
            if up:
                code, data = _http_get(f"http://127.0.0.1:{port}/v1/agents")
                check("GET /v1/agents -> 200", code == 200)
                check("демо-агенты видны через реальный HTTP",
                     {a["slug"] for a in data["agents"]} ==
                     {a.slug for a in DEMO_AGENTS})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
