"""Тесты maos.agents.loop (run_tool_loop) и AgentRuntime с назначенными
инструментами: полный цикл «модель -> инструмент -> модель» на реальных
файлах (workspace) и реальном embedded Postgres.

Философия: агент MAOS по умолчанию — чистый синтезатор, но агенту с
agent.tools ("files", "web" и т.п.) назначается настоящий цикл вызова
инструментов, как в agent_system. Все сценарии здесь проверяются на
РЕАЛЬНОМ fake HTTP LLM-сервере (управляемом по сценарию через счётчик
вызовов) и реальной файловой системе — не моки уровня Python-объектов.
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

from maos.agents.runtime import AgentRuntime                        # noqa: E402
from maos.config import Config                                      # noqa: E402

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
        _tmp = tempfile.mkdtemp(prefix="maos_toolloop_pgserver_")
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


class ScriptedToolHandler(BaseHTTPRequestHandler):
    """Сервер, отвечающий по заранее заданному сценарию (список сообщений
    ассистента, включая tool_calls), по одному на вызов — управляемый и
    воспроизводимый, но настоящий HTTP/JSON протокол."""

    script: list[dict] = []
    calls = 0
    last_bodies: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        type(self).last_bodies.append(body)
        idx = type(self).calls
        type(self).calls += 1
        if idx < len(type(self).script):
            msg = type(self).script[idx]
        else:
            msg = {"role": "assistant", "content": "(сценарий исчерпан)"}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _tool_call_msg(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def _text_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_agent_tools_loop: тесты пропущены — {SKIP_REASON}")
        return 0

    from maos.memory.store import Store

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedToolHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    import os
    os.environ["LOCAL_BASE_URL"] = f"http://127.0.0.1:{port}/v1"

    try:
        st = Store(_fresh_dsn(), dim=32)

        section("Полный успешный цикл: write_file -> финальный текст")
        with tempfile.TemporaryDirectory() as wsroot:
            cfg = Config(default_local_model="local::llama3", llm_retries=0,
                        workspace_root=wsroot, max_tool_steps=5)
            st.create_agent("coder1", "Coder1", llm_ref="local::llama3",
                            tools="files")
            agent_row = st.get_agent("coder1")

            ScriptedToolHandler.calls = 0
            ScriptedToolHandler.last_bodies = []
            ScriptedToolHandler.script = [
                _tool_call_msg("write_file", {"path": "out.txt", "content": "данные"}),
                _text_msg("Файл записан успешно."),
            ]
            events = []
            runtime = AgentRuntime(cfg)
            turn = runtime.respond(agent_row, "Сохрани данные в out.txt", [],
                                   store=st, on_event=lambda k, d: events.append((k, d)))
            check("финальный текст получен", turn.text == "Файл записан успешно.")
            check("stopped_by == done", turn.stopped_by == "done")
            check("ровно 1 вызов инструмента учтён", turn.tool_calls == 1)
            check("файл реально создан в изолированной папке агента",
                 (Path(wsroot) / "coder1" / "out.txt").read_text() == "данные")
            check("события: tool_start -> tool_end -> answer",
                 [e[0] for e in events] == ["tool_start", "tool_end", "answer"])

        section("Инструмент вернул ошибку — модель получает шанс исправиться")
        with tempfile.TemporaryDirectory() as wsroot:
            cfg = Config(default_local_model="local::llama3", llm_retries=0,
                        workspace_root=wsroot, max_tool_steps=5)
            st.create_agent("coder2", "Coder2", llm_ref="local::llama3",
                            tools="files")
            agent_row = st.get_agent("coder2")

            ScriptedToolHandler.calls = 0
            ScriptedToolHandler.last_bodies = []
            ScriptedToolHandler.script = [
                _tool_call_msg("read_file", {"path": "nonexistent.txt"}),
                _text_msg("Файла нет, сообщаю об этом честно."),
            ]
            runtime = AgentRuntime(cfg)
            turn = runtime.respond(agent_row, "Прочитай nonexistent.txt", [], store=st)
            check("итоговый ответ получен после ошибки инструмента",
                 turn.text == "Файла нет, сообщаю об этом честно.")
            check("stopped_by == done несмотря на ошибку инструмента",
                 turn.stopped_by == "done")
            # ошибка инструмента реально дошла до истории как "ОШИБКА: ..."
            second_call_body = ScriptedToolHandler.last_bodies[1]
            tool_msgs = [m for m in second_call_body["messages"] if m.get("role") == "tool"]
            check("текст ошибки передан модели как содержимое tool-сообщения",
                 tool_msgs and "ОШИБКА" in tool_msgs[0]["content"])

        section("Лимит шагов (max_tool_steps) — честный отчёт, а не бесконечный цикл")
        with tempfile.TemporaryDirectory() as wsroot:
            cfg = Config(default_local_model="local::llama3", llm_retries=0,
                        workspace_root=wsroot, max_tool_steps=3)
            st.create_agent("coder3", "Coder3", llm_ref="local::llama3",
                            tools="files")
            agent_row = st.get_agent("coder3")

            ScriptedToolHandler.calls = 0
            ScriptedToolHandler.last_bodies = []
            # модель бесконечно зовёт один и тот же безобидный инструмент
            ScriptedToolHandler.script = [
                _tool_call_msg("list_files", {}) for _ in range(10)
            ]
            runtime = AgentRuntime(cfg)
            turn = runtime.respond(agent_row, "Зацикленная задача", [], store=st)
            check("stopped_by == max_steps", turn.stopped_by == "max_steps")
            check("вызовов ровно cfg.max_tool_steps",
                 ScriptedToolHandler.calls == cfg.max_tool_steps)
            check("сообщение объясняет исчерпание лимита",
                 "предел" in turn.text.lower() or "шаг" in turn.text.lower())

        section("Агент БЕЗ tools — ведёт себя как раньше (без цикла инструментов)")
        with tempfile.TemporaryDirectory() as wsroot:
            cfg = Config(default_local_model="local::llama3", llm_retries=0,
                        workspace_root=wsroot)
            st.create_agent("plain", "Plain", llm_ref="local::llama3", tools="")
            agent_row = st.get_agent("plain")

            ScriptedToolHandler.calls = 0
            ScriptedToolHandler.last_bodies = []
            ScriptedToolHandler.script = [_text_msg("Просто ответ без инструментов.")]
            runtime = AgentRuntime(cfg)
            turn = runtime.respond(agent_row, "Привет", [], store=st)
            check("обычный текстовый ответ без вызова инструментов",
                 turn.text == "Просто ответ без инструментов.")
            check("tool_calls == 0 для агента без навыков", turn.tool_calls == 0)
            check("В ЗАПРОСЕ К МОДЕЛИ НЕ БЫЛО tools (агент без tools "
                 "не отправляет схемы функций)",
                 "tools" not in ScriptedToolHandler.last_bodies[0])

        section("Агент с несуществующим навыком в tools — явная ошибка, не тихий сбой")
        with tempfile.TemporaryDirectory() as wsroot:
            cfg = Config(default_local_model="local::llama3", llm_retries=0,
                        workspace_root=wsroot)
            st.create_agent("broken_tools", "Broken", llm_ref="local::llama3",
                            tools="files,ghost_skill")
            agent_row = st.get_agent("broken_tools")
            ScriptedToolHandler.calls = 0
            runtime = AgentRuntime(cfg)
            turn = runtime.respond(agent_row, "Что-нибудь", [], store=st)
            check("ошибка конфигурации навыков видна в ответе",
                 "ghost_skill" in turn.text)
            check("stopped_by == error", turn.stopped_by == "error")
            check("модель НЕ вызывалась вообще (ошибка до отправки запроса)",
                 ScriptedToolHandler.calls == 0)

        st.close()
    finally:
        os.environ.pop("LOCAL_BASE_URL", None)
        httpd.shutdown()
        httpd.server_close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
