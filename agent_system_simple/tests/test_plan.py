"""Тесты декомпозиции: разбор задачи на шаги с исполнителями и порядком.

Главная опасность здесь — план, который ВЫГЛЯДИТ разумно, но выполняется
в неверном порядке. «Собрать отчёт» перед «посчитать данные» не падает с
ошибкой: агент честно напишет отчёт ни о чём, и человек заметит это
последним. Поэтому проверяется не «план построился», а «пункты берутся
по готовности зависимостей».

Второе: тупики. Провалилась зависимость или планировщик замкнул кольцо —
прогон обязан объяснить, почему план не доделан, а не оставить пункты
молча открытыми.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.autorun import AutoRunner, _json_block           # noqa: E402
from agent.core import Agent, Result                        # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, Usage         # noqa: E402
from agent.store import Store                               # noqa: E402

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


PROFILES = ["cad", "coder", "office", "research"]


def _store(td: str) -> Store:
    return Store(Path(td) / "a.db")


# ══════════════════════ порядок по зависимостям ═════════════════════
def test_order_by_deps() -> None:
    section("Порядок: пункт ждёт свои зависимости")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("собрать комплект")
        ids = st.add_steps(r, [
            {"title": "измерить зазоры", "profile": "cad",
             "check": "числа в мм"},
            {"title": "посчитать нагрузку", "profile": "coder",
             "needs": [1]},
            {"title": "собрать отчёт", "profile": "office",
             "needs": [1, 2]},
        ])
        check("шаги созданы", len(ids) == 3, str(ids))

        t = st.next_ready_task(r)
        check("первым идёт независимый пункт",
              t["title"] == "измерить зазоры", t["title"])
        check("исполнитель сохранён", t["profile"] == "cad", t["profile"])
        check("подсказка проверки сохранена",
              t["check_hint"] == "числа в мм", t["check_hint"])

        # Пока первый не закрыт — второй не берётся, хотя он «следующий».
        st.set_task(ids[1], "open")
        check("зависимый пункт не берётся раньше времени",
              st.next_ready_task(r)["id"] == ids[0])

        st.set_task(ids[0], "done", "0.48 мм")
        check("после закрытия зависимости идёт следующий",
              st.next_ready_task(r)["title"] == "посчитать нагрузку")
        st.set_task(ids[1], "done", "нагрузка мала")
        check("последним — зависящий от обоих",
              st.next_ready_task(r)["title"] == "собрать отчёт")
        st.set_task(ids[2], "done", "готово")
        check("план исчерпан", st.next_ready_task(r) is None)
        st.close()


def test_deps_numbering() -> None:
    section("Зависимости: номера списка переводятся в id")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("x")
        # Модель нумерует с 1 и не знает будущих id — перевод обязателен.
        ids = st.add_steps(r, [
            {"title": "первый шаг работы"},
            {"title": "второй шаг работы", "needs": [1]},
        ])
        rows = {t["id"]: t for t in st.tasks(r)}
        check("зависимость указывает на настоящий id",
              rows[ids[1]]["needs"] == str(ids[0]),
              rows[ids[1]]["needs"])
        # Проверяем именно ПЕРЕВОД, а не совпадение: в свежей базе id
        # начинаются с 1 и случайно совпадают с номерами списка. Второй
        # прогон в той же базе сдвигает id — тут подмена и вскрывается.
        r_next = st.start_run("второй прогон в той же базе")
        ids2 = st.add_steps(r_next, [
            {"title": "первый шаг второго прогона"},
            {"title": "второй шаг второго прогона", "needs": [1]},
        ])
        rows2 = {t["id"]: t for t in st.tasks(r_next)}
        check("номер списка переведён в id, а не записан как есть",
              rows2[ids2[1]]["needs"] == str(ids2[0]) and ids2[0] != 1,
              f"needs={rows2[ids2[1]]['needs']}, id={ids2[0]}")
        check("порядок во втором прогоне тоже верен",
              st.next_ready_task(r_next)["id"] == ids2[0])

        # Самозависимость и выход за границы списка отбрасываются.
        r2 = st.start_run("y")
        i2 = st.add_steps(r2, [
            {"title": "шаг сам на себя", "needs": [1]},
            {"title": "шаг в никуда", "needs": [99]},
        ])
        rows2 = {t["id"]: t for t in st.tasks(r2)}
        check("самозависимость отброшена",
              rows2[i2[0]]["needs"] == "", rows2[i2[0]]["needs"])
        check("несуществующий номер отброшен",
              rows2[i2[1]]["needs"] == "", rows2[i2[1]]["needs"])
        check("оба шага всё равно выполнимы",
              st.next_ready_task(r2) is not None)
        st.close()


def test_deadlock_failed() -> None:
    section("Тупик: зависимость провалена")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("x")
        ids = st.add_steps(r, [
            {"title": "добыть исходные данные"},
            {"title": "обработать данные", "needs": [1]},
            {"title": "независимая работа"},
        ])
        st.set_task(ids[0], "failed", "источник недоступен")

        t = st.next_ready_task(r)
        check("берётся независимый пункт, а не зависший",
              t and t["title"] == "независимая работа", str(t and t["title"]))
        st.set_task(ids[2], "done")
        check("зависящий от проваленного не берётся",
              st.next_ready_task(r) is None)
        dead = st.deadlocked(r)
        check("тупик распознан", [t["title"] for t in dead]
              == ["обработать данные"], str([t["title"] for t in dead]))
        st.close()


def test_deadlock_cycle() -> None:
    section("Тупик: кольцо зависимостей")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("x")
        st.add_steps(r, [{"title": "шаг А кольца", "needs": [2]},
                         {"title": "шаг Б кольца", "needs": [1]}])
        check("из кольца нечего взять", st.next_ready_task(r) is None)
        dead = {t["title"] for t in st.deadlocked(r)}
        check("оба шага кольца помечены тупиковыми",
              dead == {"шаг А кольца", "шаг Б кольца"}, str(dead))

        # Длинное кольцо через третий шаг тоже должно ловиться.
        r2 = st.start_run("y")
        st.add_steps(r2, [{"title": "первый в кольце", "needs": [3]},
                          {"title": "второй в кольце", "needs": [1]},
                          {"title": "третий в кольце", "needs": [2]}])
        check("кольцо из трёх распознано",
              len(st.deadlocked(r2)) == 3, str(len(st.deadlocked(r2))))
        st.close()


# ═══════════════════════ планировщик ════════════════════════════════
class Planner(BaseLLM):
    """Модель-планировщик с заданным ответом."""

    billable = False

    def __init__(self, answer: str) -> None:
        super().__init__("planner")
        self.answer = answer
        self.prompts: list[str] = []

    def _chat_once(self, messages, tools=None):
        self.prompts.append(str(messages[-1].get("content") or ""))
        return LLMReply(text=self.answer, usage=Usage(400, 120))


GOOD_PLAN = json.dumps({"шаги": [
    {"что": "измерить зазоры в редукторе", "кто": "cad",
     "после": [], "проверка": "числа в мм в отчёте"},
    {"что": "посчитать нагрузку по замерам", "кто": "coder",
     "после": [1], "проверка": "значение в кН"},
    {"что": "оформить отчёт в docx", "кто": "office",
     "после": [1, 2], "проверка": "файл otchet.docx"},
]}, ensure_ascii=False)


def _runner(td: str, answer: str, **kw) -> tuple[AutoRunner, Store]:
    st = _store(td)
    llm = Planner(answer)

    def factory(profile: str | None = None) -> Agent:
        a = Agent.__new__(Agent)
        a.llm = llm
        a.on_event = lambda k, d: None
        a.run = lambda task: Result(llm.chat(
            [{"role": "user", "content": task}]).text, [], "done", [])
        return a

    r = AutoRunner(factory, st, decompose=True, known_profiles=PROFILES,
                   profile_hints={p: p for p in PROFILES}, **kw)
    return r, st


def test_structured_plan() -> None:
    section("Планировщик: JSON превращается в план с исполнителями")
    with tempfile.TemporaryDirectory() as td:
        r, st = _runner(td, GOOD_PLAN)
        r.run_id = st.start_run("собрать комплект по редуктору")
        ok = r._plan_structured("собрать комплект по редуктору", "")
        check("план разобран", ok)

        rows = st.tasks(r.run_id)
        check("три шага", len(rows) == 3, str(len(rows)))
        check("исполнители назначены",
              [t["profile"] for t in rows] == ["cad", "coder", "office"],
              str([t["profile"] for t in rows]))
        check("проверки сохранены",
              all(t["check_hint"] for t in rows),
              str([t["check_hint"] for t in rows]))
        check("зависимости проставлены",
              rows[1]["needs"] == str(rows[0]["id"])
              and set(rows[2]["needs"].split(",")) ==
              {str(rows[0]["id"]), str(rows[1]["id"])},
              f"{rows[1]['needs']} / {rows[2]['needs']}")
        check("первым берётся независимый",
              st.next_ready_task(r.run_id)["title"].startswith("измерить"))
        check("список исполнителей попал в промпт",
              "cad" in r.make_agent().llm.prompts[0], "модель не знает, "
                                                      "кому назначать")


def test_plan_fallback() -> None:
    section("Планировщик: мусор вместо JSON — откат к простому плану")
    with tempfile.TemporaryDirectory() as td:
        r, st = _runner(td, "Вот мой план: сначала одно, потом другое.")
        r.run_id = st.start_run("цель")
        warns: list[str] = []
        r.on_event = lambda k, v: warns.append(v.get("message", "")) \
            if k == "warn" else None
        ok = r._plan_structured("цель", "")
        check("структурный разбор честно провалился", not ok)
        check("пунктов не создано", st.tasks(r.run_id) == [])

        # Полный _plan должен откатиться к плоскому списку, а не встать.
        r2, st2 = _runner(td + "/2", "первый пункт работы\nвторой пункт работы")
        r2.run_id = st2.start_run("цель")
        msgs: list[str] = []
        r2.on_event = lambda k, v: msgs.append(v.get("message", "")) \
            if k == "warn" else None
        r2._plan("цель")
        check("план всё равно построен", len(st2.tasks(r2.run_id)) >= 2,
              str(len(st2.tasks(r2.run_id))))
        check("о переходе к простому плану сказано",
              any("списком" in m for m in msgs), str(msgs))


def test_plan_bad_profiles() -> None:
    section("Планировщик: выдуманный исполнитель не назначается")
    plan = json.dumps({"шаги": [
        {"что": "сделать первую часть работы", "кто": "супер-агент"},
        {"что": "сделать вторую часть работы", "кто": "coder"},
    ]}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        r, st = _runner(td, plan)
        r.run_id = st.start_run("цель")
        r._plan_structured("цель", "")
        rows = st.tasks(r.run_id)
        check("несуществующий профиль отброшен", rows[0]["profile"] == "",
              rows[0]["profile"])
        check("настоящий профиль сохранён", rows[1]["profile"] == "coder")
        check("пункт с пустым исполнителем всё равно выполним",
              st.next_ready_task(r.run_id) is not None)


def test_plan_cycle_recovered() -> None:
    section("Планировщик: кольцо снимается, работа не встаёт")
    plan = json.dumps({"шаги": [
        {"что": "первый шаг с кольцом", "после": [2]},
        {"что": "второй шаг с кольцом", "после": [1]},
    ]}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as td:
        r, st = _runner(td, plan)
        r.run_id = st.start_run("цель")
        warns: list[str] = []
        r.on_event = lambda k, v: warns.append(v.get("message", "")) \
            if k == "warn" else None
        r._plan_structured("цель", "")
        check("о кольце предупреждено",
              any("кольцо" in w for w in warns), str(warns))
        check("после снятия колец работа возможна",
              st.next_ready_task(r.run_id) is not None,
              "план остался невыполнимым")


def test_json_block() -> None:
    section("Разбор JSON терпит обёртку из текста")
    check("чистый JSON", _json_block('{"a": 1}') == {"a": 1})
    check("с преамбулой",
          _json_block('Вот план:\n{"a": 1}\nГотово.') == {"a": 1})
    check("мусор — None", _json_block("никакого джейсона") is None)
    check("битый JSON — None", _json_block('{"a": ') is None)
    check("список верхнего уровня — None", _json_block('[1,2]') is None)


# ═════════════════════ выполнение по агентам ════════════════════════
def test_execution_by_agent() -> None:
    section("Выполнение: пункт идёт назначенному исполнителю")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        used: list[str | None] = []
        handoffs: list[dict] = []

        def factory(profile: str | None = None) -> Agent:
            used.append(profile)
            a = Agent.__new__(Agent)
            a.llm = Planner("ок")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("сделано", [], "done", [])
            return a

        r = AutoRunner(factory, st, decompose=True, route_tasks=True,
                       known_profiles=PROFILES, max_iterations=5,
                       on_event=lambda k, v: handoffs.append(v)
                       if k == "handoff" else None)
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [
            {"title": "измерить зазоры", "profile": "cad"},
            {"title": "оформить отчёт", "profile": "office", "needs": [1]},
        ])
        r._reflect = lambda task, summary: False
        res = r.run("цель", resume=r.run_id)

        check("прогон завершён", res.stopped_by == "done", res.stopped_by)
        check("каждый пункт ушёл своему исполнителю",
              used[:2] == ["cad", "office"], str(used[:2]))
        check("передача объявлена человеку", len(handoffs) == 2,
              str(len(handoffs)))
        check("названа причина «назначен планом»",
              all("назначен планом" in h.get("reason", "")
                  for h in handoffs), str([h.get("reason") for h in handoffs]))


def test_assigned_beats_guess() -> None:
    section("Назначение плана важнее догадки диспетчера")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        used: list[str | None] = []

        def factory(profile: str | None = None) -> Agent:
            used.append(profile)
            a = Agent.__new__(Agent)
            a.llm = Planner("ок")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("сделано", [], "done", [])
            return a

        r = AutoRunner(factory, st, decompose=True, route_tasks=True,
                       known_profiles=PROFILES, max_iterations=3)
        r.run_id = st.start_run("цель")
        # Текст явно «про код», но планировщик назначил office —
        # он видел задачу целиком, диспетчер видит одну строку.
        st.add_steps(r.run_id, [
            {"title": "почини падающий тест в calc.py", "profile": "office"}])
        r._reflect = lambda task, summary: False
        r.run("цель", resume=r.run_id)
        check("исполнитель взят из плана, а не угадан",
              used[:1] == ["office"], str(used[:1]))


def test_deadlock_reported() -> None:
    section("Прогон объясняет, почему план не доделан")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)

        def factory(profile: str | None = None) -> Agent:
            a = Agent.__new__(Agent)
            a.llm = Planner("ок")
            a.on_event = lambda k, d: None
            # Пункт всегда проваливаем: за ним стоит зависимый.
            a.run = lambda task: Result("не вышло", [], "done", [])
            return a

        r = AutoRunner(factory, st, decompose=True, known_profiles=PROFILES,
                       max_iterations=6, replan_after_fails=99)
        r.run_id = st.start_run("цель")
        ids = st.add_steps(r.run_id, [
            {"title": "добыть исходные данные"},
            {"title": "обработать добытое", "needs": [1]},
        ])
        st.set_task(ids[0], "failed", "источник недоступен")
        r._reflect = lambda task, summary: False
        res = r.run("цель", resume=r.run_id)

        check("прогон остановлен как тупик", res.stopped_by == "deadlock",
              res.stopped_by)
        check("в итоге сказано, что именно не выполнено",
              "НЕ ВЫПОЛНЕНЫ" in res.summary and "обработать" in res.summary,
              res.summary[-200:])
        rows = {t["id"]: t for t in st.tasks(r.run_id)}
        check("тупиковый пункт не остался «открытым»",
              rows[ids[1]]["status"] == "skipped", rows[ids[1]]["status"])
        check("причина записана в пункт",
              "зависимост" in (rows[ids[1]]["result"] or ""),
              rows[ids[1]]["result"] or "")


def test_context_has_deps() -> None:
    section("Исполнитель видит результаты предыдущих пунктов")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)

        def factory(profile: str | None = None) -> Agent:
            a = Agent.__new__(Agent)
            a.llm = Planner("ок")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("ок", [], "done", [])
            return a

        r = AutoRunner(factory, st, decompose=True, known_profiles=PROFILES)
        r.run_id = st.start_run("цель")
        ids = st.add_steps(r.run_id, [
            {"title": "измерить зазор"},
            {"title": "оформить отчёт", "needs": [1],
             "check": "файл otchet.docx"},
        ])
        st.set_task(ids[0], "done", "зазор 0.48 мм")
        task = st.next_ready_task(r.run_id)
        ctx = r._context(task, "")

        check("в контексте есть результат зависимости",
              "0.48 мм" in ctx, ctx[:300])
        check("сказано, чем подтвердить выполнение",
              "otchet.docx" in ctx and "ПОДТВЕРДИТЬ" in ctx, ctx[:300])
        check("сама задача на месте", "оформить отчёт" in ctx)


def test_migration_old_db() -> None:
    section("Старая база: колонки добавляются, данные целы")
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "old.db"
        c = sqlite3.connect(path)
        c.executescript(
            "CREATE TABLE task(id INTEGER PRIMARY KEY, run_id INTEGER,"
            " parent_id INTEGER, title TEXT NOT NULL,"
            " status TEXT DEFAULT 'open', result TEXT, created REAL,"
            " updated REAL, ord INTEGER DEFAULT 0);"
            "INSERT INTO task(run_id,title) VALUES(1,'пункт из старой базы');")
        c.commit()
        c.close()

        st = Store(path)
        cols = {r[1] for r in st.db.execute("PRAGMA table_info(task)")}
        check("новые колонки добавлены",
              {"profile", "needs", "check_hint"} <= cols, str(sorted(cols)))
        row = dict(st.db.execute("SELECT * FROM task").fetchone())
        check("старые данные не потеряны",
              row["title"] == "пункт из старой базы", row["title"])
        check("новые поля пустые, а не None",
              row["profile"] == "" and row["needs"] == "",
              f"{row['profile']!r}/{row['needs']!r}")
        check("повторное открытие не ломается",
              Store(path).tasks(1)[0]["title"] == "пункт из старой базы")
        st.close()


# ═══════════════════ подшаги: второй уровень ════════════════════════
def test_split_basic() -> None:
    section("Подшаги: крупный пункт разбивается исполнителем")
    from agent.tools import memory as M

    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("цель")
        ids = st.add_steps(r, [
            {"title": "собрать комплект документов", "profile": "office"},
            {"title": "отправить заказчику", "needs": [1]}])
        t = {x.name: x for x in M.build(st, lambda: r)}

        out = t["plan_split"].fn(task_id=ids[0],
                                 steps="написать отчёт\nсобрать таблицу\n"
                                       "сверить с ГОСТ")
        check("подшаги созданы", "3 подшагов" in out, out[:80])
        kids = st.children(ids[0])
        check("подшаги привязаны к родителю", len(kids) == 3, str(len(kids)))
        check("родитель у подшагов верный",
              all(k["parent_id"] == ids[0] for k in kids))

        # Родитель сам в работу не идёт: работают дети.
        nxt = st.next_ready_task(r)
        check("в работу идёт подшаг, а не родитель",
              nxt["id"] == kids[0]["id"], nxt["title"])
        check("зависимый пункт по-прежнему ждёт",
              nxt["title"] != "отправить заказчику")


def test_split_closes_parent() -> None:
    section("Подшаги: родитель закрывается сам")
    from agent.tools import memory as M

    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("цель")
        ids = st.add_steps(r, [{"title": "собрать комплект документов"},
                               {"title": "отправить заказчику", "needs": [1]}])
        t = {x.name: x for x in M.build(st, lambda: r)}
        t["plan_split"].fn(task_id=ids[0], steps="раз работа\nдва работа")
        kids = st.children(ids[0])

        st.set_task(kids[0]["id"], "done", "ок")
        check("пока не все подшаги готовы — родитель ждёт",
              st.close_finished_parents(r) == [])
        check("родитель всё ещё открыт",
              [x for x in st.tasks(r) if x["id"] == ids[0]][0]["status"]
              == "open")

        st.set_task(kids[1]["id"], "done", "ок")
        closed = st.close_finished_parents(r)
        check("после всех подшагов родитель закрыт",
              [c["id"] for c in closed] == [ids[0]], str(closed))
        check("в результате написано, сколько подшагов",
              "2 из 2" in closed[0]["result"], closed[0]["result"])
        check("следом разблокировался зависимый пункт",
              st.next_ready_task(r)["title"] == "отправить заказчику")


def test_split_failed_kid() -> None:
    section("Подшаги: провал ребёнка виден в родителе")
    from agent.tools import memory as M

    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("цель")
        ids = st.add_steps(r, [{"title": "большая работа целиком"}])
        t = {x.name: x for x in M.build(st, lambda: r)}
        t["plan_split"].fn(task_id=ids[0], steps="часть первая\nчасть вторая")
        kids = st.children(ids[0])
        st.set_task(kids[0]["id"], "done", "ок")
        st.set_task(kids[1]["id"], "failed", "не вышло")

        closed = st.close_finished_parents(r)
        check("родитель закрыт как проваленный",
              closed and closed[0]["status"] == "failed",
              str(closed and closed[0]["status"]))
        check("названо, какой подшаг не вышел",
              "часть вторая" in closed[0]["result"], closed[0]["result"])
        check("успешные подшаги тоже посчитаны",
              "1 из 2" in closed[0]["result"], closed[0]["result"])


def test_split_limits() -> None:
    section("Подшаги: пределы дробления")
    from agent.tools import memory as M
    from agent.tools.base import ToolError

    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("цель")
        ids = st.add_steps(r, [{"title": "работа для разбиения"}])
        t = {x.name: x for x in M.build(st, lambda: r)}

        try:
            t["plan_split"].fn(task_id=ids[0], steps="один подшаг")
            check("разбиение на один подшаг отвергнуто", False, "принято")
        except ToolError as exc:
            check("разбиение на один подшаг отвергнуто",
                  "минимум на 2" in str(exc), str(exc)[:60])

        try:
            t["plan_split"].fn(task_id=999, steps="раз работа\nдва работа")
            check("несуществующий пункт отвергнут", False, "принят")
        except ToolError:
            check("несуществующий пункт отвергнут", True)

        t["plan_split"].fn(task_id=ids[0], steps="раз работа\nдва работа")
        try:
            t["plan_split"].fn(task_id=ids[0], steps="три работа\nчетыре раб")
            check("повторное разбиение отвергнуто", False, "принято")
        except ToolError as exc:
            check("повторное разбиение отвергнуто", "уже разбит" in str(exc))

        # Глубже двух уровней нельзя: иначе дробление вместо работы.
        kid = st.children(ids[0])[0]
        try:
            t["plan_split"].fn(task_id=kid["id"], steps="а работа\nб работа")
            check("третий уровень отвергнут", False, "создан")
        except ToolError as exc:
            check("третий уровень отвергнут",
                  "подшагом" in str(exc), str(exc)[:70])

        many = "\n".join(f"подшаг номер {i}" for i in range(1, 12))
        r2 = st.start_run("вторая цель")
        i2 = st.add_steps(r2, [{"title": "ещё одна работа"}])
        t2 = {x.name: x for x in M.build(st, lambda: r2)}
        t2["plan_split"].fn(task_id=i2[0], steps=many)
        check("больше шести подшагов не создаётся",
              len(st.children(i2[0])) == 6, str(len(st.children(i2[0]))))


def test_split_tree_view() -> None:
    section("Подшаги: план читается деревом")
    from agent.tools import memory as M

    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        r = st.start_run("цель")
        ids = st.add_steps(r, [{"title": "первый крупный пункт"},
                               {"title": "второй пункт плана"}])
        t = {x.name: x for x in M.build(st, lambda: r)}
        t["plan_split"].fn(task_id=ids[0], steps="часть A работы\n"
                                                 "часть B работы")
        rows = st.tasks(r)
        titles = [x["title"] for x in rows]
        check("подшаги идут сразу за родителем",
              titles == ["первый крупный пункт", "часть A работы",
                         "часть B работы", "второй пункт плана"],
              str(titles))
        view = t["plan_show"].fn()
        check("подшаги показаны с отступом",
              "    [ ] " in view, view[:200])
        check("виден и родитель, и второй пункт",
              "первый крупный" in view and "второй пункт" in view)


def test_split_in_run() -> None:
    section("Подшаги в прогоне: разбиение — не выполнение")
    with tempfile.TemporaryDirectory() as td:
        st = _store(td)
        events: list[tuple[str, dict]] = []
        state = {"split": False}

        def factory(profile: str | None = None) -> Agent:
            a = Agent.__new__(Agent)
            a.llm = Planner("ок")
            a.on_event = lambda k, d: None

            def run(task: str) -> Result:
                # На первом заходе агент разбивает пункт, дальше работает.
                if not state["split"] and "крупная работа" in task:
                    state["split"] = True
                    tid = [t for t in st.tasks(r.run_id)
                           if t["title"] == "крупная работа"][0]["id"]
                    st.add_tasks(r.run_id, ["часть первая", "часть вторая"],
                                 parent=tid)
                    return Result("разбил на подшаги", [], "done", [])
                return Result("сделано", [], "done", [])
            a.run = run
            return a

        r = AutoRunner(factory, st, decompose=True, known_profiles=PROFILES,
                       max_iterations=8,
                       on_event=lambda k, v: events.append((k, v)))
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [{"title": "крупная работа"}])
        r._reflect = lambda t, s: False
        res = r.run("цель", resume=r.run_id)

        kinds = [k for k, _ in events]
        check("разбиение объявлено человеку", "split" in kinds, str(set(kinds)))
        check("закрытие родителя объявлено", "parent_done" in kinds,
              str(set(kinds)))
        rows = {t["title"]: t for t in st.tasks(r.run_id)}
        check("подшаги выполнены",
              all(rows[n]["status"] == "done"
                  for n in ("часть первая", "часть вторая")),
              str({n: rows[n]["status"] for n in
                   ("часть первая", "часть вторая")}))
        check("родитель закрыт по подшагам, а не по своему ответу",
              rows["крупная работа"]["status"] == "done"
              and "подшагов" in (rows["крупная работа"]["result"] or ""),
              rows["крупная работа"]["result"] or "")
        check("прогон завершён", res.stopped_by == "done", res.stopped_by)


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ДЕКОМПОЗИЦИИ: шаги, исполнители, порядок")
    print("=" * 60)
    test_order_by_deps()
    test_deps_numbering()
    test_deadlock_failed()
    test_deadlock_cycle()
    test_structured_plan()
    test_plan_fallback()
    test_plan_bad_profiles()
    test_plan_cycle_recovered()
    test_json_block()
    test_execution_by_agent()
    test_assigned_beats_guess()
    test_deadlock_reported()
    test_context_has_deps()
    test_migration_old_db()
    test_split_basic()
    test_split_closes_parent()
    test_split_failed_kid()
    test_split_limits()
    test_split_tree_view()
    test_split_in_run()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
