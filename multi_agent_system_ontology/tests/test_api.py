"""Тесты HTTP API maos.api.server: реальные сокеты (ThreadingHTTPServer),
без моков — как в agent_system/tests/test_webui.py.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.api import server as api_server                          # noqa: E402
from maos.config import Config                                     # noqa: E402

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
        _tmp = tempfile.mkdtemp(prefix="maos_api_pgserver_")
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


class EchoingHandler(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).calls += 1
        last_user = ""
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        text = f"эхо: {last_user}"
        out = json.dumps({"choices": [{"message": {"role": "assistant",
                                                    "content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class FakeOmniVoiceHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).calls.append(body)
        out = f"AUDIO:{body['text']}:{body['voice']}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802
        if self.path == "/voices":
            out = json.dumps({"voices": [{"id": "alloy"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)


class _ApiCtx:
    """Поднимает реальный maos.api.server.Handler на localhost, реальный
    фейковый LLM-сервер и передаёт DSN временной базы в общем кластере."""

    def __init__(self, token: str | None = "test-token"):
        self.llm_httpd = ThreadingHTTPServer(("127.0.0.1", 0), EchoingHandler)
        self.llm_port = self.llm_httpd.server_address[1]
        threading.Thread(target=self.llm_httpd.serve_forever, daemon=True).start()

        FakeOmniVoiceHandler.calls = []
        self.tts_httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOmniVoiceHandler)
        self.tts_port = self.tts_httpd.server_address[1]
        threading.Thread(target=self.tts_httpd.serve_forever, daemon=True).start()

        import os
        os.environ["LOCAL_BASE_URL"] = f"http://127.0.0.1:{self.llm_port}/v1"

        self.cfg = Config(db_dsn=_fresh_dsn(), embedding_provider="hash",
                          embedding_model="hash-256", embedding_dim=32,
                          default_local_model="local::llama3",
                          complexity_char_threshold=10_000, llm_retries=0,
                          tts_provider="omnivoice",
                          tts_base_url=f"http://127.0.0.1:{self.tts_port}")
        api_server.Handler.cfg = self.cfg
        api_server.Handler.token = token
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.token = token

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path: str, auth: bool = True) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers=self._headers() if auth else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path: str, body: dict, auth: bool = True) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=self._headers() if auth else {"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post_raw(self, path: str, body: dict) -> tuple[int, bytes, str]:
        """Как post(), но возвращает СЫРЫЕ байты тела и Content-Type — для
        бинарных ответов (например /v1/tts/speak), которые не JSON."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return (resp.status, resp.read(),
                       resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")

    def close(self) -> None:
        import os
        os.environ.pop("LOCAL_BASE_URL", None)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.llm_httpd.shutdown()
        self.llm_httpd.server_close()
        self.tts_httpd.shutdown()
        self.tts_httpd.server_close()


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_api: тесты пропущены — {SKIP_REASON}")
        return 0

    section("GET /health не требует токена")
    ctx = _ApiCtx()
    try:
        code, data = ctx.get("/health", auth=False)
        check("статус 200", code == 200)
        check("status == ok", data.get("status") == "ok")

        section("GET /dashboard отдаёт HTML без токена")
        req = urllib.request.Request(f"http://127.0.0.1:{ctx.port}/dashboard")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            check("статус 200", resp.status == 200)
            check("это HTML-страница MAOS", "<title>MAOS" in body)

        section("Авторизация: без токена -> 401")
        code2, _ = ctx.get("/v1/agents", auth=False)
        check("без Authorization -> 401", code2 == 401)
        code3, _ = ctx.get("/v1/agents")
        check("с верным токеном -> 200", code3 == 200)

        section("POST /v1/agents: создание агента")
        code4, data4 = ctx.post("/v1/agents", {
            "slug": "coder", "name": "Coder", "description": "Пишет код на Python",
            "keywords": "код python", "llm_ref": "local::llama3",
            "system_prompt": "Ты программист."})
        check("создание агента -> 200", code4 == 200)
        check("slug возвращён", data4.get("slug") == "coder")

        code5, data5 = ctx.post("/v1/agents", {"slug": "BAD SLUG!", "name": "x"})
        check("невалидный slug -> 400", code5 == 400)

        code6, data6 = ctx.post("/v1/agents", {"slug": "noname"})
        check("без name -> 400", code6 == 400)

        section("GET /v1/agents и /v1/agents/<slug>")
        code7, data7 = ctx.get("/v1/agents")
        check("список агентов виден", any(a["slug"] == "coder" for a in data7["agents"]))
        code8, data8 = ctx.get("/v1/agents/coder")
        check("детали агента получены", data8["agent"]["name"] == "Coder")
        code9, _ = ctx.get("/v1/agents/ghost")
        check("несуществующий агент -> 404", code9 == 404)

        section("PATCH (через POST) /v1/agents/<slug>: обновление")
        code10, data10 = ctx.post("/v1/agents/coder",
                                  {"description": "Пишет код на Rust"})
        check("обновление применилось", data10.get("updated") is True)
        code11, data11 = ctx.get("/v1/agents/coder")
        check("описание реально изменилось",
             data11["agent"]["description"] == "Пишет код на Rust")

        section("POST /v1/chat: полный цикл")
        code12, data12 = ctx.post("/v1/chat",
                                  {"message": "Привет!", "agent_slug": "coder"})
        check("chat -> 200", code12 == 200)
        check("ответ содержит эхо от фейкового LLM", "эхо: Привет!" in data12["answer"])
        check("agent_slug верный", data12["agent_slug"] == "coder")
        check("provider_model указан", data12["provider_model"] == "local::llama3")
        conv_id = data12["conversation_id"]

        code13, _ = ctx.post("/v1/chat", {"message": ""})
        check("пустое сообщение -> 400", code13 == 400)

        section("GET /v1/conversations/<id>: история диалога")
        code14, data14 = ctx.get(f"/v1/conversations/{conv_id}")
        check("2 сообщения в истории (user+agent)", len(data14["messages"]) == 2)
        code15, _ = ctx.get("/v1/conversations/999999")
        check("несуществующий диалог -> 404", code15 == 404)

        section("GET /v1/memory/stats")
        code16, data16 = ctx.get("/v1/memory/stats")
        check("agents >= 1", data16["agents"] >= 1)
        check("messages >= 2", data16["messages"] >= 2)
        check("memory_quanta >= 1", data16["memory_quanta"] >= 1)

        section("Цепочка через API: POST /v1/chain/start + GET /v1/chain/<id>")
        ctx.post("/v1/agents", {"slug": "writer", "name": "Writer",
                                "description": "Пишет тексты",
                                "llm_ref": "local::llama3"})
        code17, data17 = ctx.post("/v1/chain/start",
                                  {"goal": "Тестовая цель",
                                   "agents": ["coder", "writer"]})
        check("chain/start -> 200", code17 == 200)
        check("статус done", data17["status"] == "done")
        chain_id = data17["chain_run_id"]

        code18, data18 = ctx.get(f"/v1/chain/{chain_id}")
        check("детали цепочки получены", len(data18["steps"]) == 2)
        check("оба шага done",
             all(s["status"] == "done" for s in data18["steps"]))

        code19, _ = ctx.post("/v1/chain/start", {"goal": "x", "agents": []})
        check("пустой agents -> 400", code19 == 400)

        section("GET /v1/graph")
        code20, data20 = ctx.get("/v1/graph")
        check("graph отдаёт nodes/edges ключи",
             "nodes" in data20 and "edges" in data20)

        section("POST /v1/maintenance/run: ручной запуск обслуживания")
        code21, data21 = ctx.post("/v1/maintenance/run", {})
        check("maintenance/run -> 200", code21 == 200)
        check("отчёт содержит числовые поля",
             isinstance(data21["distilled"], int) and
             isinstance(data21["deduped"], int))

        section("DELETE-подобный маршрут: /v1/agents/<slug>/delete")
        code22, data22 = ctx.post("/v1/agents/writer/delete", {})
        check("удаление агента -> 200", code22 == 200)
        check("deleted == True", data22["deleted"] is True)
        code23, _ = ctx.get("/v1/agents/writer")
        check("удалённый агент больше не найден", code23 == 404)

        section("Неизвестный маршрут -> 404")
        code24, _ = ctx.get("/v1/nonexistent")
        check("неизвестный GET маршрут -> 404", code24 == 404)
        code25, _ = ctx.post("/v1/nonexistent", {})
        check("неизвестный POST маршрут -> 404", code25 == 404)

        section("Онбординг: /v1/onboarding/status и /v1/onboarding/seed")
        code26, data26 = ctx.get("/v1/onboarding/status")
        check("onboarding/status -> 200", code26 == 200)
        check("coder уже создан ранее — demo_missing короче полного набора",
             "coder" not in data26["demo_missing"])
        code27, data27 = ctx.post("/v1/onboarding/seed", {})
        check("onboarding/seed -> 200", code27 == 200)
        check("после посева demo_missing пуст", data27["status"]["demo_missing"] == [])
        code28, data28 = ctx.post("/v1/onboarding/seed", {})
        check("повторный посев идемпотентен (ничего не создал)",
             data28["created"] == [])
        code29, data29 = ctx.get("/v1/agents")
        demo_slugs = {"coder", "writer", "analyst"}
        check("демо-агенты реально видны в общем списке /v1/agents",
             demo_slugs <= {a["slug"] for a in data29["agents"]})

        section("TTS: GET /v1/tts/voices и POST /v1/tts/speak (реальный OmniVoice)")
        code30, data30 = ctx.get("/v1/tts/voices")
        check("tts/voices -> 200", code30 == 200)
        check("голос alloy виден", any(v["id"] == "alloy" for v in data30["voices"]))

        code31, audio31, ctype31 = ctx.post_raw(
            "/v1/tts/speak", {"text": "привет", "voice": "alloy"})
        check("tts/speak -> 200", code31 == 200)
        check("Content-Type — audio/*", ctype31.startswith("audio/"))
        check("аудио реально пришло от фейкового OmniVoice",
             audio31 == "AUDIO:привет:alloy".encode("utf-8"))

        code32, _ = ctx.post("/v1/tts/speak", {"text": "", "voice": "alloy"})
        check("пустой text -> 400", code32 == 400)

        code33, _ = ctx.post("/v1/tts/speak", {"text": "текст"})
        check("без voice и без agent_slug -> 400", code33 == 400)

        ctx.post("/v1/agents", {"slug": "narrator", "name": "Narrator",
                                "voice_provider": "omnivoice", "voice_id": "rachel"})
        code34, audio34, _ = ctx.post_raw(
            "/v1/tts/speak", {"text": "текст от агента", "agent_slug": "narrator"})
        check("голос по agent_slug используется, если voice не передан",
             code34 == 200 and audio34 == "AUDIO:текст от агента:rachel".encode("utf-8"))

        code35, _ = ctx.post("/v1/tts/speak",
                             {"text": "текст", "agent_slug": "ghost_agent"})
        check("несуществующий agent_slug для TTS -> 404", code35 == 404)
    finally:
        ctx.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
