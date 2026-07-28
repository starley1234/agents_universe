"""Тесты maos.maintenance_runner: CLI для фонового обслуживания через
реальный subprocess (--once) и корректную обработку отсутствующего DB_DSN.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="maos_runner_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(_srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", _srv.get_uri())


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


def _run(env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("DB_DSN", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "maos.maintenance_runner", "--once"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)


def main() -> int:
    section("maintenance_runner --once: без DB_DSN падает с понятной ошибкой")
    res = _run({})
    check("ненулевой код возврата без DB_DSN", res.returncode != 0)
    check("сообщение об ошибке упоминает DB_DSN",
         "DB_DSN" in res.stdout or "DB_DSN" in res.stderr)

    if not HAVE_DEPS:
        print(f"\ntest_maintenance_runner: часть с реальной БД пропущена — {SKIP_REASON}")
        print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
        return 1 if FAIL else 0

    section("maintenance_runner --once: реальный прогон на пустой базе")
    dsn = _fresh_dsn()
    res2 = _run({"DB_DSN": dsn})
    check("код возврата 0 на пустой, но валидной базе", res2.returncode == 0)
    check("отчёт напечатан", "дистиллировано" in res2.stdout)
    check("нет необработанных ошибок в выводе", "Traceback" not in res2.stderr)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
