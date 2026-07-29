"""Тесты dataforge.copilot: AI Copilot над реальным REST API DataForge
(ТЗ §3.6, K6).

Три реальных сервера в одном тесте — намеренно, это и есть суть
Copilot-архитектуры платформы:
  1. embedded PostgreSQL (pgserver) — хранилище DataForge.
  2. Реальный uvicorn-сервер FastAPI DataForge (тот же API, что
     доступен человеку через дашборд).
  3. Fake OpenAI-совместимый HTTP-сервер (ThreadingHTTPServer),
     эмулирующий tool-calling протокол — Copilot обращается к нему как
     к настоящему LLM-провайдеру, разницы в протоколе нет.

Проверяется, что Copilot: (а) не имеет прямого доступа к Store — только
через HTTP к своему же API с тем же токеном; (б) пишет каждое
взаимодействие в audit trail; (в) корректно обрабатывает отсутствие
LLM-конфигурации и ошибки LLM-провайдера отдельными кодами.
"""
from __future__ import annotations

import json
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        _tmp = tempfile.mkdtemp(prefix="forge_copilot_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"

try:
    import httpx  # type: ignore
    import uvicorn  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = SKIP_REASON or "httpx/uvicorn не установлены"


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


class FakeOpenAI(BaseHTTPRequestHandler):
    """Эмулирует .../chat/completions с tool-calling: первый вызов
    просит инструмент get_dashboard_stats, второй (после результата в
    истории) отвечает финальным текстом. Может имитировать HTTP-ошибку
    сервера (`fail_with`) для проверки обработки сбоя LLM."""

    calls: list[dict] = []
    fail_with: int = 0
    tool_name: str = "get_dashboard_stats"
    tool_args: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        type(self).calls.append(body)
        if type(self).fail_with:
            self.send_response(type(self).fail_with)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        has_tool_result = any(m.get("role") == "tool" for m in body["messages"])
        if not has_tool_result:
            resp = {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function",
                    "function": {"name": type(self).tool_name,
                                "arguments": json.dumps(type(self).tool_args)}}],
            }}]}
        else:
            resp = {"choices": [{"message": {
                "role": "assistant", "content": "Ответ на основе данных платформы."}}]}
        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _ApiServer:
    def __init__(self, dsn: str, llm_base_url: str = "", token: str | None = None):
        from dataforge.api.server import app, configure
        from dataforge.config import Config

        self.cfg = Config(db_dsn=dsn, llm_base_url=llm_base_url, llm_model="fake-model")
        configure(self.cfg, token)
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self._wait_ready()

    def _wait_ready(self, attempts: int = 40) -> None:
        for _ in range(attempts):
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=1)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("сервер не поднялся за отведённое время")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_copilot: тесты пропущены — {SKIP_REASON}")
        return 0

    llm_port = _free_port()
    llm_srv = ThreadingHTTPServer(("127.0.0.1", llm_port), FakeOpenAI)
    llm_thread = threading.Thread(target=llm_srv.serve_forever, daemon=True)
    llm_thread.start()
    llm_base_url = f"http://127.0.0.1:{llm_port}"

    try:
        section("Copilot без настроенного LLM -> 503, платформа продолжает работать")
        srv0 = _ApiServer(_fresh_dsn(), llm_base_url="")
        try:
            r = httpx.post(f"{srv0.base_url}/v1/copilot/ask",
                           json={"prompt": "тест", "mode": "ops", "actor": "human:x"},
                           timeout=5)
            check("без LLM -> 503", r.status_code == 503)
            check("сообщение объясняет что модуль не настроен",
                 "не настроен" in r.json()["error"])
            r2 = httpx.get(f"{srv0.base_url}/health", timeout=5)
            check("остальная платформа работает независимо от Copilot",
                 r2.status_code == 200)
        finally:
            srv0.stop()

        section("Copilot: полный tool-calling цикл через реальный HTTP (не deadlock)")
        FakeOpenAI.calls.clear()
        FakeOpenAI.fail_with = 0
        FakeOpenAI.tool_name = "get_dashboard_stats"
        FakeOpenAI.tool_args = {}
        srv = _ApiServer(_fresh_dsn(), llm_base_url=llm_base_url)
        try:
            r = httpx.post(f"{srv.base_url}/v1/copilot/ask",
                           json={"prompt": "покажи статистику платформы",
                                "mode": "ops", "actor": "human:test"}, timeout=10)
            check("запрос успешен (не завис — deadlock исключён)", r.status_code == 200)
            data = r.json()
            check("получен финальный текстовый ответ", data["text"] != "")
            check("инструмент был вызван", len(data["tools_called"]) == 1
                 and data["tools_called"][0]["name"] == "get_dashboard_stats")
            check("LLM был вызван ровно 2 раза (запрос инструмента + финальный ответ)",
                 len(FakeOpenAI.calls) == 2)

            section("Copilot: неизвестный инструмент -> ошибка возвращается модели, не роняет цикл")
            FakeOpenAI.calls.clear()
            FakeOpenAI.tool_name = "delete_everything"
            r2 = httpx.post(f"{srv.base_url}/v1/copilot/ask",
                            json={"prompt": "сделай что-нибудь опасное", "mode": "ops",
                                 "actor": "human:test"}, timeout=10)
            check("запрос всё равно завершился успешно (ошибка ушла в историю модели)",
                 r2.status_code == 200)
            check("в tools_called виден вызов неизвестного инструмента",
                 r2.json()["tools_called"][0]["name"] == "delete_everything")
            check("результат содержит пометку ОШИБКА",
                 "ОШИБКА" in r2.json()["tools_called"][0]["result"])

            section("Copilot: неверный режим -> 400 (ошибка запроса, не 503)")
            FakeOpenAI.tool_name = "get_dashboard_stats"
            r3 = httpx.post(f"{srv.base_url}/v1/copilot/ask",
                            json={"prompt": "x", "mode": "not_a_mode", "actor": "human:x"},
                            timeout=5)
            check("неверный режим -> 400", r3.status_code == 400)

            section("Copilot: ошибка LLM-провайдера -> 503")
            FakeOpenAI.fail_with = 500
            r4 = httpx.post(f"{srv.base_url}/v1/copilot/ask",
                            json={"prompt": "x", "mode": "ops", "actor": "human:x"},
                            timeout=5)
            check("сбой LLM-провайдера -> 503", r4.status_code == 503)
            FakeOpenAI.fail_with = 0

            section("Copilot: setup-режим доступен отдельно от ops")
            r5 = httpx.post(f"{srv.base_url}/v1/copilot/ask",
                            json={"prompt": "как подключить источник?", "mode": "setup",
                                 "actor": "human:x"}, timeout=10)
            check("setup режим -> 200", r5.status_code == 200)

            section("Copilot: аудит взаимодействий")
            r6 = httpx.get(f"{srv.base_url}/v1/copilot/history", timeout=5)
            check("история содержит все успешные и часть неуспешных попыток",
                 len(r6.json()) >= 3)
            check("запись содержит режим и actor",
                 any(h["mode"] == "setup" for h in r6.json())
                 and any(h["actor"] == "human:test" for h in r6.json()))
            r7 = httpx.get(f"{srv.base_url}/v1/copilot/history",
                          params={"actor": "human:test"}, timeout=5)
            check("фильтр по actor работает",
                 all(h["actor"] == "human:test" for h in r7.json()))
        finally:
            srv.stop()

        section("Copilot: изоляция — не имеет прямого доступа к БД в обход API")
        # Инструменты работают ТОЛЬКО через HTTP-клиент (ApiTools принимает
        # httpx.Client, не Store) — структурная проверка модуля.
        import dataforge.copilot.tools as tools_mod
        import inspect
        sig = inspect.signature(tools_mod.ApiTools.__init__)
        check("ApiTools.__init__ принимает httpx.Client, а не Store/БД-объект",
             "client" in sig.parameters and "store" not in sig.parameters)

    finally:
        llm_srv.shutdown()
        llm_thread.join(timeout=5)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
