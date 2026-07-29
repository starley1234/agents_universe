"""Тесты новых инструментов: вопрос человеку, git-снимки, запуск Python.

Как и везде: рядом с проверкой «работает» стоит проверка того, что
инструмент УМЕЕТ ОТКАЗАТЬ. Молчаливое «ок» — худший исход, поэтому
проверяем именно опасные случаи: выдуманный ответ вместо вопроса,
закрытие незавершённого пункта, откат не того файла, побег из папки.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.store import Store                          # noqa: E402
from agent.tools import ask as ask_tools               # noqa: E402
from agent.tools import memory as memory_tools         # noqa: E402
from agent.tools import python as py_tools             # noqa: E402
from agent.tools import vcs as vcs_tools               # noqa: E402
from agent.tools.base import ToolError, Workspace      # noqa: E402

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


def _store(td: str) -> tuple[Store, int]:
    st = Store(Path(td) / "a.db")
    rid = st.start_run("тест")
    return st, rid


# ══════════════════════════ вопрос человеку ══════════════════════════
def test_ask_with_human() -> None:
    section("ask_user: человек рядом")
    with tempfile.TemporaryDirectory() as td:
        st, rid = _store(td)
        seen: list[tuple[str, list[str]]] = []

        def human(q: str, opts: list[str]) -> str:
            seen.append((q, opts))
            return "вариант Б"

        t = {x.name: x for x in ask_tools.build(st, lambda: rid, human)}
        out = t["ask_user"].fn(question="Какой формат отчёта?",
                               options="PDF|вариант Б")
        check("ответ человека возвращён агенту", "вариант Б" in out, out)
        check("вопрос дошёл до человека", seen and "формат" in seen[0][0])
        check("варианты разобраны по |", seen[0][1] == ["PDF", "вариант Б"],
              str(seen[0][1]))
        check("ответ сохранён в память",
              any("вариант Б" in f["text"] for f in st.recall("формат")))

        # молчание — не согласие
        t2 = {x.name: x for x in ask_tools.build(st, lambda: rid,
                                                 lambda q, o: "")}
        out2 = t2["ask_user"].fn(question="Продолжать?")
        check("молчание не выдаётся за согласие",
              "умолчание" in out2 and "Ответ человека:" not in out2, out2)

        # сломанный ввод не роняет агента
        def boom(q: str, o: list[str]) -> str:
            raise RuntimeError("нет терминала")

        t3 = {x.name: x for x in ask_tools.build(st, lambda: rid, boom)}
        check("сбой ввода не роняет агента",
              "не удалось" in t3["ask_user"].fn(question="что?"))

        try:
            t["ask_user"].fn(question="   ")
            check("пустой вопрос отвергнут", False, "принят")
        except ToolError:
            check("пустой вопрос отвергнут", True)

        # лимит: инструмент не должен превращаться в анкету
        t4 = {x.name: x for x in ask_tools.build(st, lambda: rid,
                                                 lambda q, o: "да")}
        res = [t4["ask_user"].fn(question=f"вопрос {i}")
               for i in range(ask_tools.ASK_LIMIT + 1)]
        check(f"после {ask_tools.ASK_LIMIT} вопросов — отказ",
              "Лимит вопросов" in res[-1], res[-1][:80])
        check("до лимита вопросы проходят",
              all("Ответ человека" in r for r in res[:-1]))
        st.close()


def test_ask_without_human() -> None:
    section("ask_user: человека нет (автономный прогон)")
    with tempfile.TemporaryDirectory() as td:
        st, rid = _store(td)
        ids = st.add_tasks(rid, ["сделать чертёж", "собрать отчёт"])
        st.set_task(ids[0], "doing")

        t = {x.name: x for x in ask_tools.build(st, lambda: rid, None)}
        out = t["ask_user"].fn(question="Какой допуск ставить?")

        check("ответ не выдуман", "Ответ человека" not in out, out)
        check("сказано, что ответить некому", "некому" in out, out)

        task = [x for x in st.tasks(rid) if x["id"] == ids[0]][0]
        check("пункт помечен blocked", task["status"] == "blocked",
              task["status"])
        check("текст вопроса сохранён в пункте",
              "допуск" in (task["result"] or ""), task["result"] or "")
        check("вопрос попал в память",
              any("допуск" in f["text"] for f in st.recall("допуск")))

        # ДИВЕРСИЯ смысла: заблокированный пункт нельзя закрыть как сделанный
        mt = {x.name: x for x in memory_tools.build(st, lambda: rid)}
        try:
            mt["plan_done"].fn(task_id=ids[0], result="как-то сделал")
            check("plan_done не закрывает заблокированный пункт", False,
                  "закрыл!")
        except ToolError as exc:
            check("plan_done не закрывает заблокированный пункт",
                  "ждёт ответа" in str(exc), str(exc))

        # следующий пункт берётся в работу, прогон не встаёт
        nxt = st.next_task(rid)
        check("работа продолжается со следующего пункта",
              nxt is not None and nxt["id"] == ids[1],
              str(nxt and nxt["id"]))
        check("заблокированный виден в плане",
              "[?]" in mt["plan_show"].fn())
        st.close()


def test_ask_in_summary() -> None:
    section("ask_user: вопрос виден в итоге прогона")
    from agent.autorun import AutoRunner
    from agent.core import Agent, Result
    from agent.llm.base import BaseLLM

    class Mute(BaseLLM):
        def _chat_once(self, messages, tools=None):  # pragma: no cover
            raise AssertionError("модель не должна вызываться")

    with tempfile.TemporaryDirectory() as td:
        st, _ = _store(td)

        # агент-пустышка: ничего не делает, план ведём вручную
        def factory() -> Agent:
            a = Agent.__new__(Agent)
            a.llm = Mute("t")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("ок", [], "done", [])
            return a

        # max_iterations=1: одна попытка взять пункт. Открытых нет —
        # остался только заблокированный, и это не «выполнено».
        r = AutoRunner(factory, st, max_hours=0.01, max_iterations=1)
        r.run_id = st.start_run("цель")
        ids = st.add_tasks(r.run_id, ["пункт с вопросом"])
        st.set_task(ids[0], "blocked", "ВОПРОС: какой материал?")
        res = r.run("цель", resume=r.run_id)

        check("итог помечен blocked, а не done", res.stopped_by == "blocked",
              res.stopped_by)
        check("вопрос напечатан в итоге",
              "ЖДУТ ОТВЕТА ЧЕЛОВЕКА" in res.summary and
              "какой материал" in res.summary, res.summary[-200:])
        st.close()


# ═══════════════════════ снимки рабочей папки ════════════════════════
def test_vcs_basic() -> None:
    section("Снимки: сохранение и различия")
    if not vcs_tools.git_available():
        check("git доступен", False, "git не установлен — тесты пропущены")
        return
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        t = {x.name: x for x in vcs_tools.build(ws)}
        f = ws.root / "main.py"
        f.write_text("print('раз')\n", encoding="utf-8")

        out = t["snapshot"].fn(message="начало")
        check("первый снимок сделан", out.startswith("Снимок"), out)
        check("репозиторий отдельный, в .agent-git",
              (ws.root / vcs_tools.GIT_DIR).is_dir() and
              not (ws.root / ".git").exists())

        check("без изменений снимок не плодится",
              "Изменений нет" in t["snapshot"].fn(message="пусто"))

        f.write_text("print('два')\n", encoding="utf-8")
        d = t["changes"].fn()
        check("изменение файла видно", "main.py" in d, d[:120])
        check("в различиях видна новая строка", "два" in d, d[:200])

        t["snapshot"].fn(message="правка")
        log = t["snapshots"].fn()
        check("снимки перечислены", log.count("\n") >= 1, log)
        check("описание снимка сохранено", "правка" in log, log)


def test_vcs_revert() -> None:
    section("Снимки: откат возвращает состояние")
    if not vcs_tools.git_available():
        return
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        t = {x.name: x for x in vcs_tools.build(ws)}
        good = ws.root / "code.py"
        good.write_text("ЦЕЛОЕ\n", encoding="utf-8")
        keep = ws.root / "данные.txt"
        keep.write_text("важное\n", encoding="utf-8")
        t["snapshot"].fn(message="рабочее состояние")
        head = vcs_tools.Repo(ws.root).head()

        # агент всё портит: правит, удаляет, мусорит
        good.write_text("СЛОМАНО\n", encoding="utf-8")
        keep.unlink()
        (ws.root / "мусор.tmp").write_text("х", encoding="utf-8")
        t["snapshot"].fn(message="после поломки")

        out = t["revert"].fn(to=head)
        check("откат сообщил о возврате", "возвращена" in out, out)
        check("испорченный файл восстановлен",
              good.read_text(encoding="utf-8") == "ЦЕЛОЕ\n",
              good.read_text(encoding="utf-8"))
        check("удалённый файл вернулся", keep.exists() and
              keep.read_text(encoding="utf-8") == "важное\n")
        check("созданный после снимка мусор убран",
              not (ws.root / "мусор.tmp").exists())

        # откат обратим: состояние до отката названо и лежит в истории
        back = out.split("снимке ")[1].split()[0]
        check("состояние до отката названо в ответе", len(back) >= 6, out)
        t["revert"].fn(to=back)
        check("откат отменяется обратным откатом",
              good.read_text(encoding="utf-8") == "СЛОМАНО\n" and
              (ws.root / "мусор.tmp").exists(),
              good.read_text(encoding="utf-8"))
        t["revert"].fn(to=head)          # возвращаемся к рабочему состоянию

        # Главный страх: откат уничтожает работу, которую не успели
        # сохранить снимком. Она обязана уцелеть в снимке «перед откатом».
        (ws.root / "черновик.txt").write_text("несохранённое\n",
                                              encoding="utf-8")
        good.write_text("НЕСОХРАНЁННАЯ ПРАВКА\n", encoding="utf-8")
        out2 = t["revert"].fn(to=head)   # снимка этой работы никто не делал
        check("откат вернул состояние снимка",
              good.read_text(encoding="utf-8") == "ЦЕЛОЕ\n" and
              not (ws.root / "черновик.txt").exists())
        back2 = out2.split("снимке ")[1].split()[0]
        t["revert"].fn(to=back2)
        check("несохранённая работа не потеряна при откате",
              good.read_text(encoding="utf-8") == "НЕСОХРАНЁННАЯ ПРАВКА\n" and
              (ws.root / "черновик.txt").exists(),
              good.read_text(encoding="utf-8"))
        t["revert"].fn(to=head)

        # ДИВЕРСИЯ смысла: несуществующий снимок не должен «получиться»
        try:
            t["revert"].fn(to="0000000")
            check("откат к несуществующему снимку отвергнут", False, "прошёл")
        except ToolError as exc:
            check("откат к несуществующему снимку отвергнут",
                  "нет" in str(exc), str(exc))
        check("после отказа файлы целы",
              good.read_text(encoding="utf-8") == "ЦЕЛОЕ\n")

        # откат без снимков — честная ошибка, а не тишина
        with tempfile.TemporaryDirectory() as td2:
            t2 = {x.name: x for x in vcs_tools.build(Workspace(td2))}
            try:
                t2["revert"].fn()
                check("откат без снимков отвергнут", False, "прошёл")
            except ToolError:
                check("откат без снимков отвергнут", True)


def test_vcs_auto_snapshot() -> None:
    section("Снимки: автоматически перед каждым шагом агента")
    if not vcs_tools.git_available():
        return
    from agent.build import build_agent
    from agent.config import Config
    from agent.llm.base import BaseLLM, LLMReply, ToolCall

    class Wrecker(BaseLLM):
        """Модель, которая портит файл, а потом отвечает текстом."""

        def __init__(self) -> None:
            super().__init__("t")
            self.n = 0

        def _chat_once(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return LLMReply(tool_calls=[ToolCall(
                    "1", "write_file",
                    {"path": "code.py", "content": "СЛОМАНО\n"})])
            return LLMReply(text="готово")

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "code.py").write_text("ЦЕЛОЕ\n", encoding="utf-8")
        cfg = Config(provider="ollama", model="m", workspace=td,
                     skills=["files", "vcs"])
        agent = build_agent(cfg)
        agent.llm = Wrecker()
        agent.run("испорти файл")

        check("файл действительно испорчен",
              (Path(td) / "code.py").read_text(encoding="utf-8") == "СЛОМАНО\n")

        t = {x.name: x for x in vcs_tools.build(Workspace(td))}
        log = t["snapshots"].fn(limit=20)
        check("снимок сделан без просьбы агента",
              "исходное состояние" in log, log)

        # и главное: исходное состояние восстановимо
        repo = vcs_tools.Repo(Path(td).resolve())
        first = repo._run("log", "--format=%h %s").stdout.strip().splitlines()
        base = [ln.split()[0] for ln in first
                if "исходное состояние" in ln][0]
        t["revert"].fn(to=base)
        check("исходный файл восстановлен из автоснимка",
              (Path(td) / "code.py").read_text(encoding="utf-8") == "ЦЕЛОЕ\n",
              (Path(td) / "code.py").read_text(encoding="utf-8"))

        # выключение автоснимка должно работать
        with tempfile.TemporaryDirectory() as td2:
            cfg2 = Config(provider="ollama", model="m", workspace=td2,
                          skills=["files", "vcs"])
            cfg2.vcs_auto = False
            a2 = build_agent(cfg2)
            check("vcs_auto=false отключает автоснимок",
                  a2.before_step is None)


# ═════════════════════════ запуск Python ═════════════════════════════
def test_python_run() -> None:
    section("run_python: счёт без Docker")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        t = {x.name: x for x in py_tools.build(ws, timeout=10, memory_mb=256)}

        check("арифметика считается",
              t["run_python"].fn(code="print(6*7)").strip() == "42")
        check("многострочный код работает",
              "3" in t["run_python"].fn(
                  code="s=0\nfor i in (1,2):\n    s+=i\nprint(s)"))
        check("пустой вывод назван пустым, а не выдан за успех",
              "вывод пуст" in t["run_python"].fn(code="x=1"))

        # ошибка не роняет агента и объясняется
        err = t["run_python"].fn(code="print('до')\nprint(1/0)")
        check("ошибка возвращена текстом", "ZeroDivisionError" in err, err)
        check("вывод до ошибки сохранён", "до" in err, err)
        check("номер строки ошибки верный (2, а не смещённый)",
              "line 2" in err, err)

        try:
            t["run_python"].fn(code="  ")
            check("пустой код отвергнут", False, "принят")
        except ToolError:
            check("пустой код отвергнут", True)


def test_python_limits() -> None:
    section("run_python: пределы времени и памяти")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        t = {x.name: x for x in py_tools.build(ws, timeout=2, memory_mb=256)}

        try:
            t["run_python"].fn(code="while True:\n    pass")
            check("зависший код прерван", False, "вернулся сам")
        except ToolError as exc:
            check("зависший код прерван", "не уложился" in str(exc), str(exc))

        out = t["run_python"].fn(code="a=bytearray(900*1024*1024)\nprint('ок')")
        check("предел памяти сработал",
              "MemoryError" in out and "ок" not in out, out[:200])
        check("превышение памяти объяснено", "предел памяти" in out, out[:200])

        # процесс агента при этом жив и считает дальше
        check("после аварий агент работоспособен",
              t["run_python"].fn(code="print(2+2)").strip() == "4")


def test_python_script() -> None:
    section("run_script: запуск файла")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        t = {x.name: x for x in py_tools.build(ws, timeout=10)}
        (ws.root / "main.py").write_text(
            'import sys\n'
            'def greet(n):\n    return "привет, " + n\n'
            'if __name__ == "__main__":\n'
            '    print(greet(sys.argv[1] if len(sys.argv)>1 else "мир"))\n',
            encoding="utf-8")

        out = t["run_script"].fn(path="main.py")
        check("скрипт запущен как __main__", "привет, мир" in out, out)
        check("аргументы переданы",
              "привет, Иван" in t["run_script"].fn(path="main.py",
                                                   args="Иван"))
        # путь, «украшенный» markdown, — реальный случай из лога LM Studio
        check("markdown-путь принят",
              "привет" in t["run_script"].fn(
                  path="[main.py](http://main.py)"))

        try:
            t["run_script"].fn(path="нет.py")
            check("несуществующий скрипт отвергнут", False, "принят")
        except ToolError:
            check("несуществующий скрипт отвергнут", True)

        # выход за пределы рабочей папки закрыт
        try:
            t["run_script"].fn(path="../../etc/hostname")
            check("побег из рабочей папки закрыт", False, "прошёл")
        except ToolError:
            check("побег из рабочей папки закрыт", True)


def test_python_without_docker() -> None:
    section("run_python работает там, где run_command требует Docker")
    # Реальная жалоба: sandbox=docker без демона делал счёт недоступным.
    from agent.build import build_agent
    from agent.config import Config
    from agent.llm.base import BaseLLM, LLMReply, ToolCall

    class Calc(BaseLLM):
        def __init__(self) -> None:
            super().__init__("t")
            self.n = 0

        def _chat_once(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return LLMReply(tool_calls=[ToolCall(
                    "1", "run_python", {"code": "print(21*2)"})])
            last = [m for m in messages if m.get("role") == "tool"][-1]
            return LLMReply(text=last["content"])

    for mode in ("docker", "confirm", "auto", "off"):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(provider="ollama", model="m", workspace=td,
                         skills=["files", "python"])
            cfg.sandbox.mode = mode
            agent = build_agent(cfg)
            # ни confirm, ни docker-демон не участвуют
            agent.llm = Calc()
            res = agent.run("посчитай 21*2")
            check(f"режим песочницы {mode}: счёт прошёл", "42" in res.answer,
                  res.answer[:80])


# ═════════════════════════ бюджет денег ══════════════════════════════
def test_budget() -> None:
    section("Бюджет: прогон останавливается по деньгам")
    from agent.autorun import AutoRunner
    from agent.core import Agent, Result
    from agent.llm.base import BaseLLM, price_of

    # Считаем на реальной цене реальной модели, а не на выдуманной.
    model = "gpt-4o-mini"
    price = price_of(model)
    assert price, "цена модели не найдена — тест бессмысленен"
    per_iter = (200_000 * price[0] + 100_000 * price[1]) / 1e6

    class Spender(BaseLLM):
        billable = True

        def _chat_once(self, messages, tools=None):  # pragma: no cover
            raise AssertionError("модель не вызывается напрямую")

    def factory() -> Agent:
        a = Agent.__new__(Agent)
        a.llm = Spender(model)
        a.on_event = lambda k, d: None
        a.run = lambda task: Result("готово", [], "done", [],
                                    prompt_tokens=200_000,
                                    completion_tokens=100_000)
        return a

    # предел ровно на 2.5 итерации: третья не должна начаться
    limit = per_iter * 2.5

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "b.db")
        r = AutoRunner(factory, st, max_hours=1, max_iterations=10,
                       max_usd=limit)
        r.run_id = st.start_run("цель")
        st.add_tasks(r.run_id, [f"пункт {i}" for i in range(1, 11)])
        # рефлексия не нужна: она бы тратила ещё
        r._reflect = lambda task, summary: False
        res = r.run("цель", resume=r.run_id)

        check("прогон остановлен по бюджету", res.stopped_by == "budget",
              res.stopped_by)
        check("остановился на 3-й итерации, а не позже", res.iterations == 3,
              str(res.iterations))
        check("потрачено не больше предела с одной итерацией запаса",
              res.cost <= limit + per_iter, f"{res.cost:.4f} при {limit:.4f}")
        check("в итоге названа причина и сумма",
              "ОСТАНОВЛЕН ПО БЮДЖЕТУ" in res.summary and
              f"{limit:.2f}" in res.summary, res.summary[-200:])
        check("сказано, как продолжить",
              "--resume" in res.summary, res.summary[-200:])
        check("незакрытые пункты остались открытыми",
              sum(1 for t in st.tasks(r.run_id) if t["status"] == "open") >= 6)

        # Без предела тот же прогон доходит до конца — значит остановила
        # именно проверка бюджета, а не что-то другое.
        st2 = Store(Path(td) / "c.db")
        # запас по итерациям: иначе прогон упрётся в их лимит, а не в план
        r2 = AutoRunner(factory, st2, max_hours=1, max_iterations=12,
                        max_usd=0.0)
        r2.run_id = st2.start_run("цель")
        st2.add_tasks(r2.run_id, [f"пункт {i}" for i in range(1, 11)])
        r2._reflect = lambda task, summary: False
        res2 = r2.run("цель", resume=r2.run_id)
        check("без предела прогон доходит до конца",
              res2.stopped_by == "done" and res2.iterations == 10,
              f"{res2.stopped_by}/{res2.iterations}")
        st.close()
        st2.close()


# ══════════════════════ гигиена памяти ═══════════════════════════════
def test_memory_hygiene() -> None:
    section("Память: правка и удаление устаревшего")
    with tempfile.TemporaryDirectory() as td:
        st, rid = _store(td)
        t = {x.name: x for x in memory_tools.build(st, lambda: rid)}

        t["remember"].fn(text="Зазор щеки водила 0.5 мм")
        t["remember"].fn(text="Материал корпуса — сталь 40Х")

        found = t["recall"].fn(query="зазор")
        check("recall показывает номер факта", "#1" in found, found)

        # правка: главное — поиск идёт по НОВОМУ тексту, а не по старому
        out = t["revise"].fn(fact_id=1, text="Зазор щеки водила 0.8 мм")
        check("правка показывает было/стало",
              "0.5" in out and "0.8" in out, out)
        check("новое значение находится",
              "0.8" in t["recall"].fn(query="зазор щеки"))
        check("старое значение больше НЕ находится",
              "0.5" not in t["recall"].fn(query="зазор щеки"),
              t["recall"].fn(query="зазор щеки"))
        check("в памяти по-прежнему 2 факта, а не 3",
              st.fact_count() == 2, str(st.fact_count()))

        # Индекс поиска обязан обновиться вместе с текстом. Правка со
        # СМЕНОЙ СЛОВ: старые слова не должны находить факт, новые —
        # должны. Иначе поиск живёт по устаревшему индексу.
        t["remember"].fn(text="Смазка узла — литол")
        wid = [r["id"] for r in st.recall("литол")][0]
        t["revise"].fn(fact_id=wid, text="Смазка узла — циатим")
        check("по новому слову факт находится",
              "циатим" in t["recall"].fn(query="циатим").lower(),
              t["recall"].fn(query="циатим"))
        check("по заменённому слову факт НЕ находится",
              "циатим" not in t["recall"].fn(query="литол").lower(),
              t["recall"].fn(query="литол"))
        t["forget"].fn(fact_id=wid)

        # уверенность правится отдельно от текста
        t["revise"].fn(fact_id=1, confidence=0.3)
        check("уверенность изменена",
              abs((st.get_fact(1) or {})["confidence"] - 0.3) < 1e-9)
        check("текст при правке уверенности не потерян",
              "0.8" in (st.get_fact(1) or {})["text"])

        try:
            t["revise"].fn(fact_id=999, text="х")
            check("правка несуществующего факта отвергнута", False, "прошла")
        except ToolError:
            check("правка несуществующего факта отвергнута", True)

        # удаление по номеру
        out = t["forget"].fn(fact_id=2)
        check("удаление называет удалённое", "сталь 40Х" in out, out)
        check("удалённое не находится поиском",
              "40Х" not in t["recall"].fn(query="материал корпуса"))
        check("счётчик уменьшился", st.fact_count() == 1)

        # удаление по запросу
        t["remember"].fn(text="Черновая версия отчёта лежит в tmp/1")
        t["remember"].fn(text="Черновая версия отчёта лежит в tmp/2")
        out = t["forget"].fn(query="черновая версия отчёта")
        check("по запросу удалены оба", "Удалено фактов: 2" in out, out)
        check("после удаления по запросу остался только нужный",
              st.fact_count() == 1, str(st.fact_count()))

        # ГЛАВНАЯ защита: пустой запрос не должен стирать всё
        before = st.fact_count()
        try:
            t["forget"].fn()
            check("пустой запрос не стирает память", False, "стёр!")
        except ToolError as exc:
            check("пустой запрос не стирает память",
                  "нельзя" in str(exc), str(exc))
        check("память цела после отказа", st.fact_count() == before)

        check("несуществующий номер — честный ответ, а не молчание",
              "Ничего не удалено" in t["forget"].fn(fact_id=777))
        st.close()


# ═══════════════════════ конфигурация ════════════════════════════════
def test_config_keys() -> None:
    section("Конфиг: комментарии разрешены, опечатки — нет")
    import json as _json
    from agent.config import Config

    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.json"
        good.write_text(_json.dumps({
            "_комментарий": "пояснение автора",
            "provider": "ollama", "model": "m",
            "skills": ["files", "python", "vcs", "ask"],
            "vcs_auto": False, "python_timeout": 30, "max_usd": 1.5,
        }, ensure_ascii=False), encoding="utf-8")
        cfg = Config.load(str(good))
        check("ключ-комментарий не мешает загрузке", cfg.model == "m")
        check("новые настройки читаются",
              cfg.vcs_auto is False and cfg.python_timeout == 30
              and abs(cfg.max_usd - 1.5) < 1e-9,
              f"{cfg.vcs_auto}/{cfg.python_timeout}/{cfg.max_usd}")

        bad = Path(td) / "bad.json"
        bad.write_text('{"provider":"ollama","modell":"x"}', encoding="utf-8")
        try:
            Config.load(str(bad))
            check("опечатка в ключе отвергнута", False, "принята молча")
        except ValueError as exc:
            check("опечатка в ключе отвергнута", "modell" in str(exc),
                  str(exc)[:80])
            check("подсказаны допустимые ключи", "Допустимы" in str(exc))

        # готовый пример из репозитория обязан грузиться
        ex = Path(__file__).resolve().parents[1] / "examples" / "config.safe.json"
        if ex.exists():
            c2 = Config.load(str(ex))
            check("пример config.safe.json рабочий",
                  "vcs" in c2.skills and "ask" in c2.skills, str(c2.skills))


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ИНСТРУМЕНТОВ: вопрос человеку, git, запуск Python")
    print("=" * 60)
    test_ask_with_human()
    test_ask_without_human()
    test_ask_in_summary()
    test_vcs_basic()
    test_vcs_revert()
    test_vcs_auto_snapshot()
    test_python_run()
    test_python_limits()
    test_python_script()
    test_python_without_docker()
    test_budget()
    test_memory_hygiene()
    test_config_keys()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
