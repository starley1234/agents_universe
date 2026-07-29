"""Тесты постоянного состояния и автономного режима.

Проверяется то, ради чего это всё: агент помнит между запусками,
ведёт план, строит онтологию, не зацикливается и умеет продолжить
прерванный прогон.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.autorun import AutoRunner                    # noqa: E402
from agent.core import Agent                            # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, ToolCall  # noqa: E402
from agent.store import Store                           # noqa: E402
from agent.tools import memory as mem_tools             # noqa: E402
from agent.tools.base import ToolRegistry               # noqa: E402

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


# ============================================================ хранилище
def test_store() -> None:
    section("Хранилище: память переживает перезапуск")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")

        s1 = Store(db)
        rid = s1.start_run("построить редуктор", "cad_auto")
        s1.remember("модуль зуба 1.25 мм", tags="геометрия", run_id=rid)
        s1.remember("зазор щека/венец 3.87 мм", tags="геометрия", run_id=rid)
        s1.remember("венец проваливается без буртика", tags="дефект", run_id=rid)
        s1.upsert_entity("part", "венец", {"z": 51})
        s1.upsert_entity("part", "водило")
        s1.link(("part", "водило"), "вращается_внутри", ("part", "венец"))
        s1.close()

        # ЭТО ГЛАВНОЕ: новый процесс, та же база
        s2 = Store(db)
        check("факты пережили перезапуск", s2.fact_count() == 3,
              str(s2.fact_count()))
        hits = s2.recall("зазор")
        check("полнотекстовый поиск находит",
              any("3.87" in h["text"] for h in hits), str(hits))
        check("поиск по метке", len(s2.recall("дефект")) >= 1)

        n = s2.neighbours("part", "венец")
        check("граф пережил перезапуск", len(n) == 1, str(n))
        check("направление связи верное",
              n[0]["dir"] == "in" and n[0]["name"] == "водило", str(n))

        # дубликаты не плодятся
        s2.remember("модуль зуба 1.25 мм")
        check("дубликат факта не создаётся", s2.fact_count() == 3,
              str(s2.fact_count()))
        s2.link(("part", "водило"), "вращается_внутри", ("part", "венец"))
        e, r = s2.graph_stats()
        check("дубликат связи не создаётся", r == 1, str(r))

        # свойства сущности дополняются, а не затираются
        s2.upsert_entity("part", "венец", {"material": "PETG"})
        row = s2.db.execute(
            "SELECT props FROM entity WHERE kind='part' AND name='венец'"
        ).fetchone()
        props = json.loads(row["props"])
        check("свойства объединяются",
              props.get("z") == 51 and props.get("material") == "PETG",
              str(props))
        s2.close()


# ================================================================ план
def test_recall_scale() -> None:
    section("Память: скорость на объёме")
    import random
    import time as _t
    words = ["редуктор", "зазор", "венец", "модуль", "коллизия", "печать"]
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "big.db"))
        rid = st.start_run("объём")
        rows = [(f"факт {i}: " + " ".join(random.choices(words, k=8)),
                 "t", "", 1.0, rid, 0) for i in range(30_000)]
        st.db.executemany(
            "INSERT OR IGNORE INTO fact(text,tags,source,confidence,run_id,created)"
            " VALUES(?,?,?,?,?,?)", rows)
        st.db.commit()
        check("30 тысяч фактов записаны", st.fact_count() == 30_000,
              str(st.fact_count()))

        times = []
        for q in words:
            t0 = _t.time()
            got = st.recall(q, 10)
            times.append((_t.time() - t0) * 1000)
            assert got, q
        worst = max(times)
        # Регрессия ORDER BY rank давала здесь десятки миллисекунд.
        # Порог с большим запасом: важно поймать возврат линейного роста.
        check("поиск быстрый на объёме", worst < 10.0, f"{worst:.2f} мс")
        check("поиск возвращает результат", len(st.recall("зазор", 5)) == 5)
        st.close()


def test_hybrid_recall() -> None:
    section("Гибридный поиск: слова в любом порядке")
    # Раньше искалась только точная фраза: запрос «зазор щеки» не находил
    # факт «зазор между щекой водила…», хотя оба слова есть. Агент из-за
    # этого повторно исследовал уже известное.
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "h.db"))
        rid = st.start_run("поиск")
        facts = [
            "венец проваливался внутрь корпуса без опорного буртика",
            "зазор между щекой водила и вершинами зубьев 3.87 мм",
            "лыска вала NEMA 17 имеет глубину 0.5 мм",
            "габарит редуктора Ø83.875 мм",
        ]
        for f in facts:
            st.remember(f, run_id=rid)

        cases = [
            ("буртик", "буртика"),                    # одно слово
            ("зазор щеки", "щекой"),                  # слова в другом порядке
            ("буртик венца корпуса", "буртика"),      # часть слов
            ("вершины зубьев водило", "вершинами"),   # другая форма
            ("лыск", "лыска"),                        # часть слова
            ("83.8", "83.875"),                       # число
        ]
        for q, must in cases:
            got = st.recall(q, 3)
            check(f"запрос {q!r} находит",
                  bool(got) and any(must in g["text"] for g in got),
                  str([g["text"][:35] for g in got]))

        # КАЧЕСТВО РАНЖИРОВАНИЯ: факт, где совпали ВСЕ слова запроса,
        # обязан идти выше факта с одним общим словом. Без этого
        # OR-ступень заваливает выдачу случайными совпадениями.
        st.remember("зазор в другом узле, ни при чём", run_id=rid)
        st.remember("щека отдельно упомянута тут", run_id=rid)
        ranked = st.recall("зазор щека", 5)
        check("совпадение по всем словам — первым",
              ranked and "щекой" in ranked[0]["text"],
              str([r["text"][:32] for r in ranked]))

        # мусорный запрос НЕ должен возвращать что попало
        junk = st.recall("абсолютно посторонняя тема квантовая физика", 3)
        check("несвязанный запрос не выдаёт мусор", len(junk) == 0,
              str([j["text"][:30] for j in junk]))

        # порядок: точное совпадение впереди
        st.remember("отдельный факт про зазор", run_id=rid)
        top = st.recall("зазор между щекой водила", 3)
        check("точная фраза идёт первой",
              top and "щекой" in top[0]["text"], str(top[0]["text"][:40]) if top else "-")
        st.close()

    # скорость не должна деградировать
    import random
    import time as _t
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "s.db"))
        rid = st.start_run("объём")
        w = ["редуктор", "зазор", "венец", "модуль", "печать"]
        rows = [(f"факт {i}: " + " ".join(random.choices(w, k=8)),
                 "t", "", 1.0, rid, 0) for i in range(30_000)]
        st.db.executemany(
            "INSERT OR IGNORE INTO fact(text,tags,source,confidence,run_id,created)"
            " VALUES(?,?,?,?,?,?)", rows)
        st.db.commit()
        t0 = _t.time()
        st.recall("зазор венец", 10)
        hit = (_t.time() - t0) * 1000
        t0 = _t.time()
        st.recall("такого точно нет", 10)
        miss = (_t.time() - t0) * 1000
        check("попадание быстрое", hit < 5, f"{hit:.2f} мс")
        check("промах не катастрофичен", miss < 60, f"{miss:.2f} мс")
        st.close()


def test_memory_across_runs() -> None:
    section("Память между ОТДЕЛЬНЫМИ запусками агента")
    # Реальная жалоба: агент в новом запуске говорил «да, помню»,
    # хотя memory не была подключена и он физически не помнил.
    from agent.build import build_agent
    from agent.config import Config
    from agent.llm.base import BaseLLM as _B
    from agent.llm.base import ToolCall as _TC

    cfg0 = Config.load(None)
    check("memory включена по умолчанию", "memory" in cfg0.skills,
          str(cfg0.skills))
    check("present включён по умолчанию", "present" in cfg0.skills)

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "shared.db")

        class Writer(_B):
            def __init__(self):
                super().__init__("t")
                self.n = 0

            def _chat_once(self, m, t=None):
                self.n += 1
                if self.n == 1:
                    return LLMReply(tool_calls=[_TC(
                        "1", "remember",
                        {"text": "габарит редуктора 84 мм", "tags": "факт"})])
                return LLMReply(text="записал")

        class Reader(_B):
            def __init__(self):
                super().__init__("t")
                self.n = 0

            def _chat_once(self, m, t=None):
                self.n += 1
                if self.n == 1:
                    return LLMReply(tool_calls=[_TC("1", "recall",
                                                    {"query": "габарит"})])
                got = [x for x in m if x.get("role") == "tool"][-1]["content"]
                return LLMReply(text=got)

        # ДВА независимых построения агента = два «запуска»
        c1 = Config.load(None, workspace=td)
        c1.db = db
        c1.sandbox.mode = "off"
        a1 = build_agent(c1)
        a1.llm = Writer()
        a1.run("запомни")

        c2 = Config.load(None, workspace=td)
        c2.db = db
        c2.sandbox.mode = "off"
        a2 = build_agent(c2)
        a2.llm = Reader()
        res = a2.run("что известно?")
        check("новый запуск находит записанное ранее",
              "84" in res.answer, res.answer[:80])

    # системный промпт обязан запрещать выдумывать память
    from agent.core import DEFAULT_SYSTEM
    check("промпт запрещает выдумывать память",
          "НЕ ВЫДУМЫВАЙ ПАМЯТЬ" in DEFAULT_SYSTEM)
    check("промпт советует recall", "recall" in DEFAULT_SYSTEM)


def test_plan() -> None:
    section("План: состояние между итерациями")
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "a.db"))
        rid = st.start_run("цель", None)
        st.add_tasks(rid, ["проверить А", "проверить Б", "проверить В"])

        t = st.next_task(rid)
        check("первый пункт выдан", t["title"] == "проверить А", str(t))
        st.set_task(t["id"], "done", "А сходится")

        t2 = st.next_task(rid)
        check("переход к следующему", t2["title"] == "проверить Б")
        st.set_task(t2["id"], "failed", "тупик")

        t3 = st.next_task(rid)
        check("провал не блокирует план", t3["title"] == "проверить В")
        st.set_task(t3["id"], "done")
        check("план исчерпан", st.next_task(rid) is None)

        rows = st.tasks(rid)
        check("статусы сохранены",
              [r["status"] for r in rows] == ["done", "failed", "done"],
              str([r["status"] for r in rows]))
        st.close()


# ========================================================== инструменты
def test_memory_tools() -> None:
    section("Инструменты памяти")
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "a.db"))
        rid = st.start_run("цель", None)
        tools = {t.name: t for t in mem_tools.build(st, lambda: rid)}

        tools["remember"].fn(text="вершина зуба 0.97 мм", tags="печать")
        check("remember пишет", st.fact_count() == 1)
        out = tools["recall"].fn(query="вершина")
        check("recall находит", "0.97" in out, out)
        check("recall честен о пустоте",
              "ничего нет" in tools["recall"].fn(query="щыъэ"))

        tools["note_entity"].fn(kind="part", name="солнце", props='{"z":17}')
        tools["link"].fn(subject_kind="part", subject="солнце",
                         predicate="в_зацеплении_с",
                         object_kind="part", object="сателлит")
        d = tools["describe"].fn(kind="part", name="солнце")
        check("describe показывает связь", "сателлит" in d, d)

        # НЕГАТИВНЫЙ: битый JSON отвергается
        from agent.tools.base import ToolError
        try:
            tools["note_entity"].fn(kind="x", name="y", props="{не json}")
            check("битый JSON отвергнут", False, "принял!")
        except ToolError:
            check("битый JSON отвергнут", True)

        tools["plan_add"].fn(items="пункт один\nпункт два")
        show = tools["plan_show"].fn()
        check("план показывается", "пункт один" in show, show)
        tid = st.tasks(rid)[0]["id"]
        tools["plan_done"].fn(task_id=tid, result="готово")
        check("пункт закрывается", "[x]" in tools["plan_show"].fn())
        st.close()


# ======================================================= автономный цикл
class Scripted(BaseLLM):
    """Модель по сценарию: словарь «фрагмент промпта -> ответы»."""

    def __init__(self, plan_items, work_replies, reflect):
        super().__init__("scripted")
        self.plan_items = plan_items
        self.work = list(work_replies)
        self.reflect = reflect
        self.seen_goal = 0
        self.work_prompts: list[str] = []

    def chat(self, messages, tools=None):
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "Ты планировщик" in text:
            return LLMReply(text="\n".join(self.plan_items))
        if "ТОЛЬКО валидным JSON" in text:
            return LLMReply(text=json.dumps(self.reflect, ensure_ascii=False))
        if "ЦЕЛЬ ПРОГОНА" in text:
            self.seen_goal += 1
            self.work_prompts.append(text)
        return self.work.pop(0) if self.work else LLMReply(text="сделано")


def test_autorun() -> None:
    section("Автономный цикл")
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "a.db"))
        reg = ToolRegistry()
        rid_box = {"v": 0}
        reg.extend(mem_tools.build(st, lambda: rid_box["v"]))

        llm = Scripted(
            plan_items=["изучить вход", "проверить выход"],
            work_replies=[LLMReply(text="изучил")] * 10,
            reflect={"learned": ["вход равен 42"], "next": "дальше",
                     "stuck": False})

        events = []
        runner = AutoRunner(lambda: Agent(llm, reg, max_steps=3), st,
                            max_hours=1, max_iterations=5,
                            on_event=lambda k, d: events.append(k))
        orig = st.start_run

        def start(g, p=None):
            rid_box["v"] = orig(g, p)
            return rid_box["v"]
        st.start_run = start  # type: ignore

        res = runner.run("исследовать систему", "autonomous")
        check("прогон завершён по плану", res.stopped_by == "done",
              res.stopped_by)
        check("итераций по числу пунктов", res.iterations == 2,
              str(res.iterations))
        check("план сохранён в базе", len(st.tasks(res.run_id)) == 2)
        check("все пункты закрыты",
              all(t["status"] in ("done", "failed")
                  for t in st.tasks(res.run_id)))
        check("рефлексия записала факт",
              any("42" in f["text"] for f in st.recall("42")),
              str(st.recall("")))
        check("цель подставлялась в каждую итерацию", llm.seen_goal >= 2,
              str(llm.seen_goal))
        # ГЛАВНОЕ для долгого прогона: цель реально ВНУТРИ промпта,
        # а не просто счётчик вызовов. Без этого агент через час
        # работает вслепую.
        check("текст цели присутствует в каждом рабочем промпте",
              all("исследовать систему" in p for p in llm.work_prompts),
              f"промптов {len(llm.work_prompts)}, "
              f"с целью {sum('исследовать систему' in p for p in llm.work_prompts)}")
        check("пункт плана присутствует в промпте",
              any("изучить вход" in p for p in llm.work_prompts))
        check("события пришли наблюдателю",
              "plan" in events and "iteration" in events and "finish" in events,
              str(set(events)))

        run = st.get_run(res.run_id)
        check("статистика прогона пишется", run["steps"] > 0, str(dict(run)))
        st.close()


def test_stuck_and_resume() -> None:
    section("Застой и возобновление")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")
        st = Store(db)
        reg = ToolRegistry()
        reg.extend(mem_tools.build(st, lambda: 1))

        # модель всё время сигналит о застое
        # пункты длиннее 8 символов — иначе планировщик их отфильтрует
        llm = Scripted(plan_items=["первый шаг", "второй шаг", "третий шаг",
                                   "четвёртый шаг", "пятый шаг"],
                       work_replies=[LLMReply(text="топчусь")] * 30,
                       reflect={"learned": [], "next": "", "stuck": True})
        runner = AutoRunner(lambda: Agent(llm, reg, max_steps=2), st,
                            max_hours=1, max_iterations=20)
        res = runner.run("зациклиться")
        check("застой останавливает прогон", res.stopped_by == "stuck",
              res.stopped_by)
        check("остановка быстрая, а не по лимиту", res.iterations <= 4,
              str(res.iterations))

        # --- возобновление после обрыва ---
        st2 = Store(db)
        rid = st2.start_run("длинная цель", "autonomous")
        st2.add_tasks(rid, ["шаг 1", "шаг 2", "шаг 3"])
        st2.set_task(st2.tasks(rid)[0]["id"], "done", "ок")
        st2.finish_run(rid, "stopped")
        st2.close()

        st3 = Store(db)          # новый процесс
        reg3 = ToolRegistry()
        reg3.extend(mem_tools.build(st3, lambda: rid))
        llm3 = Scripted(plan_items=[], work_replies=[LLMReply(text="x")] * 10,
                        reflect={"learned": [], "next": "", "stuck": False})
        r3 = AutoRunner(lambda: Agent(llm3, reg3, max_steps=2), st3,
                        max_hours=1, max_iterations=10)
        res3 = r3.run("", resume=rid)
        check("возобновление доделало остаток", res3.iterations == 2,
              str(res3.iterations))
        check("прогон закрыт как выполненный", res3.stopped_by == "done")
        check("выполненный ранее пункт не переделан",
              st3.tasks(rid)[0]["result"] == "ок")
        st3.close()


def test_repeat_detector() -> None:
    section("Детектор повторов")
    with tempfile.TemporaryDirectory() as td:
        st = Store(str(Path(td) / "a.db"))
        rid = st.start_run("цель")
        sig = AutoRunner._sig("read_file", {"path": "a.txt"})
        same = AutoRunner._sig("read_file", {"path": "a.txt"})
        other = AutoRunner._sig("read_file", {"path": "b.txt"})
        check("одинаковый вызов даёт одну подпись", sig == same)
        check("разные аргументы — разные подписи", sig != other)

        for i in range(4):
            st.log_event(rid, i, "tool", "read_file", "", sig)
        check("повторы считаются", st.sig_count(rid, sig) == 4,
              str(st.sig_count(rid, sig)))
        check("чужая подпись не засчитана", st.sig_count(rid, other) == 0)
        st.close()


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ПАМЯТИ И АВТОНОМНОГО РЕЖИМА")
    print("=" * 60)
    test_store()
    test_recall_scale()
    test_hybrid_recall()
    test_memory_across_runs()
    test_plan()
    test_memory_tools()
    test_autorun()
    test_stuck_and_resume()
    test_repeat_detector()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
