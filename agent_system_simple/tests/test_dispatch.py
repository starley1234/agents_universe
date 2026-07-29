"""Тесты простого режима: кто берёт задачу и кому передаёт дальше.

Опасность здесь особая: неверный выбор роли не выглядит ошибкой. Агент
без нужных инструментов бодро отчитается, что «сделал», просто результат
будет пустым. Поэтому проверяется не только «выбрал профиль», но и
«выбрал ТОТ профиль» — на задачах из реальной работы.

Отдельно проверяется, что при непонятной задаче система честно говорит
«не распознал», а не назначает случайного исполнителя.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config, replace_profile          # noqa: E402
from agent.dispatch import choose_profile, score_profiles  # noqa: E402

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


KNOWN = Config.list_profiles()

#: Задачи из настоящей работы и ожидаемый исполнитель.
CASES = [
    ("посчитай зазоры в редукторе и проверь STL на коллизии", "cad"),
    ("собери модель водила в OpenSCAD", "cad"),
    ("почини падающий тест в calc.py", "coder"),
    ("напиши скрипт на Python для разбора логов", "coder"),
    ("проверь код в src/ и найди недостатки", "reviewer"),
    ("почему на сервере кончается память", "ops"),
    ("служба не запускается, посмотри journalctl", "ops"),
    ("сделай презентацию по итогам квартала", "office"),
    ("подготовь письмо заказчику и отправь", "office"),
    ("разбери договоры из папки in/ и вытащи реквизиты", "docs"),
    ("проверь комплект документов на соответствие ГОСТ", "verify"),
    ("проиндексируй docs/ и найди требования к материалам", "rag"),
    ("построй онтологию изделия", "onto"),
    ("собери обзор рынка редукторов", "research"),
    ("напиши рекламный пост про новый редуктор", "marketing"),
]


def test_choice() -> None:
    section("Выбор исполнителя по задаче")
    for task, expect in CASES:
        got = choose_profile(task, KNOWN)
        check(f"«{task[:44]}» → {expect}", got.profile == expect,
              f"выбран {got.profile} ({got.reason})")


def test_unknown() -> None:
    section("Непонятная задача: честное «не знаю»")
    for task in ("сделай что-нибудь полезное", "приберись в комнате",
                 "ага", ""):
        got = choose_profile(task, KNOWN)
        check(f"«{task[:30]}» → без профиля", got.profile is None,
              f"выбран {got.profile}")
        check("  причина названа", bool(got.reason), got.reason)

    # Одно слабое слово не должно уводить задачу целиком.
    got = choose_profile("отчёт", KNOWN)
    check("одиночное слабое слово не назначает исполнителя",
          got.profile is None, f"{got.profile}: {got.reason}")


def test_explicit() -> None:
    section("Явное указание сильнее правил")
    got = choose_profile("[профиль: cad] сделай отчёт", KNOWN)
    check("указание в задаче принято", got.profile == "cad", str(got.profile))
    check("сказано, что указано явно", "явно" in got.reason, got.reason)

    # Указание перебивает даже сильные признаки другой темы.
    got = choose_profile("[профиль: office] почини падающий тест в calc.py",
                         KNOWN)
    check("указание перебивает признаки", got.profile == "office",
          f"{got.profile}: {got.reason}")

    got = choose_profile("[профиль: несуществующий] сделай", KNOWN)
    check("неизвестный профиль отвергнут", got.profile is None,
          str(got.profile))
    check("названа причина отказа", "неизвестн" in got.reason, got.reason)


def test_autonomous() -> None:
    section("Долгая задача → автономный режим")
    for task in ("в течение ночи собери обзор рынка",
                 "поработай несколько часов над рефакторингом",
                 "автономно проверь все документы"):
        check(f"«{task[:36]}» → автономно",
              choose_profile(task, KNOWN).autonomous, task)
    for task in ("почини тест", "сделай презентацию"):
        check(f"«{task}» → одиночный запуск",
              not choose_profile(task, KNOWN).autonomous, task)


def test_borderline() -> None:
    section("Задача на стыке тем")
    # «проверь документы на соответствие» — verify и docs рядом
    got = choose_profile(
        "проверь документы и подготовь отчёт о комплектности", KNOWN)
    check("на стыке выбирается лучший, но об этом сказано",
          got.profile is not None, str(got.profile))
    if got.runners_up:
        check("показаны ближайшие варианты", len(got.runners_up) >= 1,
              str(got.runners_up))
    check("объяснение читаемо человеком",
          "профиль" in got.explain() or "набор" in got.explain(),
          got.explain())


def test_scores() -> None:
    section("Баллы считаются прозрачно")
    s = score_profiles("сделай чертёж редуктора в openscad")
    check("cad набрал больше всех", max(s, key=s.get) == "cad", str(s))
    check("баллы положительные", all(v > 0 for v in s.values()), str(s))
    check("пустая задача — пустые баллы", score_profiles("") == {})

    # Русские падежи: основа слова должна ловить словоформы.
    for word in ("шестерня", "шестерни", "шестерённая передача"):
        check(f"«{word}» опознано как CAD",
              "cad" in score_profiles(f"рассчитай {word}"), word)


def test_replace_profile() -> None:
    section("Передача задачи: смена профиля не портит конфиг")
    cfg = Config.load(None, profile="coder", provider="ollama", model="m")
    other = replace_profile(cfg, "office")
    check("у нового профиля свои навыки",
          "makedocs" in other.skills and "makedocs" not in cfg.skills,
          f"{other.skills} / {cfg.skills}")
    check("исходный конфиг не изменён", cfg.profile == "coder"
          and "shell" in cfg.skills, str(cfg.skills))
    check("общее осталось общим",
          other.model == cfg.model and other.db == cfg.db)
    # Промпт прежней роли не должен утечь в новую: агент-делопроизводитель
    # с промптом разработчика будет вести себя как разработчик.
    check("промпт заменён на промпт новой роли",
          "документ" in (other.system_prompt or "").lower()
          and "разработчик" not in (other.system_prompt or "").lower(),
          (other.system_prompt or "")[:70])
    check("промпт исходной роли не изменён",
          "разработчик" in (cfg.system_prompt or "").lower(),
          (cfg.system_prompt or "")[:70])


def test_handoff() -> None:
    section("Передача между агентами внутри прогона")
    from agent.autorun import AutoRunner
    from agent.core import Agent, Result
    from agent.llm.base import BaseLLM
    from agent.store import Store

    class Mute(BaseLLM):
        def _chat_once(self, messages, tools=None):  # pragma: no cover
            raise AssertionError("модель не вызывается")

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        seen: list[str | None] = []

        def factory(profile: str | None = None) -> Agent:
            seen.append(profile)
            a = Agent.__new__(Agent)
            a.llm = Mute("t")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("сделано", [], "done", [])
            return a

        events: list[tuple[str, dict]] = []
        r = AutoRunner(factory, st, max_hours=1, max_iterations=5,
                       route_tasks=True, known_profiles=KNOWN,
                       on_event=lambda k, d: events.append((k, d)))
        r.run_id = st.start_run("разная работа")
        st.add_tasks(r.run_id, [
            "посчитать зазоры в редукторе по STL",
            "подготовить письмо заказчику и отправить",
            "починить падающий тест в calc.py",
        ])
        r._reflect = lambda task, summary: False
        r.run("разная работа", resume=r.run_id)

        check("каждый пункт ушёл своему агенту",
              seen == ["cad", "office", "coder"], str(seen))
        handoffs = [d for k, d in events if k == "handoff"]
        check("передачи объявлены человеку", len(handoffs) == 3,
              str(len(handoffs)))
        check("в передаче названа причина",
              all(h.get("reason") for h in handoffs), str(handoffs[:1]))

        # Без route_tasks поведение прежнее: один агент на всё.
        seen.clear()
        r2 = AutoRunner(factory, st, max_hours=1, max_iterations=5)
        r2.run_id = st.start_run("однородная работа")
        st.add_tasks(r2.run_id, ["раз", "два"])
        r2._reflect = lambda task, summary: False
        r2.run("однородная работа", resume=r2.run_id)
        check("без маршрутизации профиль не передаётся",
              seen == [None, None], str(seen))
        st.close()


def test_factory_without_profile() -> None:
    section("Старая фабрика без аргумента продолжает работать")
    from agent.autorun import AutoRunner
    from agent.core import Agent, Result
    from agent.llm.base import BaseLLM
    from agent.store import Store

    class Mute(BaseLLM):
        def _chat_once(self, messages, tools=None):  # pragma: no cover
            raise AssertionError("модель не вызывается")

    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "a.db")
        calls = {"n": 0}

        def old_factory() -> Agent:          # БЕЗ параметра profile
            calls["n"] += 1
            a = Agent.__new__(Agent)
            a.llm = Mute("t")
            a.on_event = lambda k, d: None
            a.run = lambda task: Result("ок", [], "done", [])
            return a

        r = AutoRunner(old_factory, st, max_hours=1, max_iterations=3,
                       route_tasks=True, known_profiles=KNOWN)
        r.run_id = st.start_run("цель")
        st.add_tasks(r.run_id, ["посчитать зазоры в редукторе по STL"])
        r._reflect = lambda task, summary: False
        res = r.run("цель", resume=r.run_id)
        check("прогон не упал на старой фабрике",
              res.stopped_by == "done", res.stopped_by)
        check("агент всё же создан", calls["n"] >= 1, str(calls["n"]))
        st.close()


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ: простой режим и передача между агентами")
    print("=" * 60)
    test_choice()
    test_unknown()
    test_explicit()
    test_autonomous()
    test_borderline()
    test_scores()
    test_replace_profile()
    test_handoff()
    test_factory_without_profile()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
