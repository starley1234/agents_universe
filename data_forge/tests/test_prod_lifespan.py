"""Тесты прод-совместимости запуска: `uvicorn dataforge.api.server:app`
напрямую (без явного вызова `configure()` из `dataforge.server.serve()`)
— типичный способ запуска ASGI-приложения в проде (gunicorn с
uvicorn-воркерами, `uvicorn module:app --workers N`, Docker CMD).

Реальный subprocess uvicorn (не TestClient/моки) — единственный способ
честно проверить `lifespan`, т.к. FastAPI TestClient тоже вызывает
lifespan, но в этом же процессе/импорте модуля, где `_STATE` мог
остаться сконфигурированным предыдущим тестом. Отдельный процесс
гарантирует чистое состояние модуля, как при реальном запуске контейнера.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="forge_prod_lifespan_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"

try:
    import httpx  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = SKIP_REASON or "httpx не установлен"

try:
    subprocess.run(["uvicorn", "--version"], capture_output=True, timeout=5, check=False)
    _HAVE_UVICORN_CLI = True
except (FileNotFoundError, OSError):
    _HAVE_UVICORN_CLI = False
    HAVE_DEPS = HAVE_DEPS and False
    SKIP_REASON = SKIP_REASON or "команда uvicorn не найдена в PATH"


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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _project_root() -> str:
    return str(Path(__file__).resolve().parents[1])


def _run_uvicorn(env_overrides: dict[str, str], port: int,
                 timeout_wait: float = 8.0) -> tuple[subprocess.Popen, str]:
    env = os.environ.copy()
    env.update(env_overrides)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = _project_root()
    for var in ("FORGE_LLM_BASE_URL", "FORGE_API_TOKEN"):
        env.pop(var, None)
    proc = subprocess.Popen(
        ["uvicorn", "dataforge.api.server:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=_project_root(), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    return proc, ""


def _wait_health(port: int, attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_prod_lifespan: тесты пропущены — {SKIP_REASON}")
        return 0

    section("Прод-запуск: uvicorn module:app напрямую БЕЗ вызова configure()")
    port1 = _free_port()
    proc1, _ = _run_uvicorn({"DB_DSN": _fresh_dsn()}, port1)
    try:
        healthy = _wait_health(port1)
        check("сервер поднялся и отвечает на /health без явного configure()",
             healthy)
        if healthy:
            r = httpx.get(f"http://127.0.0.1:{port1}/v1/sources", timeout=3)
            check("реальный REST-эндпоинт отвечает 200, а не 500 "
                 "'не сконфигурирован'", r.status_code == 200)
            check("тело ответа — валидный JSON-список", r.json() == [])
    finally:
        proc1.terminate()
        try:
            out1, _ = proc1.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc1.kill()
            out1, _ = proc1.communicate()

    section("Fail-fast: без DB_DSN процесс НЕ поднимается")
    port2 = _free_port()
    env2 = os.environ.copy()
    env2.pop("DB_DSN", None)
    env2["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env2.get("PATH", "")
    env2["PYTHONPATH"] = _project_root()
    proc2 = subprocess.Popen(
        ["uvicorn", "dataforge.api.server:app", "--host", "127.0.0.1",
         "--port", str(port2)],
        cwd=_project_root(), env=env2, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    try:
        out2, _ = proc2.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc2.kill()
        out2, _ = proc2.communicate()
    check("процесс завершился с ненулевым кодом (не завис, не отдаёт "
         "трафик молча)", proc2.returncode != 0)
    check("сообщение об ошибке упоминает DB_DSN", "DB_DSN" in out2)
    check("порт не отвечает после падения процесса",
         not _wait_health(port2, attempts=3))

    section("Fail-fast: недоступная PostgreSQL — процесс НЕ поднимается")
    port3 = _free_port()
    proc3, _ = _run_uvicorn(
        {"DB_DSN": "postgresql://baduser:badpass@127.0.0.1:1/nonexistent"}, port3)
    try:
        out3, _ = proc3.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc3.kill()
        out3, _ = proc3.communicate()
    check("процесс завершился с ненулевым кодом при недостижимой БД",
         proc3.returncode != 0)
    check("сообщение об ошибке упоминает подключение к PostgreSQL",
         "PostgreSQL" in out3 or "connect" in out3.lower())

    section("Явный configure() из dataforge.server.serve() продолжает работать")
    # Регрессия: убедиться, что lifespan НЕ перезаписывает уже
    # выполненный вручную configure() (idempotent-проверка через
    # отдельный процесс, имитирующий CLI-путь python3 -m dataforge.server).
    port4 = _free_port()
    dsn4 = _fresh_dsn()
    env4 = os.environ.copy()
    env4["DB_DSN"] = dsn4
    env4["FORGE_PORT"] = str(port4)
    env4["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env4.get("PATH", "")
    env4["PYTHONPATH"] = _project_root()
    proc4 = subprocess.Popen(
        [sys.executable, "-m", "dataforge.server"],
        cwd=_project_root(), env=env4, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
    try:
        healthy4 = _wait_health(port4)
        check("python3 -m dataforge.server (существующий CLI-путь) "
             "по-прежнему работает", healthy4)
    finally:
        proc4.terminate()
        try:
            proc4.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc4.kill()
            proc4.communicate()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
