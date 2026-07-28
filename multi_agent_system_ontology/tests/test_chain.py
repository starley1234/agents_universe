"""Тесты maos.orchestrator.chain (ChainRunner): детерминированная ручная
цепочка Agent_A -> Agent_B (ТЗ п.5) — на реальном Postgres+pgvector и
фейковом HTTP-сервере, эмулирующем LLM.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.config import Config                                     # noqa: E402
from maos.llm.embeddings import HashEmbedder                        # noqa: E402
from maos.orchestrator.chain import ChainError, ChainRunner, _fill  # noqa: E402

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
        _tmp = tempfile.mkdtemp(prefix="maos_chain_pgserver_")
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
    always_bad = False   # эмулирует падение шага (сервер отвечает 500)

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).calls += 1
        if type(self).always_bad:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        last_user = ""
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        text = f"[шаг {type(self).calls}] обработано: {last_user}"
        out = json.dumps({"choices": [{"message": {"role": "assistant",
                                                    "content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    section("_fill: подстановка плейсхолдеров {goal}/{prev}/{step_N}")
    check("{goal} подставляется", _fill("Цель: {goal}", "сделай X", []) == "Цель: сделай X")
    check("{prev} берёт последний ответ",
         _fill("Продолжи: {prev}", "цель", ["первый", "второй"]) == "Продолжи: второй")
    check("{step_0} берёт конкретный шаг",
         _fill("См. {step_0}", "цель", ["первый", "второй"]) == "См. первый")
    try:
        _fill("{step_5}", "цель", ["a"])
        check("{step_N} на ещё не выполненный шаг кидает ChainError", False)
    except ChainError:
        check("{step_N} на ещё не выполненный шаг кидает ChainError", True)

    if not HAVE_DEPS:
        print(f"\ntest_chain: тесты ChainRunner пропущены — {SKIP_REASON}")
        print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
        return 1 if FAIL else 0

    from maos.memory.store import Store

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), EchoingHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    import os
    os.environ["LOCAL_BASE_URL"] = f"http://127.0.0.1:{port}/v1"

    try:
        st = Store(_fresh_dsn(), dim=32)
        emb = HashEmbedder(dim=32)
        cfg = Config(default_local_model="local::llama3", llm_retries=0)

        st.create_agent("researcher", "Researcher", description="Исследует тему",
                        llm_ref="local::llama3")
        st.create_agent("writer", "Writer", description="Пишет итоговый отчёт",
                        llm_ref="local::llama3")

        section("ChainRunner: полная успешная цепочка A -> B")
        events: list[tuple[str, dict]] = []
        runner = ChainRunner(cfg, st, emb, on_event=lambda k, d: events.append((k, d)))
        result = runner.run("Исследовать рынок", ["researcher", "writer"])
        check("статус done", result["status"] == "done")
        check("оба ответа собраны", len(result["answers"]) == 2)
        check("второй шаг реально получил ответ первого",
             "обработано: " in result["answers"][1] and
             "[шаг 1]" in result["answers"][0])

        steps = st.chain_steps(result["chain_run_id"])
        check("оба шага помечены done", all(s["status"] == "done" for s in steps))
        check("порядок событий: chain_start первым",
             events[0][0] == "chain_start")
        check("chain_finish последним", events[-1][0] == "chain_finish")
        check("оба step_start/step_done присутствуют",
             sum(1 for k, _ in events if k == "step_start") == 2 and
             sum(1 for k, _ in events if k == "step_done") == 2)

        section("ChainRunner: несуществующий агент в списке -> ChainError")
        try:
            runner.run("цель", ["researcher", "ghost"])
            check("несуществующий агент кидает ChainError", False)
        except ChainError:
            check("несуществующий агент кидает ChainError", True)

        section("ChainRunner: пустая цепочка -> ChainError")
        try:
            runner.run("цель", [])
            check("пустой список агентов кидает ChainError", False)
        except ChainError:
            check("пустой список агентов кидает ChainError", True)

        section("ChainRunner: сбой первого шага останавливает цепочку")
        EchoingHandler.always_bad = True
        result2 = runner.run("Другая цель", ["researcher", "writer"])
        check("статус failed", result2["status"] == "failed")
        check("ни один ответ не собран", result2["answers"] == [])
        steps2 = st.chain_steps(result2["chain_run_id"])
        check("первый шаг failed", steps2[0]["status"] == "failed")
        check("второй шаг skipped (НЕ выполнялся)", steps2[1]["status"] == "skipped")
        EchoingHandler.always_bad = False

        section("ChainRunner: остановка по stop_event пропускает все шаги")
        import threading as th
        stop_event = th.Event()
        stop_event.set()
        calls_before = EchoingHandler.calls
        runner_stopped = ChainRunner(cfg, st, emb, stop_event=stop_event)
        result3 = runner_stopped.run("Цель, которую остановили",
                                     ["researcher", "writer"])
        check("статус stopped", result3["status"] == "stopped")
        check("модель НЕ вызывалась", EchoingHandler.calls == calls_before)
        steps3 = st.chain_steps(result3["chain_run_id"])
        check("все шаги skipped", all(s["status"] == "skipped" for s in steps3))

        st.close()
    finally:
        os.environ.pop("LOCAL_BASE_URL", None)
        httpd.shutdown()
        httpd.server_close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
