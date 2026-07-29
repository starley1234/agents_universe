"""Сквозной тест: поднимаем поддельный OpenAI-сервер и HTTP API агента.

Проверяется вся цепочка на настоящих сокетах: CLI/API -> драйвер модели ->
HTTP -> разбор ответа -> вызов инструмента -> файл на диске -> ответ.
Заглушка стоит только на месте самой модели.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config      # noqa: E402
from agent.server import Handler     # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


# ------------------------------------------------- поддельная модель
class FakeLLMHandler(BaseHTTPRequestHandler):
    """Сценарий: сначала просит записать файл, потом отвечает текстом."""
    calls = 0

    def log_message(self, *a):  # тихо
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        type(self).calls += 1
        if type(self).calls == 1:
            msg = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps(
                    {"path": "hello.txt", "content": "сделано агентом"})},
            }]}
        else:
            msg = {"role": "assistant",
                   "content": "Готово: файл hello.txt создан."}
        body = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    print("=" * 60)
    print("СКВОЗНОЙ ТЕСТ (реальные HTTP-соединения)")
    print("=" * 60)

    llm_port, api_port = free_port(), free_port()
    llm_srv = ThreadingHTTPServer(("127.0.0.1", llm_port), FakeLLMHandler)
    threading.Thread(target=llm_srv.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="openai", model="fake",
                     base_url=f"http://127.0.0.1:{llm_port}/v1",
                     api_key="test", workspace=td, skills=["files"],
                     max_steps=6)
        cfg.sandbox.mode = "off"

        print("\nHTTP API агента\n" + "─" * 20)
        Handler.cfg = cfg
        Handler.token = "secret"
        api = ThreadingHTTPServer(("127.0.0.1", api_port), Handler)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        time.sleep(0.3)
        base = f"http://127.0.0.1:{api_port}"

        # /health без токена
        with urllib.request.urlopen(f"{base}/health", timeout=10) as r:
            check("/health отвечает", json.load(r)["status"] == "ok")

        # защита токеном
        try:
            urllib.request.urlopen(f"{base}/info", timeout=10)
            check("без токена доступ закрыт", False, "пустил!")
        except urllib.error.HTTPError as e:
            check("без токена доступ закрыт", e.code == 401)

        req = urllib.request.Request(f"{base}/info",
                                     headers={"Authorization": "Bearer secret"})
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.load(r)
        check("/info отдаёт инструменты", "write_file" in info["tools"])
        check("ключ не светится в /info",
              info["config"].get("api_key") in (None, "***"),
              str(info["config"].get("api_key")))

        # основной прогон
        FakeLLMHandler.calls = 0
        payload = json.dumps({"task": "создай файл hello.txt"}).encode()
        req = urllib.request.Request(
            f"{base}/run", data=payload, method="POST",
            headers={"Authorization": "Bearer secret",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
        check("задача завершена", res["stopped_by"] == "done", res["stopped_by"])
        check("инструмент вызывался", res["tool_calls"] == 1)
        check("файл реально создан", (Path(td) / "hello.txt").exists())
        check("содержимое верное",
              (Path(td) / "hello.txt").read_text() == "сделано агентом")
        check("в трассировке виден вызов",
              any(c["name"] == "write_file"
                  for s in res["trace"] for c in s["calls"]))

        # НЕГАТИВНЫЙ: пустая задача обязана быть отвергнута
        try:
            req = urllib.request.Request(
                f"{base}/run", data=json.dumps({"task": ""}).encode(),
                method="POST",
                headers={"Authorization": "Bearer secret",
                         "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            check("пустая задача отклонена", False, "приняли!")
        except urllib.error.HTTPError as e:
            check("пустая задача отклонена", e.code == 400)

        # стриминг
        FakeLLMHandler.calls = 0
        (Path(td) / "hello.txt").unlink(missing_ok=True)
        req = urllib.request.Request(
            f"{base}/run/stream",
            data=json.dumps({"task": "ещё раз"}).encode(), method="POST",
            headers={"Authorization": "Bearer secret",
                     "Content-Type": "application/json"})
        events = []
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                line = line.decode().strip()
                if line:
                    events.append(json.loads(line))
        kinds = [e["event"] for e in events]
        check("стрим отдаёт события", len(events) > 0, str(kinds))
        check("виден старт инструмента", "tool_start" in kinds, str(kinds))
        check("виден финал", "done" in kinds, str(kinds))

        api.shutdown()

        # ошибка модели не роняет процесс
        print("\nУстойчивость\n" + "─" * 20)
        bad = Config(provider="openai", model="x",
                     base_url="http://127.0.0.1:1/v1", api_key="k",
                     workspace=td, skills=["files"], max_steps=3)
        bad.sandbox.mode = "off"
        from agent.build import build_agent
        res2 = build_agent(bad).run("задача")
        check("недоступная модель -> понятная ошибка",
              res2.stopped_by == "error" and "модели" in res2.answer.lower(),
              res2.answer[:100])

    llm_srv.shutdown()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
