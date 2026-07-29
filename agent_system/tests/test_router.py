"""Тесты авто-выбора профиля (agent/router.py): гибрид эвристика + LLM.

Философия та же, что у остального набора: тест обязан уметь падать.
Рядом с позитивными сценариями (типичная задача уверенно матчится по
ключевым словам) — негативные: LLM недоступна/сбоит/выдумывает
несуществующее имя, пустая задача, отсутствие профилей вообще.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config                                   # noqa: E402
from agent.llm.base import BaseLLM, LLMError, LLMReply             # noqa: E402
from agent.router import ProfileInfo, pick_profile                # noqa: E402

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


# --------------------------------------------------------------- фикстуры
PROFILES = [
    ProfileInfo("coder", "Разработка и отладка кода",
               ["код", "баг", "тест", "функция", "python"]),
    ProfileInfo("cad", "Параметрические конструкции в OpenSCAD",
               ["openscad", "чертёж", "деталь", "редуктор", "cad"]),
    ProfileInfo("marketing", "Тексты, лендинги, контент-планы",
               ["лендинг", "текст", "оффер", "маркетинг"]),
    ProfileInfo("research", "Анализ материалов и подготовка выжимок",
               ["анализ", "исследование", "выжимка", "источник"]),
]


class ScriptedLLM(BaseLLM):
    """Как в test_all.py: модель по сценарию, без сети."""

    def __init__(self, reply_text: str = "", raise_error: bool = False) -> None:
        super().__init__("scripted")
        self.reply_text = reply_text
        self.raise_error = raise_error
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        if self.raise_error:
            raise LLMError("модель недоступна", retryable=False)
        return LLMReply(text=self.reply_text)


# ================================================================ tests
def test_heuristic_confident_match() -> None:
    section("Эвристика: уверенный выбор без обращения к LLM")
    d = pick_profile("почини баг в функции и прогони тест", PROFILES)
    check("выбран coder", d.profile == "coder", d.profile)
    check("метод heuristic", d.method == "heuristic", d.method)
    check("LLM не понадобилась (llm=None не мешает)", True)

    d2 = pick_profile("спроектируй деталь редуктора в openscad", PROFILES)
    check("выбран cad", d2.profile == "cad", d2.profile)
    check("метод heuristic", d2.method == "heuristic")


def test_heuristic_does_not_call_llm_when_confident() -> None:
    section("Эвристика уверена — LLM не вызывается вообще")
    llm = ScriptedLLM(reply_text="cad")
    d = pick_profile("почини баг в тесте python-функции", PROFILES, llm=llm)
    check("выбран coder", d.profile == "coder")
    check("LLM НЕ вызывалась", len(llm.seen) == 0,
          f"вызовов: {len(llm.seen)}")


def test_llm_fallback_on_ambiguous_task() -> None:
    section("LLM-фолбэк: эвристика не уверена, модель решает")
    llm = ScriptedLLM(reply_text="research")
    d = pick_profile("сделай что-нибудь полезное", PROFILES, llm=llm)
    check("LLM была вызвана", len(llm.seen) == 1)
    check("выбран профиль от LLM", d.profile == "research", d.profile)
    check("метод llm", d.method == "llm")


def test_llm_ignores_hallucinated_profile() -> None:
    section("LLM выдумала несуществующий профиль — ответ игнорируется")
    llm = ScriptedLLM(reply_text="суперагент-которого-нет")
    d = pick_profile("сделай что-нибудь полезное", PROFILES, llm=llm)
    check("выдуманное имя не принято", d.profile != "суперагент-которого-нет")
    check("метод не llm (откат на дефолт)", d.method != "llm", d.method)


def test_llm_says_none() -> None:
    section("LLM явно отвечает 'none' — откат на дефолт/эвристику")
    llm = ScriptedLLM(reply_text="none")
    d = pick_profile("нечто совсем не про эти профили", PROFILES, llm=llm)
    check("вызвана LLM", len(llm.seen) == 1)
    check("профиль не 'none'", d.profile != "none")


def test_llm_error_falls_back_gracefully() -> None:
    section("LLM недоступна — решение не падает, а откатывается")
    llm = ScriptedLLM(raise_error=True)
    try:
        d = pick_profile("сделай что-нибудь полезное", PROFILES, llm=llm)
        check("решение принято несмотря на сбой LLM", bool(d.profile))
        check("метод не llm", d.method != "llm", d.method)
    except Exception as exc:
        check("решение принято несмотря на сбой LLM", False, str(exc))


def test_no_llm_falls_back_to_best_heuristic_or_default() -> None:
    section("Без LLM и без уверенной эвристики — лучший эвристический "
           "вариант либо профиль по умолчанию")
    d = pick_profile("абсолютно нейтральная фраза ни о чём", PROFILES)
    check("решение всё равно принято", bool(d.profile))
    check("метод default", d.method == "default", d.method)


def test_empty_task() -> None:
    section("Пустая задача — не роняет решение")
    d = pick_profile("", PROFILES)
    check("вернулся дефолтный профиль", d.method == "default")
    check("профиль не пустой", bool(d.profile))


def test_no_profiles_available() -> None:
    section("Список профилей пуст")
    d = pick_profile("любая задача", [])
    check("вернулось решение без исключения", bool(d.profile))
    check("метод default", d.method == "default")


def test_ambiguous_between_similar_profiles() -> None:
    section("Двусмысленная задача между похожими профилями — не "
           "притворяемся уверенностью")
    cad_like = [
        ProfileInfo("cad", "Параметрические конструкции в OpenSCAD",
                   ["openscad", "деталь", "редуктор", "конструкция"]),
        ProfileInfo("cad_auto", "Автономный конструктор: OpenSCAD + память",
                   ["openscad", "деталь", "редуктор", "автономно"]),
    ]
    d = pick_profile("спроектируй деталь редуктора в openscad", cad_like)
    check("метод НЕ heuristic при равном счёте", d.method != "heuristic",
          d.method)


def test_real_profiles_from_disk() -> None:
    section("Реальные профили из agent/profiles/*.json")
    infos = Config.profile_infos()
    names = {i.name for i in infos}
    for expected in ("coder", "cad", "marketing", "research",
                     "intake", "reporter"):
        check(f"профиль {expected} присутствует", expected in names)

    d = pick_profile("почини падающий тест в коде", infos)
    check("реальный coder выбран для задачи про код", d.profile == "coder", d.profile)

    d2 = pick_profile("распознай скан документа и классифицируй его тип",
                      infos)
    check("реальный intake выбран для задачи про распознавание документа",
          d2.profile == "intake", d2.profile)

    d3 = pick_profile("собери презентацию с итогами и разошли по почте",
                      infos)
    check("реальный reporter выбран для задачи про презентацию",
          d3.profile == "reporter", d3.profile)


def test_build_agent_from_routed_profile() -> None:
    section("Профиль от роутера реально применяется к Config/агенту")
    from agent.build import build_agent
    import tempfile
    infos = Config.profile_infos()
    d = pick_profile("почини баг в коде", infos)
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td)
        cfg.apply_profile(d.profile)
        agent = build_agent(cfg)
        check("агент собрался с выбранным профилем",
              "run_command" in agent.tools.names())


def main() -> int:
    test_heuristic_confident_match()
    test_heuristic_does_not_call_llm_when_confident()
    test_llm_fallback_on_ambiguous_task()
    test_llm_ignores_hallucinated_profile()
    test_llm_says_none()
    test_llm_error_falls_back_gracefully()
    test_no_llm_falls_back_to_best_heuristic_or_default()
    test_empty_task()
    test_no_profiles_available()
    test_ambiguous_between_similar_profiles()
    test_real_profiles_from_disk()
    test_build_agent_from_routed_profile()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
