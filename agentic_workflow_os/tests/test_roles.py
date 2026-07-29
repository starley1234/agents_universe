"""Тесты ролей: ход Исполнителя, разбор вердикта Критика, решения Контролёра.

Заглушка стоит ровно в одном месте — на месте самой модели (StubLLM со
сценарием ответов). Всё остальное настоящее: реальные инструменты,
реальный разбор протокола, реальная логика решений. Это позволяет
проверять то, ради чего среда существует: что она делает с ПЛОХИМИ
ответами модели — сломанным JSON, пустым текстом, бесконечными вызовами
инструментов, отказом провайдера.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import check, check_raises, section, summary          # noqa: E402
from awos.config import Config                                     # noqa: E402
from awos.llm.base import BaseLLM, LLMError, Reply, Usage          # noqa: E402
from awos.llm.stub import StubLLM                                  # noqa: E402
from awos.roles.critic import UNPARSED_SCORE, parse_verdict, review  # noqa: E402
from awos.roles.profile import (Profile, ProfileError,             # noqa: E402
                                default_profile, describe_profiles,
                                load_profile, resolve_profile)
from awos.roles.supervisor import Decision, decide, decide_with_llm  # noqa: E402
from awos.roles.worker import PauseForHuman, run_turn              # noqa: E402
from awos.tools.base import Tool, ToolError, ToolRegistry, Workspace  # noqa: E402
from awos.tools.builtin import file_tools                          # noqa: E402


class BrokenLLM(BaseLLM):
    """Провайдер, который всегда падает — проверяем устойчивость среды."""
    name = "broken"

    def __init__(self, retryable: bool = False) -> None:
        super().__init__("broken", retries=0)
        self.retryable = retryable

    def _chat_once(self, messages):
        raise LLMError("провайдер недоступен", retryable=self.retryable)


class FlakyLLM(BaseLLM):
    """Падает N раз, потом отвечает — проверяем retry."""
    name = "flaky"

    def __init__(self, failures: int) -> None:
        super().__init__("flaky", retries=3, retry_base=0.0)
        self.failures = failures
        self.attempts = 0

    def _chat_once(self, messages):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise LLMError("временный сбой", retryable=True)
        return Reply(text="получилось", usage=Usage(1, 1))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="awos_roles_"))
    ws = Workspace(tmp / "ws")

    section("Профили: разбор и границы полномочий")
    p = Profile.parse({"name": "p", "role": "worker", "system": "промпт",
                       "tools": ["read_file"], "temperature": 0.5})
    check("профиль разобран", p.name == "p" and p.role == "worker")
    check("температура разобрана", p.temperature == 0.5)
    check("инструменты разобраны", p.tools == ["read_file"])
    check("строка в tools превращается в список",
          Profile.parse({"name": "p", "system": "s", "tools": "read_file"}
                        ).tools == ["read_file"])
    check_raises("профиль без имени", ProfileError, Profile.parse,
                 {"system": "s"})
    check_raises("профиль без системного промпта", ProfileError, Profile.parse,
                 {"name": "p"})
    check_raises("неизвестная роль", ProfileError, Profile.parse,
                 {"name": "p", "system": "s", "role": "начальник"})
    check_raises("температура не число", ProfileError, Profile.parse,
                 {"name": "p", "system": "s", "temperature": "тепло"})

    cfg = Config(provider="openai_like", model="gpt-4o-mini", api_key="k",
                 temperature=0.2)
    kwargs = p.llm_kwargs(cfg)
    check("температура профиля перебивает конфиг", kwargs["temperature"] == 0.5)
    check("ключ берётся из конфига среды", kwargs["api_key"] == "k",
          "профиль не может принести свой ключ")
    plain = Profile.parse({"name": "q", "system": "s"})
    check("без температуры берётся конфиг",
          plain.llm_kwargs(cfg)["temperature"] == 0.2)
    stub_kwargs = Profile.parse({"name": "s", "system": "s", "provider": "stub"}
                                ).llm_kwargs(cfg)
    check("для stub не тащим сетевые параметры",
          "base_url" not in stub_kwargs and "api_key" not in stub_kwargs)

    section("Профили: встроенные и запасные")
    for role in ("worker", "critic", "supervisor"):
        d = default_profile(role)
        check(f"встроенный профиль роли {role}", d.role == role and bool(d.system))
    check("неизвестная роль -> worker", default_profile("нет").role == "worker")
    check("пустое имя -> встроенный", resolve_profile("", "critic").role == "critic")
    check("отсутствующий профиль -> встроенный, без падения",
          resolve_profile("нет_такого", "worker").name == "default_worker",
          "среда обязана работать без единого JSON-файла")
    builtin = describe_profiles()
    check("встроенные профили поставки есть", len(builtin) >= 5)
    broken = [b for b in builtin if "error" in b]
    check("все встроенные профили валидны", not broken,
          "; ".join(f"{b['name']}: {b['error']}" for b in broken))
    check("роли встроенных профилей известны",
          {b["role"] for b in builtin} <= {"worker", "critic", "supervisor"})

    section("Исполнитель: простой ход без инструментов")
    llm = StubLLM(scripted=["Готовый результат"])
    r = run_turn(llm, "система", "задача")
    check("текст получен", r.text == "Готовый результат")
    check("причина остановки done", r.stopped_by == "done")
    check("инструменты не вызывались", r.tool_calls == 0)
    check("расход учтён", r.usage.tokens_in > 0 and r.llm_calls == 1)

    section("Исполнитель: цикл с инструментами")
    reg = ToolRegistry()
    for t in file_tools(ws):
        reg.add(t)
    (ws.root / "data.txt").write_text("СОДЕРЖИМОЕ", encoding="utf-8")
    calls: list[str] = []
    llm = StubLLM(scripted=[
        '```tool\n{"tool": "read_file", "args": {"path": "data.txt"}}\n```',
        "Прочитал: СОДЕРЖИМОЕ",
    ])
    r = run_turn(llm, "система", "прочитай файл", tools=reg,
                 on_tool=lambda c, o, ok, e: calls.append(c.tool))
    check("инструмент вызван", calls == ["read_file"])
    check("результат инструмента попал модели",
          any("СОДЕРЖИМОЕ" in str(m.get("content")) for m in llm.seen[-1]))
    check("итоговый текст верный", r.text == "Прочитал: СОДЕРЖИМОЕ")
    check("счётчик вызовов", r.tool_calls == 1)

    section("Исполнитель: ошибки не роняют ход")
    llm = StubLLM(scripted=[
        '```tool\n{"tool": "read_file", "args": {"path": "нет.txt"}}\n```',
        "Файла нет, сообщаю об этом",
    ])
    r = run_turn(llm, "с", "з", tools=reg)
    check("ошибка инструмента возвращена модели",
          any("Ошибка инструмента" in str(m.get("content")) for m in llm.seen[-1]))
    check("ход завершился нормально", r.stopped_by == "done")

    llm = StubLLM(scripted=[
        '```tool\n{"tool": "read_file", "args": "не объект"}\n```',
        "Исправился, вот ответ",
    ])
    r = run_turn(llm, "с", "з", tools=reg)
    check("нарушение протокола объяснено модели",
          any("Ошибка формата" in str(m.get("content")) for m in llm.seen[-1]))
    check("после исправления ход завершён", r.text == "Исправился, вот ответ")

    def boom(**kwargs):
        raise RuntimeError("внутренняя поломка инструмента")
    reg.add(Tool("boom", "падает", {}, boom))
    llm = StubLLM(scripted=['```tool\n{"tool": "boom", "args": {}}\n```',
                            "Инструмент сломан, работаю без него"])
    r = run_turn(llm, "с", "з", tools=reg)
    check("необработанное падение инструмента не роняет ход",
          r.stopped_by == "done")
    check("текст падения ушёл модели",
          any("упал" in str(m.get("content")) for m in llm.seen[-1]))

    llm = StubLLM(scripted=[
        '```tool\n{"tool": "read_file", "args": {"wrong": 1}}\n```',
        "Понял аргументы",
    ])
    r = run_turn(llm, "с", "з", tools=reg)
    check("неверные аргументы объяснены",
          any("Неверные аргументы" in str(m.get("content")) or
              "Ошибка инструмента" in str(m.get("content"))
              for m in llm.seen[-1]))

    section("Исполнитель: лимит вызовов инструментов")
    llm = StubLLM(rule=lambda m: '```tool\n{"tool": "read_file", '
                                 '"args": {"path": "data.txt"}}\n```')
    r = run_turn(llm, "с", "з", tools=reg, max_tool_steps=3)
    check("лимит соблюдён", r.tool_calls == 3, f"вызовов: {r.tool_calls}")
    check("причина остановки — лимит", r.stopped_by == "tool_limit")
    check("причина объяснена в detail", "лимит" in r.detail.lower())

    section("Исполнитель: сбой модели")
    r = run_turn(BrokenLLM(), "с", "з")
    check("сбой модели помечен", r.stopped_by == "llm_error")
    check("текст ошибки сохранён", "недоступен" in r.detail)
    check("исключение наружу не летит", isinstance(r.text, str))

    flaky = FlakyLLM(failures=2)
    r = run_turn(flaky, "с", "з")
    check("retry срабатывает на временном сбое", r.text == "получилось")
    check("попыток было три", flaky.attempts == 3)

    section("Исполнитель: опасный инструмент требует человека")
    danger = ToolRegistry()
    danger.add(Tool("shell", "опасный", {"command": "к"},
                    lambda command="": "выполнено", dangerous=True))
    llm = StubLLM(scripted=['```tool\n{"tool": "shell", "args": {"command": "rm -rf /"}}\n```'])
    raised = False
    try:
        run_turn(llm, "с", "з", tools=danger, confirm=lambda call: False)
    except PauseForHuman as pause:
        raised = True
        check("в паузе передан сам вызов", pause.call.tool == "shell")
        check("в паузе есть аргументы", pause.call.args["command"] == "rm -rf /")
    check("опасный вызов остановлен до выполнения", raised)

    llm = StubLLM(scripted=['```tool\n{"tool": "shell", "args": {"command": "ls"}}\n```',
                            "готово"])
    r = run_turn(llm, "с", "з", tools=danger, confirm=lambda call: True)
    check("разрешённый опасный вызов выполняется", r.tool_calls == 1)

    section("Критик: разбор вердикта")
    v = parse_verdict('{"score": 0.9, "verdict": "accept", "issues": [], '
                      '"summary": "хорошо"}')
    check("чистый JSON разобран", v.score == 0.9 and v.verdict == "accept")
    check("флаг parsed выставлен", v.parsed is True)
    v = parse_verdict('Вот разбор:\n```json\n{"score": 0.4, "verdict": "reject", '
                      '"issues": ["выдуманный факт"]}\n```\nКонец.')
    check("JSON в ограде с текстом вокруг", v.score == 0.4)
    check("список замечаний разобран", v.issues == ["выдуманный факт"])
    v = parse_verdict('{"score": 0.5, "verdict": "revise", "issues": ["а",],}')
    check("висящая запятая прощается", v.parsed is True and v.score == 0.5)
    v = parse_verdict('{"score": 0.85}')
    check("вердикт выводится из оценки при отсутствии поля",
          v.verdict == "accept")
    check("низкая оценка -> reject",
          parse_verdict('{"score": 0.2}').verdict == "reject")
    v = parse_verdict('{"score": 3, "verdict": "accept"}')
    check("оценка вне диапазона зажимается", v.score == 1.0)
    v = parse_verdict('{"score": "плохо"}')
    check("нечисловая оценка -> нейтральная", v.score == UNPARSED_SCORE)
    v = parse_verdict("Мне всё понравилось, отличная работа!")
    check("текст без JSON помечен как неразобранный", v.parsed is False)
    check("неразобранный вердикт нейтрален", v.score == UNPARSED_SCORE,
          "иначе кривой ответ Критика либо пропустит брак, либо завалит шаг")
    check("неразобранный вердикт требует доработки", v.verdict == "revise")
    v = parse_verdict('{"issues": "одна строка вместо списка"}')
    check("строка вместо списка замечаний принимается",
          v.issues == ["одна строка вместо списка"])
    check("feedback собирает замечания",
          "выдуманный факт" in parse_verdict(
              '{"score":0.3,"issues":["выдуманный факт"],"summary":"плохо"}'
          ).feedback())

    section("Критик: сбой модели не ломает шаг")
    v = review(BrokenLLM(), "система критика", "задача", "результат")
    check("сбой Критика -> нейтральная оценка", v.score == UNPARSED_SCORE)
    check("сбой Критика виден в замечаниях",
          any("недоступен" in i for i in v.issues))
    check("сбой Критика не кидает исключение", v.parsed is False)

    v = review(StubLLM(), "критик", "задача", "результат")
    check("stub-критик отдаёт валидный вердикт", v.parsed is True)

    section("Контролёр: решения среды (без модели)")
    good = parse_verdict('{"score": 0.9, "verdict": "accept"}')
    d = decide(good, min_score=0.7, revisions_left=2, hitl_enabled=False)
    check("хорошая работа принимается", d.decision == "accept")
    check("решение принято средой", d.by == "engine")

    bad = parse_verdict('{"score": 0.3, "verdict": "revise"}')
    d = decide(bad, min_score=0.7, revisions_left=2, hitl_enabled=False)
    check("плохая работа возвращается", d.decision == "revise")
    check("в причине указан остаток доработок", "осталось доработок" in d.reason)

    d = decide(bad, min_score=0.7, revisions_left=0, hitl_enabled=False)
    check("доработки исчерпаны без HITL -> провал", d.decision == "fail",
          "тихо принять брак нельзя")
    d = decide(bad, min_score=0.7, revisions_left=0, hitl_enabled=True)
    check("доработки исчерпаны с HITL -> человек", d.decision == "escalate")

    reject = parse_verdict('{"score": 0.95, "verdict": "reject"}')
    d = decide(reject, min_score=0.7, revisions_left=1, hitl_enabled=False)
    check("явный reject важнее высокой оценки", d.decision == "revise",
          "иначе шаг с verdict=reject проскочит по числу")

    edge = parse_verdict('{"score": 0.7, "verdict": "revise"}')
    d = decide(edge, min_score=0.7, revisions_left=1, hitl_enabled=False)
    check("оценка ровно на пороге принимается", d.decision == "accept")

    section("Контролёр: модель")
    llm = StubLLM(scripted=['{"decision": "accept", "reason": "годится"}'])
    d = decide_with_llm(llm, "с", "задача", "результат", bad, revisions_left=2,
                        min_score=0.7, hitl_enabled=False)
    check("решение модели принято", d.decision == "accept" and d.by == "llm")
    check("причина от модели сохранена", d.reason == "годится")

    llm = StubLLM(scripted=['{"decision": "revise", "reason": "плохо"}'])
    d = decide_with_llm(llm, "с", "з", "р", bad, revisions_left=0,
                        min_score=0.7, hitl_enabled=True)
    check("revise без остатка доработок -> эскалация", d.decision == "escalate")
    d = decide_with_llm(StubLLM(scripted=['{"decision": "revise"}']), "с", "з",
                        "р", bad, revisions_left=0, min_score=0.7,
                        hitl_enabled=False)
    check("revise без доработок и без HITL -> провал", d.decision == "fail")

    llm = StubLLM(scripted=["не понял вопроса"])
    d = decide_with_llm(llm, "с", "з", "р", bad, revisions_left=2,
                        min_score=0.7, hitl_enabled=False)
    check("неразобранный ответ -> откат на правило среды",
          d.decision == "revise")
    check("откат объяснён в причине", "не разобран" in d.reason)

    d = decide_with_llm(BrokenLLM(), "с", "з", "р", good, revisions_left=2,
                        min_score=0.7, hitl_enabled=False)
    check("сбой Контролёра -> откат на правило среды", d.decision == "accept")
    check("сбой Контролёра объяснён", "недоступен" in d.reason)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return summary("Роли")


if __name__ == "__main__":
    raise SystemExit(main())
