"""Тесты дискуссии: ловит ли она поддакивание, топтание и арбитра-тягуна.

Опасность здесь та же, что у дымового теста: система, которая всегда
доводит спор до «согласия», бесполезна — человек по её итогу принимает
решение. Поэтому проверяются не «две модели поговорили», а конкретные
режимы отказа из docs/DEBATE.md, каждый на заглушке с заданным нравом.

Отдельно проверяется, что «стороны не сошлись» — законный результат, а
не ошибка: подменять его мнением последнего говорившего нельзя.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import debate as D                             # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, Usage       # noqa: E402
from agent.store import Store                             # noqa: E402

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


# ───────────────────────── заглушки моделей ─────────────────────────
class Side(BaseLLM):
    """Сторона с заданным нравом."""

    billable = False

    def __init__(self, name: str, kind: str = "normal") -> None:
        super().__init__(name)
        self.kind = kind
        self.n = 0
        self.seen: list[str] = []

    def _chat_once(self, messages, tools=None):
        self.n += 1
        self.seen.append(str(messages[0]["content"]))
        u = Usage(500, 80)
        if self.kind == "agree":
            return LLMReply(text="Полностью согласен, добавить нечего.",
                            usage=u)
        if self.kind == "repeat":
            return LLMReply(text="Ресурсов не хватит на такую нагрузку.",
                            usage=u)
        if self.kind == "fact":
            if self.n == 1:
                return LLMReply(text="[ФАКТ?] проверь, сколько строк в "
                                     "data.txt", usage=u)
            return LLMReply(text=f"Учитывая проверенное, довод {self.n}.",
                            usage=u)
        if self.kind == "empty":
            return LLMReply(text="", usage=u)
        return LLMReply(text=f"Довод номер {self.n} от {self.model}: "
                             f"аспект {self.n} важнее прочего.", usage=u)


class Arbiter(BaseLLM):
    """Арбитр с заданным нравом."""

    billable = False

    def __init__(self, kind: str = "decides", after: int = 1) -> None:
        super().__init__("arbiter-" + kind)
        self.kind = kind
        self.after = after
        self.n = 0
        self.prompts: list[str] = []

    def _chat_once(self, messages, tools=None):
        self.n += 1
        self.prompts.append(str(messages[0]["content"]))
        u = Usage(800, 60)

        def j(**kw):
            return LLMReply(text=json.dumps(kw, ensure_ascii=False), usage=u)

        if self.kind == "stall":                 # всегда «продолжить»
            return j(**{"решение": "продолжить", "почему": "рано"})
        if self.kind == "broken":                # не умеет в JSON
            return LLMReply(text="Думаю, надо ещё поспорить.", usage=u)
        if self.kind == "executor":
            if self.n == 1:
                return j(**{"решение": "исполнитель",
                            "вопрос": "сколько строк в data.txt",
                            "почему": "спор упёрся в факт"})
            return j(**{"решение": "завершить", "итог": "Строк семь, "
                        "значит хватит SQLite.", "почему": "факт получен"})
        if self.kind == "branch":
            return j(**{"решение": "ветвление",
                        "варианты": ["мало данных", "много данных"],
                        "почему": "оба верны при разных условиях"})
        # decides: несколько кругов молчим, потом завершаем
        if self.n < self.after:
            return j(**{"решение": "продолжить", "почему": "ещё не всё"})
        return j(**{"решение": "завершить",
                    "итог": "Хватит SQLite: нагрузка мала.",
                    "почему": "доводы исчерпаны"})


def _debate(a_kind="normal", b_kind="normal", arb_kind="decides",
            after=1, **kw):
    td = tempfile.mkdtemp()
    st = Store(Path(td) / "d.db")
    d = D.Debate(Side("model-a", a_kind), Side("model-b", b_kind),
                 Arbiter(arb_kind, after), st, **kw)
    return d, st


# ───────────────────────── подпись довода ───────────────────────────
def test_signature() -> None:
    section("Подпись довода: перестановка слов — то же самое")
    sig = D.argument_sig
    check("перестановка слов даёт ту же подпись",
          sig("ресурсов не хватит") == sig("не хватит ресурсов"))
    check("отрицание меняет подпись",
          sig("ресурсов не хватит") != sig("ресурсов хватит"),
          "иначе согласие выглядело бы как спор")
    check("разные доводы — разные подписи",
          sig("нужен PostgreSQL") != sig("нужен Redis"))
    check("падежи не мешают",
          sig("нагрузка на систему") == sig("нагрузке систему"),
          "основы слов должны совпасть")
    check("пустой текст — пустая подпись", sig("") == "" and sig("и в на") == "")
    check("служебные слова не влияют",
          sig("это уже надо для нагрузки") == sig("нагрузки"),
          "предлоги и связки в подпись не идут")
    # Числа значимы: «3 сервера» и «30 серверов» — разный смысл.
    # Через основу слова они сливались, и спор о величине объявлялся
    # топтанием. Поймано тестом.
    check("числа различают доводы",
          sig("нужно 3 сервера") != sig("нужно 30 серверов"))


# ─────────────────────────── ядро ───────────────────────────────────
def test_basic_flow() -> None:
    section("Обычный ход: стороны говорят по очереди, арбитр завершает")
    d, st = _debate(after=2, rounds=8)
    res = d.run("нужен ли PostgreSQL")

    check("дискуссия завершена решением арбитра", res.status == "done",
          res.status)
    check("итог от арбитра, а не отсебятина",
          "SQLite" in res.verdict, res.verdict[:80])
    roles = [t.role for t in res.turns if t.role in ("a", "b")]
    check("ходы строго по очереди A-B",
          roles == ["a", "b"] * (len(roles) // 2), str(roles))
    check("реплики сохранены в базе",
          len(st.turns(res.debate_id)) == len(res.turns),
          f"{len(st.turns(res.debate_id))} против {len(res.turns)}")
    check("расход посчитан", res.tokens > 0, str(res.tokens))
    row = st.get_debate(res.debate_id)
    check("статус записан в базу", row["status"] == "done", row["status"])
    check("итог записан в базу", bool(row["verdict"]))
    check("первой стороне велено назвать слабое место",
          "СЛАБОЕ" in d.a.seen[0], d.a.seen[0][-200:])
    check("второй стороне велено возразить",
          "возражение" in d.b.seen[0], d.b.seen[0][-200:])


def test_rounds_limit() -> None:
    section("Предел кругов: спор не бесконечен")
    d, st = _debate(arb_kind="stall", rounds=4)
    res = d.run("бесконечный спор")
    check("остановились по кругам", res.status in ("no_consensus",),
          res.status)
    check("кругов ровно столько, сколько отвели", res.rounds <= 4,
          str(res.rounds))
    check("итог честно говорит об отсутствии согласия",
          "СОГЛАСИЕ НЕ ДОСТИГНУТО" in res.verdict, res.verdict[:80])
    check("в итоге приведены обе позиции",
          "Позиция A" in res.verdict and "Позиция B" in res.verdict)


def test_arbiter_stalling() -> None:
    section("Арбитр-тягун: «продолжить» не может длиться вечно")
    d, st = _debate(arb_kind="stall", rounds=30, arbiter_every=1)
    res = d.run("вопрос")
    check("разбор остановлен, а не идёт до предела кругов",
          res.rounds < 30, f"кругов {res.rounds}")
    check("названа причина: арбитр не решает",
          "продолжать" in res.verdict.lower(), res.verdict[:120])
    check(f"«продолжить» не больше {D.MAX_CONTINUE} раз подряд",
          d._continues <= D.MAX_CONTINUE + 1, str(d._continues))


def test_broken_arbiter() -> None:
    section("Арбитр не умеет в JSON: не падаем, но и не терпим")
    d, st = _debate(arb_kind="broken", rounds=30, arbiter_every=1)
    events: list[str] = []
    d.on_event = lambda k, v: events.append(k)
    res = d.run("вопрос")
    check("разбор не упал", res.status in ("no_consensus", "stuck"),
          res.status)
    check("нечитаемые ответы замечены", "arbiter_unparsed" in events,
          str(set(events)))
    check("после нескольких неудач арбитр признан негодным",
          "протокол" in res.verdict.lower(), res.verdict[:140])
    check("остановились быстро, не тратя бюджет", res.rounds <= 5,
          str(res.rounds))


def test_repetition() -> None:
    section("Топтание: одно и то же — не спор")
    d, st = _debate(a_kind="repeat", b_kind="repeat", arb_kind="stall",
                    rounds=20, arbiter_every=99)
    res = d.run("вопрос")
    check("топтание распознано", res.status == "stuck", res.status)
    check("остановились рано", res.rounds <= 6, str(res.rounds))
    check("названа причина", "повтор" in res.verdict.lower(),
          res.verdict[:100])


def test_agreement_triggers_arbiter() -> None:
    section("Поддакивание зовёт арбитра")
    d, st = _debate(a_kind="agree", b_kind="agree", after=1, rounds=8)
    res = d.run("вопрос")
    check("арбитр вызван и завершил", res.status == "done", res.status)
    check("это случилось быстро", res.rounds <= 3, str(res.rounds))
    # Согласие на ПЕРВОМ круге не считается: разбор для того и нужен,
    # чтобы найти возражения.
    d2, _ = _debate(a_kind="agree", b_kind="agree", arb_kind="stall",
                    rounds=1, arbiter_every=99)
    d2.run("вопрос")
    check("на первом круге согласие не засчитывается",
          not d2._need_arbiter(1), d2._need_arbiter(1))


def test_budget() -> None:
    section("Бюджет денег останавливает разбор")

    class Paid(Side):
        billable = True

    td = tempfile.mkdtemp()
    st = Store(Path(td) / "d.db")
    # реальная цена реальной модели, а не выдуманная
    d = D.Debate(Paid("gpt-4o-mini"), Paid("gpt-4o-mini"),
                 Arbiter("stall"), st, rounds=50, max_usd=0.001,
                 arbiter_every=99)
    res = d.run("дорогой вопрос")
    check("остановлено по бюджету", res.status == "budget", res.status)
    check("потрачено не сильно больше предела", res.cost <= 0.01,
          f"${res.cost:.4f}")
    check("в итоге названа причина", "бюджет" in res.verdict.lower(),
          res.verdict[:100])


def test_executor() -> None:
    section("Исполнитель: факт проверяется, а не додумывается")
    asked: list[str] = []

    def executor(task: str) -> str:
        asked.append(task)
        return "в файле 7 строк"

    d, st = _debate(a_kind="fact", arb_kind="executor", rounds=8,
                    executor=executor)
    res = d.run("сколько строк")

    check("исполнитель вызван", len(asked) == 1, str(asked))
    check("вызван с вопросом от арбитра", "data.txt" in asked[0], asked[0])
    check("результат стал репликой",
          any(t.role == "executor" for t in res.turns))
    check("факт попал в список проверенного",
          any("7 строк" in f for f in d.facts), str(d.facts))
    # Самое важное: факт должен дойти до СТОРОН, иначе проверка впустую.
    later = [p for p in d.a.seen if "ПРОВЕРЕННЫЕ ФАКТЫ" in p]
    check("стороны увидели проверенный факт", bool(later),
          "факт не попал в контекст сторон")
    check("разбор завершён с учётом факта",
          res.status == "done" and "семь" in res.verdict.lower(),
          res.verdict[:80])


def test_executor_limits() -> None:
    section("Исполнитель: пределы и сбои")
    calls = {"n": 0}

    def boom(task: str) -> str:
        calls["n"] += 1
        raise RuntimeError("инструмент сломался")

    d, st = _debate(a_kind="fact", arb_kind="executor", rounds=6,
                    executor=boom)
    res = d.run("вопрос")
    check("сбой исполнителя не роняет разбор",
          res.status in ("done", "no_consensus"), res.status)
    check("о сбое сказано прямо",
          any("не удалась" in t.text for t in res.turns
              if t.role == "executor"),
          str([t.text[:60] for t in res.turns if t.role == "executor"]))

    # без исполнителя — честный отказ, а не молчание
    d2, _ = _debate(a_kind="fact", arb_kind="executor", rounds=4)
    res2 = d2.run("вопрос")
    check("без исполнителя сказано, что проверить нечем",
          any("не подключён" in t.text for t in res2.turns
              if t.role == "executor"), "молча пропустили проверку")


def test_unknown_decision() -> None:
    section("Незнакомое решение арбитра трактуется безопасно")
    # Ветвление из системы убрано: разбор ведётся одной веткой. Старая
    # или чужая модель всё равно может прислать «ветвление» — это не
    # должно ни ронять разбор, ни молча считаться завершением.
    d, st = _debate(arb_kind="branch", rounds=4, arbiter_every=1)
    res = d.run("вопрос")
    check("разбор не упал", res.status in ("no_consensus", "stuck"),
          res.status)
    check("незнакомое решение не выдано за согласие",
          res.status != "done", res.status)
    check("итог честно говорит об отсутствии согласия",
          "СОГЛАСИЕ НЕ ДОСТИГНУТО" in res.verdict, res.verdict[:80])
    check("разбор всё же остановился, а не крутился до предела",
          res.rounds <= 4, str(res.rounds))


def test_monologue_warning() -> None:
    section("Одинаковые стороны: предупреждение о монологе")
    td = tempfile.mkdtemp()
    st = Store(Path(td) / "d.db")
    same = "одна и та же роль"
    d = D.Debate(Side("m"), Side("m"), Arbiter("decides", 1), st,
                 stance_a=same, stance_b=same, rounds=2)
    warns: list[str] = []
    d.on_event = lambda k, v: warns.append(v.get("message", "")) \
        if k == "warn" else None
    d.run("вопрос")
    check("о монологе предупреждено",
          any("монолог" in w for w in warns), str(warns))


def test_context_compaction() -> None:
    section("Свёртка: контекст не растёт линейно")
    d, st = _debate(arb_kind="stall", rounds=10, arbiter_every=99)
    d.run("вопрос")
    sizes = [len(p) for p in d.a.seen]
    check("запросов было не меньше восьми", len(sizes) >= 8, str(len(sizes)))
    growth = sizes[-1] / max(sizes[1], 1)
    check(f"последний запрос не втрое больше второго ({growth:.1f}x)",
          growth < 3.0, f"{sizes[1]} -> {sizes[-1]}")
    check("в контексте есть свёрнутая история",
          "Коротко о пройденном" in d.a.seen[-1])
    check("последние реплики даны целиком",
          "ПОСЛЕДНИЕ РЕПЛИКИ" in d.a.seen[-1])


def test_empty_replies() -> None:
    section("Пустая реплика не выдаётся за участие")
    d, st = _debate(a_kind="empty", arb_kind="stall", rounds=3,
                    arbiter_every=99)
    res = d.run("вопрос")
    check("пустой ответ помечен прямо",
          any("не дала ответа" in t.text for t in res.turns
              if t.role == "a"),
          str([t.text[:40] for t in res.turns if t.role == "a"]))


def test_resume() -> None:
    section("Разбор переживает перезапуск")
    d, st = _debate(arb_kind="stall", rounds=2, arbiter_every=99)
    res = d.run("длинный вопрос")
    n_first = len(res.turns)
    check("первый заход дал реплики", n_first >= 4, str(n_first))

    d2 = D.Debate(Side("model-a"), Side("model-b"),
                  Arbiter("decides", 1), st, rounds=4, arbiter_every=1)
    res2 = d2.run("", resume=res.debate_id)
    check("продолжили ту же дискуссию", res2.debate_id == res.debate_id)
    check("прежние реплики подхвачены", len(res2.turns) > n_first,
          f"{n_first} -> {len(res2.turns)}")
    check("вопрос взят из базы, а не потерян",
          st.get_debate(res.debate_id)["question"] == "длинный вопрос")


def test_executor_has_tools() -> None:
    section("Исполнитель всегда получает инструменты для проверки")
    # Живой прогон показал: тема «посчитай 300000/5000» не распознаётся,
    # профиль не выбирается, и исполнитель приходил БЕЗ run_python —
    # отвечал «инструмента нет». Набор для проверки фактов обязан быть
    # гарантированным, а не зависеть от распознавания темы.
    import agent.cli as cli
    import agent.debate as dbt
    from agent.config import Config

    seen: list[list[str]] = []
    real_build = cli.build_agent
    real_init = dbt.Debate.__init__
    grabbed: dict = {}

    class FakeAgent:
        def run(self, task):
            class R:
                answer = "посчитано: 60"
            return R()

    def fake_build(cfg, **kw):
        seen.append(sorted(cfg.skills))
        return FakeAgent()

    def spy_init(self, *a, **kw):
        grabbed["executor"] = kw.get("executor")
        real_init(self, *a, **kw)
        self.max_rounds = 0          # разбор не начнётся: модели не нужны

    cli.build_agent = fake_build
    dbt.Debate.__init__ = spy_init
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(provider="ollama", model="m", workspace=td,
                         skills=["files", "memory"])   # python НЕТ намеренно
            cfg.db = str(Path(td) / "d.db")
            cli.cmd_debate(cfg, "вопрос", 1, 0.0, "", False, False)

            ex = grabbed.get("executor")
            check("исполнитель собран и передан в разбор", ex is not None)
            if ex:
                ex("посчитай 300000 / 5000")
                check("тема не распозналась, но python подключён",
                      seen and "python" in seen[-1], str(seen[-1:]))
                check("files тоже на месте",
                      seen and "files" in seen[-1], str(seen[-1:]))
                check("исходный конфиг не испорчен",
                      "python" not in cfg.skills, str(cfg.skills))
    finally:
        cli.build_agent = real_build
        dbt.Debate.__init__ = real_init


# ═══════════════════════ протокол разбора ═══════════════════════════
def test_protocol_basic() -> None:
    section("Протокол: собирается из записей, ничего не выдумывая")
    from agent.protocol import DISCLAIMER, build_protocol

    d, st = _debate(a_kind="fact", arb_kind="executor", rounds=6,
                    executor=lambda t: "в файле 7 строк")
    res = d.run("сколько строк в data.txt")
    text = build_protocol(st, res.debate_id)

    check("есть заголовок с номером", f"№{res.debate_id}" in text)
    check("вопрос на месте", "сколько строк" in text)
    check("заключение арбитра включено",
          "## Заключение" in text and "семь" in text.lower(), text[:200])
    check("проверенные факты выделены отдельно",
          "## Что проверено инструментами" in text and "7 строк" in text)
    check("сказано, что только это и проверено",
          "не рассуждением" in text)
    check("позиции обеих сторон приведены",
          "Сторона A" in text and "Сторона B" in text)
    check("модели названы", "model-a" in text and "model-b" in text)
    check("решения арбитра перечислены", "## Решения арбитра" in text)
    check("дисклеймер о происхождении на месте",
          DISCLAIMER[:40] in text, "читатель должен знать, что это не "
                                   "экспертное заключение")
    check("это markdown, а не каша", text.startswith("# Протокол"))


def test_protocol_no_consensus() -> None:
    section("Протокол: расхождение не превращается в согласие")
    from agent.protocol import build_protocol

    d, st = _debate(arb_kind="stall", rounds=3, arbiter_every=99)
    res = d.run("спорный вопрос")
    text = build_protocol(st, res.debate_id)

    check("итог честно назван",
          "СОГЛАСИЕ НЕ ДОСТИГНУТО" in text, text[:300])
    check("бодрого заключения не появилось",
          "согласие достигнуто" not in text.lower().replace(
              "согласие не достигнуто", ""))
    check("обе позиции всё равно приведены",
          "Сторона A" in text and "Сторона B" in text)
    check("сказано, что проверок не было",
          "Ничего не проверялось" in text, "иначе читатель решит, что "
                                           "выводы подтверждены")


def test_protocol_sections() -> None:
    section("Протокол: гипотезы и возражения разделены")
    from agent.protocol import _disagreements, _hypotheses

    turns = [
        {"role": "a", "round": 1, "text": "Хватит SQLite. [ГИПОТЕЗА] "
                                          "нагрузка не вырастет."},
        {"role": "b", "round": 1, "text": "Возражаю: важен объём на "
                                          "пользователя."},
        {"role": "b", "round": 2, "text": "Принимаю, но нужен план "
                                          "переезда."},
        {"role": "a", "round": 2, "text": "Согласен, однако сроки жмут."},
    ]
    dis = _disagreements(turns)
    check("настоящее возражение попало", any("Возражаю" in d for d in dis),
          str(dis))
    check("«принимаю, но…» НЕ попало в расхождения",
          not any("Принимаю" in d for d in dis), str(dis))
    check("«согласен, однако…» тоже не попало",
          not any("Согласен" in d for d in dis), str(dis))

    hyp = _hypotheses(turns)
    check("гипотеза вынесена отдельно",
          len(hyp) == 1 and "не вырастет" in hyp[0], str(hyp))
    check("автор гипотезы указан", hyp and hyp[0].startswith("A:"), str(hyp))


def test_protocol_summarize() -> None:
    section("Протокол: пересказ моделью не обязателен")
    from agent.protocol import build_protocol

    class Teller(BaseLLM):
        billable = False

        def _chat_once(self, messages, tools=None):
            return LLMReply(text="Стороны обсудили объём данных и сошлись "
                                 "на SQLite.", usage=Usage(100, 30))

    class Broken(BaseLLM):
        billable = False

        def _chat_once(self, messages, tools=None):
            from agent.llm.base import LLMError
            raise LLMError("модель недоступна")

    d, st = _debate(after=1, rounds=3)
    res = d.run("вопрос")

    plain = build_protocol(st, res.debate_id)
    check("без модели раздела «Ход разбора» нет",
          "## Ход разбора" not in plain)

    told = build_protocol(st, res.debate_id, Teller("m"))
    check("с моделью появляется связный пересказ",
          "## Ход разбора" in told and "сошлись на SQLite" in told,
          told[:200])
    check("остальные разделы никуда не делись",
          "Позиции сторон" in told and "Заключение" in told)

    # Сбой модели не должен рушить протокол: он и без пересказа полезен.
    broken = build_protocol(st, res.debate_id, Broken("m"))
    check("сбой модели не ломает протокол",
          "## Заключение" in broken and "## Ход разбора" not in broken)


def test_protocol_missing() -> None:
    section("Протокол несуществующего разбора")
    from agent.protocol import build_protocol
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "d.db")
        try:
            build_protocol(st, 999)
            check("несуществующий разбор отвергнут", False, "собрался")
        except ValueError as exc:
            check("несуществующий разбор отвергнут", "999" in str(exc))


def test_protocol_saved_after_debate() -> None:
    section("Протокол пишется файлом сразу после разбора")
    import agent.cli as cli
    import agent.debate as dbt
    from agent.config import Config

    real_build = cli.build_agent
    real_init = dbt.Debate.__init__

    def spy_init(self, *a, **kw):
        real_init(self, *a, **kw)
        self.a, self.b = Side("model-a"), Side("model-b")
        self.arb = Arbiter("decides", 1)

    cli.build_agent = lambda cfg, **kw: None
    dbt.Debate.__init__ = spy_init
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(provider="ollama", model="m", workspace=td)
            cfg.db = str(Path(td) / "d.db")
            cli.cmd_debate(cfg, "нужен ли кэш", 2, 0.0, "", False, False)
            files = list(Path(td).glob("протокол-разбора-*.md"))
            check("файл протокола создан", len(files) == 1, str(files))
            if files:
                body = files[0].read_text(encoding="utf-8")
                check("в файле настоящий протокол",
                      body.startswith("# Протокол") and "нужен ли кэш" in body,
                      body[:80])
    finally:
        cli.build_agent = real_build
        dbt.Debate.__init__ = real_init


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ДИСКУССИИ: ловит поддакивание, топтание, тягуна")
    print("=" * 60)
    test_signature()
    test_basic_flow()
    test_rounds_limit()
    test_arbiter_stalling()
    test_broken_arbiter()
    test_repetition()
    test_agreement_triggers_arbiter()
    test_budget()
    test_executor()
    test_executor_limits()
    test_unknown_decision()
    test_monologue_warning()
    test_context_compaction()
    test_empty_replies()
    test_resume()
    test_executor_has_tools()
    test_protocol_basic()
    test_protocol_no_consensus()
    test_protocol_sections()
    test_protocol_summarize()
    test_protocol_missing()
    test_protocol_saved_after_debate()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
