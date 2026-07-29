"""Тесты оркестратора: решает ли он по делу и знает ли меру.

Оркестратор опаснее прочих частей: он меняет план во время работы. Две
крайности, обе плохи. Слишком робкий бесполезен — тогда это прежнее
механическое исполнение списка. Слишком деятельный никогда не
заканчивает: добавляет шаги быстрее, чем они выполняются, и перекидывает
пункт между агентами по кругу.

Поэтому проверяется не «оркестратор ответил», а что ограничения
СРАБАТЫВАЮТ на модели, которая нарочно их нарушает.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.autorun import AutoRunner                       # noqa: E402
from agent.core import Agent, Result                       # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, Usage        # noqa: E402
from agent.orchestrator import (GROWTH_LIMIT, MAX_ADD_AT_ONCE,  # noqa: E402
                                MAX_REASSIGN, Orchestrator)
from agent.store import Store                              # noqa: E402

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


PROFILES = {"cad": "конструкции", "coder": "код", "office": "документы",
            "verify": "проверка"}


class Boss(BaseLLM):
    """Оркестратор с заданным ответом."""

    billable = False

    def __init__(self, answer: Any) -> None:
        super().__init__("boss")
        self.answer = (json.dumps(answer, ensure_ascii=False)
                       if isinstance(answer, dict) else answer)
        self.prompts: list[str] = []

    def _chat_once(self, messages, tools=None):
        self.prompts.append(str(messages[-1].get("content") or ""))
        return LLMReply(text=self.answer, usage=Usage(700, 90))


def _task(tid: int = 1, title: str = "измерить зазоры",
          profile: str = "cad") -> dict:
    return {"id": tid, "title": title, "profile": profile,
            "status": "done", "result": ""}


def _plan() -> list[dict]:
    return [
        {"id": 1, "title": "измерить зазоры", "profile": "cad",
         "status": "done", "result": "0.48"},
        {"id": 2, "title": "оформить отчёт", "profile": "office",
         "status": "open", "result": ""},
    ]


# ═══════════════════════ когда вмешиваться ══════════════════════════
def test_reason() -> None:
    section("Повод вмешаться: правилами, а не гаданием")
    o = Orchestrator(Boss({}), PROFILES, every=3)
    good = "Сделано: замерены зазоры, получено 0.48 мм, всё в допуске."

    check("шаг провалился — вмешиваемся",
          "провалился" in o.reason(_task(), good, "failed", 1, False))
    check("агент топчется — вмешиваемся",
          "топчется" in o.reason(_task(), good, "done", 1, True))
    check("пустой результат — вмешиваемся",
          "пустой" in o.reason(_task(), "готово", "done", 1, False))
    check("каждый третий шаг — вмешиваемся",
          "пройдено" in o.reason(_task(), good, "done", 3, False))
    # Иначе каждый шаг стоил бы лишнего вызова модели, а решение при
    # нормальном ходе всегда одно — «дальше».
    check("когда всё идёт по плану — молчим",
          o.reason(_task(), good, "done", 2, False) == "",
          o.reason(_task(), good, "done", 2, False))
    check("без модели оркестратор не мешает",
          Orchestrator(None, PROFILES).reason(_task(), "х", "failed", 1,
                                              True) == "")


# ═════════════════════════ решения ══════════════════════════════════
def test_decisions() -> None:
    section("Решения: понимаются и исполняются")
    with tempfile.TemporaryDirectory():
        o = Orchestrator(Boss({"решение": "дальше", "почему": "всё по плану"}),
                         PROFILES)
        d = o.decide("цель", _task(), "результат", "done", _plan(), [], "п")
        check("«дальше» распознано", d.action == "дальше", d.action)

        o = Orchestrator(Boss({"решение": "сменить", "кто": "coder",
                               "почему": "это задача для программиста"}),
                         PROFILES)
        d = o.decide("цель", _task(), "не вышло", "failed", _plan(), [], "п")
        check("«сменить» распознано", d.action == "сменить", d.action)
        check("новый исполнитель назван", d.who == "coder", d.who)
        check("объяснение читаемо человеком",
              "coder" in d.explain(), d.explain())

        o = Orchestrator(Boss({"решение": "добавить", "почему": "вскрылось",
                               "шаги": [{"что": "проверить материал детали",
                                         "кто": "cad", "после": [1]}]}),
                         PROFILES)
        d = o.decide("цель", _task(), "р", "done", _plan(), [], "п")
        check("«добавить» распознано", d.action == "добавить", d.action)
        check("шаг разобран", len(d.steps) == 1 and
              d.steps[0]["profile"] == "cad", str(d.steps))

        o = Orchestrator(Boss({"решение": "закончить", "почему": "цель есть"}),
                         PROFILES)
        d = o.decide("цель", _task(), "р", "done", _plan(), [], "п")
        check("«закончить» распознано", d.action == "закончить", d.action)


def test_limits() -> None:
    section("Ограничения: модель нарушает — система не даёт")
    # Выдуманный исполнитель: если пропустить, пункт уйдёт в никуда.
    o = Orchestrator(Boss({"решение": "сменить", "кто": "супер-агент"}),
                     PROFILES)
    d = o.decide("цель", _task(), "р", "failed", _plan(), [], "п")
    check("несуществующий агент отвергнут", d.action == "дальше", d.action)
    check("причина названа", "неизвестн" in d.why, d.why)

    # Смена на того же исполнителя — пустая трата подхода.
    o = Orchestrator(Boss({"решение": "сменить", "кто": "cad"}), PROFILES)
    d = o.decide("цель", _task(profile="cad"), "р", "failed", _plan(), [], "п")
    check("смена на того же отвергнута", d.action == "дальше", d.action)

    # Больше двух раз менять исполнителя нельзя: дело не в нём.
    o = Orchestrator(Boss({"решение": "сменить", "кто": "coder"}), PROFILES)
    for _ in range(MAX_REASSIGN):
        o.note_reassign(1)
    d = o.decide("цель", _task(), "р", "failed", _plan(), [], "п")
    check(f"после {MAX_REASSIGN} смен исполнителя больше не меняем",
          d.action == "дальше", d.action)
    check("сказано почему", "дважды" in d.why, d.why)

    # Добавление сверх меры: план растёт быстрее, чем выполняется.
    many = [{"что": f"новый шаг работы номер {i}", "кто": "cad"}
            for i in range(10)]
    o = Orchestrator(Boss({"решение": "добавить", "шаги": many}), PROFILES)
    d = o.decide("цель", _task(), "р", "done", _plan(), [], "п")
    check(f"за раз не больше {MAX_ADD_AT_ONCE} шагов",
          len(d.steps) <= MAX_ADD_AT_ONCE, str(len(d.steps)))

    o = Orchestrator(Boss({"решение": "добавить", "шаги": many}), PROFILES,
                     max_growth=2)
    o.note_added(2)
    d = o.decide("цель", _task(), "р", "done", _plan(), [], "п")
    check("предел роста плана соблюдён", d.action == "дальше", d.action)
    check("сказано про предел", "предел" in d.why, d.why)

    # Пустое «пропустить» без номеров — ничего не делаем.
    o = Orchestrator(Boss({"решение": "пропустить"}), PROFILES)
    d = o.decide("цель", _task(), "р", "done", _plan(), [], "п")
    check("«пропустить» без номеров отвергнуто", d.action == "дальше",
          d.action)


def test_broken_answer() -> None:
    section("Оркестратор сломался: работа не встаёт")
    o = Orchestrator(Boss("Думаю, надо продолжать в том же духе."), PROFILES)
    d = o.decide("цель", _task(), "р", "done", _plan(), [], "п")
    check("нечитаемый ответ — работаем по плану", d.action == "дальше",
          d.action)
    check("это замечено", o.unparsed == 1, str(o.unparsed))
    check("причина сохранена", "не разобран" in d.why, d.why)

    class Dead(BaseLLM):
        billable = False

        def _chat_once(self, messages, tools=None):
            from agent.llm.base import LLMError
            raise LLMError("модель недоступна")

    o2 = Orchestrator(Dead("x"), PROFILES)
    d2 = o2.decide("цель", _task(), "р", "done", _plan(), [], "п")
    check("недоступная модель не роняет прогон", d2.action == "дальше")
    check("сказано, что оркестратор недоступен",
          "недоступен" in d2.why, d2.why)


def test_prompt_content() -> None:
    section("Оркестратор видит то, что нужно для решения")
    boss = Boss({"решение": "дальше"})
    o = Orchestrator(boss, PROFILES)
    o.decide("собрать комплект", _task(), "зазор 0.48 мм", "done", _plan(),
             ["материал сталь 40Х"], "шаг провалился")
    p = boss.prompts[0]
    check("видит цель", "собрать комплект" in p)
    check("видит результат шага", "0.48" in p)
    check("видит весь план", "оформить отчёт" in p)
    check("видит, кто исполнял", "cad" in p)
    check("видит список исполнителей", "office: документы" in p, p[-200:])
    check("видит известные факты", "сталь 40Х" in p)
    check("знает, почему его позвали", "провалился" in p)


# ═════════════════════ работа в прогоне ═════════════════════════════
def _runner(td: str, boss_answer: Any, **kw):
    st = Store(Path(td) / "a.db")
    used: list[str | None] = []

    def factory(profile: str | None = None) -> Agent:
        used.append(profile)
        a = Agent.__new__(Agent)
        a.llm = Boss("ок")
        a.on_event = lambda k, d: None
        a.run = lambda task: Result("сделано, но подробностей нет", [],
                                    "done", [])
        return a

    o = Orchestrator(Boss(boss_answer), PROFILES, every=1, **kw)
    r = AutoRunner(factory, st, decompose=True, known_profiles=list(PROFILES),
                   profile_hints=PROFILES, orchestrator=o,
                   max_iterations=8, replan_after_fails=99)
    r._reflect = lambda t, s: False
    return r, st, o, used


def test_reassign_in_run() -> None:
    section("В прогоне: пункт передаётся другому агенту")
    with tempfile.TemporaryDirectory() as td:
        r, st, o, used = _runner(td, {"решение": "сменить", "кто": "coder",
                                      "почему": "это работа для кода"})
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [{"title": "починить расчёт", "profile": "cad"}])
        events: list[tuple] = []
        r.on_event = lambda k, v: events.append((k, v))
        r.run("цель", resume=r.run_id)

        rows = st.tasks(r.run_id)
        check("исполнитель у пункта сменился",
              rows[0]["profile"] == "coder", rows[0]["profile"])
        check("передача объявлена человеку",
              any(k == "orchestrate" and v["action"] == "сменить"
                  for k, v in events), str([k for k, _ in events]))
        check("второй раз работал уже coder",
              "coder" in used, str(used))
        # Бесконечной передачи быть не должно.
        check("смен не больше предела",
              sum(o.reassigned.values()) <= MAX_REASSIGN + 1,
              str(o.reassigned))


def test_add_steps_in_run() -> None:
    section("В прогоне: план дополняется на ходу")
    with tempfile.TemporaryDirectory() as td:
        r, st, o, used = _runner(td, {
            "решение": "добавить", "почему": "вскрылась работа",
            "шаги": [{"что": "проверить материал по паспорту",
                      "кто": "verify"}]}, max_growth=2)
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [{"title": "измерить зазоры", "profile": "cad"}])
        grew: list[list[str]] = []
        r.on_event = lambda k, v: grew.append(v["items"]) \
            if k == "plan_grew" else None
        r.run("цель", resume=r.run_id)

        titles = [t["title"] for t in st.tasks(r.run_id)]
        check("новый шаг появился в плане",
              any("паспорт" in t for t in titles), str(titles))
        check("о росте плана сказано", grew, str(grew))
        check("новый шаг тоже выполнен",
              all(t["status"] in ("done", "skipped")
                  for t in st.tasks(r.run_id)),
              str([(t["title"][:20], t["status"])
                   for t in st.tasks(r.run_id)]))
        check("рост ограничен", o.added <= 2, str(o.added))


def test_finish_early() -> None:
    section("В прогоне: цель достигнута — остальное не нужно")
    with tempfile.TemporaryDirectory() as td:
        r, st, o, used = _runner(td, {"решение": "закончить",
                                      "почему": "ответ уже получен"})
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [
            {"title": "быстрый способ найти ответ"},
            {"title": "долгий способ найти ответ"},
            {"title": "ещё один способ"}])
        res = r.run("цель", resume=r.run_id)

        check("прогон завершён досрочно", res.stopped_by == "done",
              res.stopped_by)
        rows = st.tasks(r.run_id)
        skipped = [t for t in rows if t["status"] == "skipped"]
        check("лишние пункты закрыты явно, а не забыты",
              len(skipped) == 2, str([(t["title"][:18], t["status"])
                                      for t in rows]))
        check("в пункте написано, почему он не нужен",
              "не нужен" in (skipped[0]["result"] or ""),
              skipped[0]["result"] or "")


def test_skip_in_run() -> None:
    section("В прогоне: ненужные пункты снимаются")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")

        def factory(profile: str | None = None) -> Agent:
            a = Agent.__new__(Agent)
            a.llm = Boss("ок")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("сделано", [], "done", [])
            return a

        o = Orchestrator(Boss({"решение": "пропустить", "пункты": [3],
                               "почему": "данные уже есть"}),
                         PROFILES, every=1)
        r = AutoRunner(factory, st, decompose=True,
                       known_profiles=list(PROFILES), profile_hints=PROFILES,
                       orchestrator=o, max_iterations=6)
        r._reflect = lambda t, s: False
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [{"title": "первый шаг"},
                                {"title": "второй шаг"},
                                {"title": "третий шаг, ставший ненужным"}])
        r.run("цель", resume=r.run_id)

        rows = {t["id"]: t for t in st.tasks(r.run_id)}
        check("указанный пункт снят", rows[3]["status"] == "skipped",
              rows[3]["status"])
        check("причина записана",
              "оркестратором" in (rows[3]["result"] or ""),
              rows[3]["result"] or "")


def test_no_orchestrator() -> None:
    section("Без оркестратора всё работает как прежде")
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")

        def factory(profile: str | None = None) -> Agent:
            a = Agent.__new__(Agent)
            a.llm = Boss("ок")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("сделано", [], "done", [])
            return a

        r = AutoRunner(factory, st, decompose=True,
                       known_profiles=list(PROFILES), max_iterations=5)
        r._reflect = lambda t, s: False
        r.run_id = st.start_run("цель")
        st.add_steps(r.run_id, [{"title": "первый шаг"},
                                {"title": "второй шаг"}])
        res = r.run("цель", resume=r.run_id)
        check("прогон прошёл без вмешательств", res.stopped_by == "done",
              res.stopped_by)
        check("все пункты выполнены",
              all(t["status"] == "done" for t in st.tasks(r.run_id)))


def test_report() -> None:
    section("Итог: видно, что делал оркестратор")
    o = Orchestrator(Boss({"решение": "дальше"}), PROFILES)
    check("без вмешательств отчёт пуст", o.report() == "", o.report())
    o.asked = 4
    o.note_added(2)
    o.note_reassign(1)
    o.unparsed = 1
    rep = o.report()
    check("сколько раз вмешивался", "4 раз" in rep, rep)
    check("сколько добавил", "добавил шагов: 2" in rep, rep)
    check("сколько сменил", "сменил исполнителя: 1" in rep, rep)
    check("неразобранные ответы видны", "не разобрано" in rep, rep)


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ОРКЕСТРАТОРА: решает по делу и знает меру")
    print("=" * 60)
    test_reason()
    test_decisions()
    test_limits()
    test_broken_answer()
    test_prompt_content()
    test_reassign_in_run()
    test_add_steps_in_run()
    test_finish_early()
    test_skip_in_run()
    test_no_orchestrator()
    test_report()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
