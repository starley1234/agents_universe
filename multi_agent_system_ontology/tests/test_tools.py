"""Тесты maos.tools (files/web/office_docs — перенесённые из agent_system
почти без изменений) и maos.tools.toolbox (сборка набора по строке
agent.tools). Реальные HTTP-серверы для web, реальная файловая система
для files/office_docs — без моков.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.tools import files as files_tools                        # noqa: E402
from maos.tools import web as web_tools                             # noqa: E402
from maos.tools.base import ToolError, Workspace                    # noqa: E402
from maos.tools.toolbox import (KNOWN_TOOLS, ToolboxError,           # noqa: E402
                                agent_workspace, build_toolbox,
                                parse_tools_field)

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


SEARXNG_JSON = json.dumps({
    "results": [
        {"title": "Python.org — Welcome", "url": "https://www.python.org/",
         "content": "The official home of the Python programming language."},
        {"title": "Python (Wikipedia)", "url": "https://en.wikipedia.org/wiki/Python",
         "content": "Python is a high-level programming language."},
    ]
}).encode()


class _StaticHandler(BaseHTTPRequestHandler):
    body = b""
    ctype = "application/json"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", type(self).ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def main() -> int:
    section("maos.tools.files: файловые инструменты (перенос из agent_system)")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        tools = {t.name: t for t in files_tools.build(ws)}
        tools["write_file"].fn(path="a.txt", content="раз\nдва\nтри\n")
        check("файл создан", (ws.root / "a.txt").exists())
        check("чтение возвращает содержимое",
             "два" in tools["read_file"].fn(path="a.txt"))
        tools["edit_file"].fn(path="a.txt", old_text="два", new_text="ДВА")
        check("правка применена", "ДВА" in (ws.root / "a.txt").read_text())
        try:
            tools["read_file"].fn(path="../outside.txt")
            check("выход за пределы workspace отклонён", False)
        except ToolError:
            check("выход за пределы workspace отклонён", True)
        check("список файлов видит a.txt", "a.txt" in tools["list_files"].fn())
        check("поиск текста находит", "a.txt" in tools["search_text"].fn(query="ДВА"))

    section("maos.tools.web: поиск через SearXNG-бэкенд (реальный HTTP)")

    class H(_StaticHandler):
        body = SEARXNG_JSON

    srv, port = _start(H)
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Workspace(td)
            cfg = web_tools.WebConfig(
                backend="searxng", search_base_url=f"http://127.0.0.1:{port}",
                rate_limit=0)
            web = {t.name: t for t in web_tools.build(ws, cfg)}
            out = web["web_search"].fn(query="python")
            check("результат поиска содержит заголовок", "Python.org" in out, out)
            check("второй результат тоже виден", "Wikipedia" in out, out)
    finally:
        srv.shutdown()

    section("maos.tools.web: SSRF-защита реально отклоняет приватный адрес")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = web_tools.WebConfig(rate_limit=0, allow_local=False)
        web = {t.name: t for t in web_tools.build(ws, cfg)}
        try:
            web["web_fetch"].fn(url="http://127.0.0.1:1/secret")
            check("доступ к 127.0.0.1 отклонён", False)
        except ToolError:
            check("доступ к 127.0.0.1 отклонён", True)

    section("maos.tools.toolbox: parse_tools_field")
    check("разбирает через запятую", parse_tools_field("files, web") == ["files", "web"])
    check("пустая строка -> пустой список", parse_tools_field("") == [])
    check("KNOWN_TOOLS содержит встроенные и внешние навыки",
         set(KNOWN_TOOLS) == {"files", "web", "office", "rag", "mcp", "messaging"})

    section("maos.tools.toolbox: build_toolbox без Store — files/web/office")
    with tempfile.TemporaryDirectory() as wsroot:
        from maos.config import Config
        cfg = Config(workspace_root=wsroot)
        agent_row = {"slug": "coder", "tools": "files,web"}
        built = build_toolbox(cfg, agent_row)
        names = {t.name for t in built}
        check("инструменты files присутствуют",
             {"read_file", "write_file", "edit_file"} <= names)
        check("инструменты web присутствуют",
             {"web_search", "web_fetch"} <= names)
        check("рабочая папка агента реально создана",
             (Path(wsroot) / "coder").exists())

        agent_row2 = {"slug": "writer2", "tools": "office"}
        built2 = build_toolbox(cfg, agent_row2)
        check("office-инструменты присутствуют",
             any("docx" in t.name or "xlsx" in t.name or "pptx" in t.name
                for t in built2), [t.name for t in built2])

        agent_row3 = {"slug": "empty", "tools": ""}
        check("агент без tools -> пустой список инструментов",
             build_toolbox(cfg, agent_row3) == [])

    section("maos.tools.toolbox: неизвестный навык -> ToolboxError")
    with tempfile.TemporaryDirectory() as wsroot:
        from maos.config import Config
        cfg = Config(workspace_root=wsroot)
        try:
            build_toolbox(cfg, {"slug": "x", "tools": "files,ghost_tool"})
            check("неизвестный навык кидает ToolboxError", False)
        except ToolboxError as exc:
            check("неизвестный навык кидает ToolboxError", True)
            check("сообщение называет неизвестный навык", "ghost_tool" in str(exc))

    section("maos.tools.toolbox: rag требует Store и embedder")
    with tempfile.TemporaryDirectory() as wsroot:
        from maos.config import Config
        cfg = Config(workspace_root=wsroot)
        try:
            build_toolbox(cfg, {"slug": "x", "tools": "rag"})
            check("rag без Store/embedder кидает ToolboxError", False)
        except ToolboxError:
            check("rag без Store/embedder кидает ToolboxError", True)

    section("maos.tools.toolbox: изоляция рабочих папок между агентами")
    with tempfile.TemporaryDirectory() as wsroot:
        from maos.config import Config
        cfg = Config(workspace_root=wsroot)
        ws_a = agent_workspace(cfg, "agent_a")
        ws_b = agent_workspace(cfg, "agent_b")
        ws_a.resolve("secret.txt").write_text("A's secret", encoding="utf-8")
        check("папка agent_a создана", ws_a.root.exists())
        check("папка agent_b НЕ видит файл agent_a",
             not (ws_b.root / "secret.txt").exists())
        check("папки физически разные", ws_a.root != ws_b.root)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
