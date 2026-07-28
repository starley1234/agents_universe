"""Тесты dashboard.html: структурная целостность, синтаксис встроенного
JS (через node --check, если Node доступен — иначе проверка пропускается
только для этой ЧАСТИ, остальные структурные проверки идут всегда), и
реальный сценарий онбординга через живой HTTP-сервер + embedded Postgres.

Философия: дашборд — это код, а не «просто разметка». Ломающие правки
(отсутствующий обработчик кнопки, битый JS, эндпоинт, которого дашборд
ждёт, но сервер не отдаёт) должны ловиться тестом, а не глазами при
следующем ручном открытии страницы.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DASHBOARD_PATH = ROOT / "maos" / "web" / "dashboard.html"

PASS, FAIL = 0, 0


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
        _ = pgserver.__name__
    except ImportError:
        HAVE_DEPS = False
        SKIP_REASON = "pgserver не установлен"


def _fresh_dsn(srv) -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", srv.get_uri())


def main() -> int:
    html = DASHBOARD_PATH.read_text(encoding="utf-8")

    section("dashboard.html: все вкладки Admin/Chat/Graph присутствуют")
    for view in ("overview", "agents", "chat", "chain", "graph"):
        check(f"есть секция view-{view}", f'id="view-{view}"' in html)
        check(f"есть кнопка навигации на {view}", f'data-view="{view}"' in html)

    section("dashboard.html: ключевые элементы онбординга (быстрый старт)")
    check("баннер онбординга объявлен", 'id="onboardingBanner"' in html)
    check("вызывает /v1/onboarding/status", "/v1/onboarding/status" in html)
    check("вызывает /v1/onboarding/seed", "/v1/onboarding/seed" in html)
    check("есть кнопка создания демо-агентов в JS-шаблоне", "ob-seed" in html)

    section("dashboard.html: элементы управления агентами/чатом/цепочками/графом")
    for element_id in ("a-save", "a-slug", "a-name", "a-desc", "a-prompt",
                       "c-send", "c-input", "c-agent", "ch-start", "ch-goal",
                       "ch-agents", "graphCanvas", "statsGrid", "modelsTable"):
        check(f"элемент #{element_id} присутствует", f'id="{element_id}"' in html)

    section("dashboard.html: встроенный JS синтаксически корректен")
    node = shutil.which("node")
    if not node:
        print("  (node не найден в PATH — проверка синтаксиса JS пропущена)")
    else:
        m = re.search(r"<script>(.*)</script>", html, re.S)
        check("тег <script> найден", m is not None)
        if m:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".js",
                                             delete=False) as tf:
                tf.write(m.group(1))
                js_path = tf.name
            try:
                res = subprocess.run([node, "--check", js_path],
                                     capture_output=True, text=True, timeout=15)
                check("node --check не нашёл синтаксических ошибок",
                     res.returncode == 0, res.stderr[:300])
            finally:
                Path(js_path).unlink(missing_ok=True)

    if not HAVE_DEPS:
        print(f"\ntest_dashboard: сквозной сценарий пропущен — {SKIP_REASON}")
        print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
        return 1 if FAIL else 0

    from maos.api import server as api_server
    from maos.config import Config
    from http.server import ThreadingHTTPServer

    section("Сквозной сценарий: используемые дашбордом эндпоинты РЕАЛЬНО отвечают")
    with tempfile.TemporaryDirectory(prefix="maos_dash_pg_") as tmp:
        srv = pgserver.get_server(Path(tmp) / "pgdata")
        try:
            dsn = _fresh_dsn(srv)
            cfg = Config(db_dsn=dsn, embedding_dim=32)
            api_server.Handler.cfg = cfg
            api_server.Handler.token = None
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            time.sleep(0.2)

            def get(path):
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return r.status, json.loads(r.read())

            def post(path, body):
                data = json.dumps(body).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}", data=data, method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())

            # Реальная проверка (не текстовый grep по исходникам): каждый
            # GET-маршрут, который использует dashboard.html, должен
            # РЕАЛЬНО существовать на сервере — если бы маршрут был удалён
            # или переименован в server.py, здесь всплыл бы 404, а не
            # текстовое совпадение в докстринге, которое ничего не
            # гарантирует про фактическую регистрацию обработчика.
            for ep in ("/health", "/info", "/v1/agents", "/v1/memory/stats",
                      "/v1/graph", "/v1/chains", "/v1/onboarding/status"):
                try:
                    code, _ = get(ep)
                    check(f"GET {ep}, используемый дашбордом, реально отвечает "
                         "(не 404)", code != 404)
                except urllib.error.HTTPError as exc:
                    check(f"GET {ep}, используемый дашбордом, реально отвечает "
                         "(не 404)", exc.code != 404)

            code, dash_body = None, None
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/dashboard", timeout=5) as r:
                code, dash_body = r.status, r.read().decode()
            check("дашборд реально отдаётся сервером", code == 200)
            check("это HTML MAOS", "<title>MAOS" in dash_body)

            _, status0 = get("/v1/onboarding/status")
            check("пустая база -> is_empty=True (баннер должен показаться)",
                 status0["is_empty"] is True)

            _, seed_result = post("/v1/onboarding/seed", {})
            check("посев через API реально создаёт агентов",
                 len(seed_result["created"]) == 3)

            _, agents_after = get("/v1/agents")
            check("агенты видны через /v1/agents (дашборд их так и подгружает)",
                 len(agents_after["agents"]) == 3)

            _, status1 = get("/v1/onboarding/status")
            check("после посева баннер больше не должен предлагать создание",
                 status1["is_empty"] is False and status1["demo_missing"] == [])

            httpd.shutdown()
            httpd.server_close()
        finally:
            srv.cleanup()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
