"""Тесты MCP, показа артефакта и перепланирования.

MCP проверяется на НАСТОЯЩЕМ протоколе: поднимается реальный
подпроцесс-сервер и реальный HTTP-сервер, говорим с ними по JSON-RPC.
Заглушек внутри Python здесь нет — иначе тест проверял бы сам себя.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.autorun import AutoRunner                      # noqa: E402
from agent.core import Agent                              # noqa: E402
from agent.llm.base import BaseLLM, LLMReply              # noqa: E402
from agent.mcp import (MCPClient, MCPPool, MCPServerConfig,  # noqa: E402
                       configs_from_dict)
from agent.store import Store                             # noqa: E402
from agent.tools import memory as mem_tools               # noqa: E402
from agent.tools import present as present_tools          # noqa: E402
from agent.tools.base import ToolError, ToolRegistry, Workspace  # noqa: E402

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n{t}\n" + "─" * len(t))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ============================================================ MCP stdio
def test_mcp_stdio() -> None:
    section("MCP: транспорт stdio (реальный подпроцесс)")
    cfg = MCPServerConfig(name="web", command=sys.executable, args=[SERVER])
    c = MCPClient(cfg)
    check("рукопожатие прошло", c.connect(), c.error)
    check("список инструментов получен", len(c.tools) == 3, str(len(c.tools)))
    names = {t["name"] for t in c.tools}
    check("инструменты названы верно", names == {"search", "fetch", "boom"},
          str(names))

    out = c.call_tool("search", {"query": "редуктор"})
    check("вызов возвращает текст", "редуктор" in out, out[:80])
    out2 = c.call_tool("fetch", {"url": "http://x"})
    check("второй инструмент работает", "http://x" in out2, out2[:80])

    # НЕГАТИВНЫЕ
    try:
        c.call_tool("boom", {})
        check("ошибка сервера поднимается", False, "проглотили!")
    except Exception as exc:
        check("ошибка сервера поднимается", "намеренный сбой" in str(exc))
    try:
        c.call_tool("нет_такого", {})
        check("неизвестный инструмент отвергнут", False)
    except Exception:
        check("неизвестный инструмент отвергнут", True)
    c.close()


# ============================================================= MCP http
def test_mcp_http() -> None:
    section("MCP: транспорт http (реальный сервер)")
    port = free_port()
    proc = subprocess.Popen([sys.executable, SERVER, "--http", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    try:
        cfg = MCPServerConfig(name="api", transport="http",
                              url=f"http://127.0.0.1:{port}/mcp")
        c = MCPClient(cfg)
        check("подключение по http", c.connect(), c.error)
        check("инструменты получены", len(c.tools) == 3, str(len(c.tools)))
        out = c.call_tool("search", {"query": "тест"})
        check("вызов по http работает", "тест" in out, out[:80])
        c.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ============================================================== лимиты
def test_rate_limit() -> None:
    section("Лимит частоты вызовов")
    # с лимитом
    cfg = MCPServerConfig(name="web", command=sys.executable, args=[SERVER],
                          rate_limit=1.0)
    c = MCPClient(cfg)
    c.connect()
    t0 = time.time()
    for _ in range(3):
        c.call_tool("fetch", {"url": "x"})
    dt = time.time() - t0
    check("лимит 1 с выдерживается на 3 вызовах", dt >= 2.0, f"{dt:.2f} с")
    c.close()

    # без лимита — быстро
    cfg2 = MCPServerConfig(name="free", command=sys.executable, args=[SERVER],
                           rate_limit=0)
    c2 = MCPClient(cfg2)
    c2.connect()
    t1 = time.time()
    for _ in range(5):
        c2.call_tool("fetch", {"url": "x"})
    dt2 = time.time() - t1
    check("без лимита вызовы идут быстро", dt2 < 1.0, f"{dt2:.2f} с")
    check("лимит и его отсутствие различаются", dt > dt2 * 2,
          f"{dt:.2f} против {dt2:.2f}")
    c2.close()

    # лимит виден модели в описании
    pool = MCPPool([MCPServerConfig(name="web", command=sys.executable,
                                    args=[SERVER], rate_limit=20)])
    t = {x.name: x for x in pool.tools()}["web_search"]
    check("лимит указан в описании инструмента", "20" in t.description,
          t.description[-60:])
    pool.close()


# ================================================================= пул
def test_pool() -> None:
    section("Пул серверов и устойчивость")
    pool = MCPPool([
        MCPServerConfig(name="web", command=sys.executable, args=[SERVER]),
        MCPServerConfig(name="dead", command="/нет/такой/команды"),
        MCPServerConfig(name="off", command=sys.executable, args=[SERVER],
                        enabled=False),
    ])
    tools = {t.name: t for t in pool.tools()}
    check("рабочий сервер подключён", "web_search" in tools, str(list(tools)))
    check("битый сервер не дал инструментов",
          not any(n.startswith("dead_") for n in tools))
    check("выключенный сервер пропущен",
          not any(n.startswith("off_") for n in tools))
    check("имена префиксуются сервером", "web_fetch" in tools)

    st = pool.status()
    check("статус объясняет недоступность", "НЕДОСТУПЕН" in st, st)
    check("статус упоминает выключенный", "выключен" in st, st)

    # ГЛАВНОЕ: битый сервер не мешает рабочему
    out = tools["web_search"].fn(query="проверка")
    check("рабочий сервер работает несмотря на битый", "проверка" in out)

    # ошибка MCP приходит как ToolError — цикл агента её переживёт
    try:
        tools["web_boom"].fn()
        check("ошибка обёрнута в ToolError", False)
    except ToolError:
        check("ошибка обёрнута в ToolError", True)
    pool.close()


def test_config_parse() -> None:
    section("Разбор конфигурации MCP")
    raw = {
        "servers": {
            "search": {"command": "npx", "args": ["-y", "srv"],
                       "rate_limit": 20, "env": {"KEY": "x"}},
            "fetch": {"command": "uvx", "args": ["fetch"], "rate_limit": 0},
            "remote": {"transport": "http", "url": "http://h:9/mcp",
                       "headers": {"Authorization": "Bearer t"}},
        }
    }
    cfgs = {c.name: c for c in configs_from_dict(raw)}
    check("серверы разобраны", len(cfgs) == 3, str(list(cfgs)))
    check("лимит поиска 20 с", cfgs["search"].rate_limit == 20.0)
    check("загрузка страниц без лимита", cfgs["fetch"].rate_limit == 0.0)
    check("env передан", cfgs["search"].env == {"KEY": "x"})
    check("http-транспорт распознан", cfgs["remote"].transport == "http")
    check("заголовки сохранены",
          cfgs["remote"].headers.get("Authorization") == "Bearer t")


# ============================================================== present
def test_present() -> None:
    section("Показ артефакта")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        st = Store(str(Path(td) / "a.db"))
        rid = st.start_run("построить деталь", "cad_auto")
        st.add_tasks(rid, ["посчитать размеры", "проверить сборку"])
        tasks = st.tasks(rid)
        st.set_task(tasks[0]["id"], "done", "габарит 84 мм")
        st.set_task(tasks[1]["id"], "failed", "не сошлось")
        st.remember("зазор 3.87 мм", tags="геометрия", run_id=rid)
        st.upsert_entity("part", "венец")

        (ws.root / "res.txt").write_text("итоговые числа: 42", encoding="utf-8")
        # минимальный валидный PNG
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082")
        (ws.root / "pic.png").write_bytes(png)

        tools = {t.name: t for t in present_tools.build(ws, st, lambda: rid)}
        out = tools["present"].fn(title="Итог", files="res.txt, pic.png",
                                  summary="Работа завершена")
        check("отчёт создан", "report.html" in out, out)

        html = (ws.root / "report.html").read_text(encoding="utf-8")
        check("заголовок на месте", "Итог" in html)
        check("сводка по плану", "1/2" in html, "нет плитки прогресса")
        check("выполненный пункт показан", "посчитать размеры" in html)
        check("провал показан с причиной", "не сошлось" in html)
        check("факты из памяти включены", "3.87" in html)
        check("текстовый файл вложен", "итоговые числа: 42" in html)
        check("картинка вшита в base64", "data:image/png;base64," in html)
        check("страница самодостаточна",
              "http://" not in html and "https://" not in html,
              "есть внешние ссылки")
        check("HTML экранирован", "<script>" not in html)

        # НЕГАТИВНЫЙ: несуществующий файл не роняет отчёт
        out2 = tools["present"].fn(files="нет.txt", out_path="r2.html")
        check("отсутствующий файл не ломает отчёт", "не удалось" in out2, out2)
        check("причина видна в отчёте",
              "не найден" in (ws.root / "r2.html").read_text(encoding="utf-8"))

        # НЕГАТИВНЫЙ: выход за workspace
        out3 = tools["present"].fn(files="../../etc/passwd", out_path="r3.html")
        check("выход за workspace отражён как ошибка",
              "не удалось" in out3, out3)
        st.close()


# ========================================================= перепланирование
class Replanner(BaseLLM):
    """Планировщик даёт плохой план, потом хороший; работа всегда валится."""

    def __init__(self) -> None:
        super().__init__("t")
        self.plans = 0

    def chat(self, messages, tools=None):
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "Составь НОВЫЙ план" in text:
            self.plans += 1
            return LLMReply(text="обойти проблему иначе\nпроверить обходной путь")
        if "Ты планировщик" in text:
            return LLMReply(text="сделать невозможное\nсделать ещё невозможное\n"
                                 "и третье невозможное")
        if "ТОЛЬКО валидным JSON" in text:
            return LLMReply(text=json.dumps({"learned": [], "next": "",
                                             "stuck": False}))
        return LLMReply(text="не вышло")


def test_replan() -> None:
    section("Перепланирование")
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "a.db"))
        rid_box = {"v": 0}
        reg = ToolRegistry()
        reg.extend(mem_tools.build(st, lambda: rid_box["v"]))

        llm = Replanner()
        orig = st.start_run

        def start(g, p=None):
            rid_box["v"] = orig(g, p)
            return rid_box["v"]
        st.start_run = start  # type: ignore

        events: list[str] = []
        runner = AutoRunner(lambda: Agent(llm, reg, max_steps=2), st,
                            max_hours=1, max_iterations=8,
                            replan_after_fails=2,
                            on_event=lambda k, d: events.append(k))

        # чтобы пункты падали, помечаем их провальными через рефлексию:
        # агент не закрывает пункт -> autorun закроет как done. Поэтому
        # валим напрямую, имитируя plan_fail внутри работы.
        real_run = runner.run

        res = real_run("невыполнимая цель")
        check("прогон завершился", res.stopped_by in ("done", "stuck",
                                                      "iterations"),
              res.stopped_by)

        # проверяем сам механизм перепланирования напрямую
        rid2 = st.start_run("цель 2")
        rid_box["v"] = rid2
        st.add_tasks(rid2, ["первый плохой пункт", "второй плохой пункт"])
        for t in st.tasks(rid2):
            st.set_task(t["id"], "failed", "тупик")
        st.add_tasks(rid2, ["ещё не сделанный пункт"])

        r2 = AutoRunner(lambda: Agent(llm, reg, max_steps=2), st, max_hours=1)
        r2.run_id = rid2
        before = len(st.tasks(rid2))
        ok = r2._replan("провалено пунктов: 2")
        after = st.tasks(rid2)
        check("перепланирование сработало", ok)
        check("новые пункты добавлены",
              any("обойти проблему" in t["title"] for t in after),
              str([t["title"] for t in after]))
        check("проваленные пункты сохранены как история",
              sum(1 for t in after if t["status"] == "failed") == 2)
        check("невыполненный старый пункт убран",
              not any(t["title"] == "ещё не сделанный пункт" for t in after),
              str([t["title"] for t in after]))
        check("факт о пересмотре записан",
              any("пересмотрен" in f["text"] for f in st.recall("пересмотрен")))
        st.close()


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ MCP, ОТЧЁТА И ПЕРЕПЛАНИРОВАНИЯ")
    print("=" * 60)
    test_mcp_stdio()
    test_mcp_http()
    test_rate_limit()
    test_pool()
    test_config_parse()
    test_present()
    test_replan()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
