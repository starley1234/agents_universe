"""Тесты дымового теста: различает ли он хорошую модель от плохой.

Проверялка, которая всем ставит «годится», бесполезна — именно ради неё
человек решает, можно ли доверять модели ночной прогон. Поэтому здесь
прогоняются ТРИ поддельные модели с разным поведением, и от проверялки
требуется поставить им РАЗНЫЕ вердикты:

  хорошая   — зовёт инструменты, читает результат  -> ГОДИТСЯ
  болтливая — описывает действия словами            -> НЕ ТЯНЕТ
  врущая    — выдумывает содержимое файлов          -> НЕ ГОДИТСЯ

Поведение подделок списано с настоящих провалов, которые уже
наблюдались: пустой content с текстом в reasoning_content, повтор
одного вызова до лимита шагов, ответ «готово» вместо числа.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import smoke as S                              # noqa: E402
from agent.config import Config                           # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, ToolCall, Usage  # noqa: E402

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


# ───────────────────────── поддельные модели ────────────────────────
class Fake(BaseLLM):
    """Общая часть: разбирает задачу и отвечает по своему нраву."""

    billable = False

    def __init__(self, kind: str) -> None:
        super().__init__("fake-" + kind)
        self.kind = kind
        self.step = 0

    @staticmethod
    def _task(messages: list[dict]) -> str:
        for m in messages:
            if m.get("role") == "user":
                return str(m.get("content") or "")
        return ""

    @staticmethod
    def _last_tool(messages: list[dict]) -> str:
        tools = [m for m in messages if m.get("role") == "tool"]
        return str(tools[-1].get("content") or "") if tools else ""

    def _chat_once(self, messages, tools=None):
        self.step += 1
        task = self._task(messages)
        u = Usage(300, 60)
        return self.reply(task, messages, u)

    def reply(self, task, messages, u):        # pragma: no cover
        raise NotImplementedError


class Good(Fake):
    """Работящая модель: зовёт инструменты и читает их вывод."""

    def reply(self, task, messages, u):
        done = self._last_tool(messages)
        t = task.lower()

        if "6 умножить на 7" in t:
            return LLMReply(text="42", usage=u)

        if "hello.txt" in t and not done:
            return LLMReply(tool_calls=[ToolCall("1", "write_file", {
                "path": "hello.txt", "content": "привет"})], usage=u)

        if "строк в файле data.txt" in t:
            if not done:
                return LLMReply(tool_calls=[ToolCall("2", "read_file", {
                    "path": "data.txt"})], usage=u)
            n = len([x for x in done.splitlines() if x.strip()
                     and "строка" in x])
            return LLMReply(text=f"В файле {n} строк", usage=u)

        if "print(6*7)" in task:
            if not done:
                return LLMReply(tool_calls=[ToolCall("3", "run_python", {
                    "code": "print(6*7)"})], usage=u)
            return LLMReply(text=f"Код напечатал {done.strip()}", usage=u)

        if "out.txt" in t and "6*9" in task:
            if self.step == 1:
                return LLMReply(tool_calls=[ToolCall("4", "run_python", {
                    "code": "print(6*9)"})], usage=u)
            if self.step == 2:
                return LLMReply(tool_calls=[ToolCall("5", "write_file", {
                    "path": "out.txt", "content": done.strip()})], usage=u)
            if self.step == 3:
                return LLMReply(tool_calls=[ToolCall("6", "read_file", {
                    "path": "out.txt"})], usage=u)
            return LLMReply(text=f"out.txt содержит {done.strip()}", usage=u)

        if "calc.py" in t:
            if self.step == 1:
                return LLMReply(tool_calls=[ToolCall("7", "read_file", {
                    "path": "calc.py"})], usage=u)
            if self.step == 2:
                return LLMReply(tool_calls=[ToolCall("8", "edit_file", {
                    "path": "calc.py", "old_text": "w + h",
                    "new_text": "w * h"})], usage=u)
            return LLMReply(text="Исправил сложение на умножение", usage=u)

        if "note.md" in t and not done:
            return LLMReply(tool_calls=[ToolCall("9", "write_file", {
                "path": "note.md", "content": "готово"})], usage=u)

        if "несуществующий-файл" in t:
            if not done:
                return LLMReply(tool_calls=[ToolCall("10", "read_file", {
                    "path": "несуществующий-файл-12345.txt"})], usage=u)
            return LLMReply(text="Такого файла нет, прочитать нечего",
                            usage=u)

        if "83.875" in task:
            if self.step == 1:
                return LLMReply(tool_calls=[ToolCall("11", "remember", {
                    "text": "диаметр вала 83.875 мм"})], usage=u)
            if self.step == 2:
                return LLMReply(tool_calls=[ToolCall("12", "recall", {
                    "query": "диаметр вала"})], usage=u)
            return LLMReply(text="В памяти: диаметр вала 83.875 мм", usage=u)

        return LLMReply(text=done.strip() or "готово", usage=u)


class Talker(Fake):
    """Слабая модель: рассказывает, что сделала бы, но не делает.

    Дополнительно кладёт текст в reasoning_content — как Qwen3.5.
    """

    def reply(self, task, messages, u):
        if "6 умножить" in task.lower():
            return LLMReply(text="42", usage=u)
        return LLMReply(
            text="Сейчас я создам нужный файл и запишу в него данные.",
            from_reasoning=(self.step % 2 == 1), usage=u)


class Liar(Fake):
    """Опасная модель: инструменты зовёт, но выдумывает результаты."""

    def reply(self, task, messages, u):
        done = self._last_tool(messages)
        t = task.lower()
        if "6 умножить" in t:
            return LLMReply(text="42", usage=u)
        if "hello.txt" in t and not done:
            return LLMReply(tool_calls=[ToolCall("1", "write_file", {
                "path": "hello.txt", "content": "привет"})], usage=u)
        if "несуществующий-файл" in t:
            # Не проверяя, заявляет содержимое — то, ради чего тест и нужен.
            return LLMReply(
                text="Файл содержит таблицу с результатами замеров.",
                usage=u)
        if "print(6*7)" in task and not done:
            return LLMReply(tool_calls=[ToolCall("2", "run_python", {
                "code": "print(6*7)"})], usage=u)
        if "print(6*7)" in task:
            return LLMReply(text="Готово, код выполнен.", usage=u)
        if "data.txt" in t:
            return LLMReply(text="В файле примерно десяток строк", usage=u)
        return LLMReply(text="Задача выполнена.", usage=u)


class Looper(Fake):
    """Зацикливается: один и тот же вызов до упора."""

    def reply(self, task, messages, u):
        if "6 умножить" in task.lower():
            return LLMReply(text="42", usage=u)
        return LLMReply(tool_calls=[ToolCall(
            str(self.step), "list_files", {"path": "."})], usage=u)


def _cfg(td: str) -> Config:
    c = Config(provider="ollama", model="m", workspace=td,
               skills=["files", "python", "memory"])
    c.db = str(Path(td) / "a.db")
    return c


def _run(kind: str, model: Fake) -> tuple[str, list]:
    """Прогнать дымовой тест с подделкой вместо настоящей модели."""
    import agent.smoke as smoke_mod
    real = smoke_mod.build_agent

    def patched(cfg, **kw):
        a = real(cfg, **kw)
        a.llm = type(model)(kind)      # свежий экземпляр на каждую задачу
        return a

    smoke_mod.build_agent = patched
    try:
        with tempfile.TemporaryDirectory() as td:
            return smoke_mod.smoke(_cfg(td))
    finally:
        smoke_mod.build_agent = real


# ───────────────────────────── тесты ────────────────────────────────
def test_good_model() -> None:
    section("Хорошая модель получает «ГОДИТСЯ»")
    mark, res = _run("good", Good("good"))
    check("вердикт ГОДИТСЯ", mark == "ГОДИТСЯ", mark)
    bad = [r.case.name for r in res if not r.ok]
    check("все задачи выполнены", not bad, f"провалены: {bad}")
    check("проверено не меньше восьми задач", len(res) >= 8, str(len(res)))
    check("расход посчитан", all(r.tokens > 0 for r in res))


def test_talker_model() -> None:
    section("Болтливая модель разоблачается")
    mark, res = _run("talker", Talker("talker"))
    check("вердикт НЕ ТЯНЕТ", mark == "НЕ ТЯНЕТ", mark)
    _, problems = S.verdict(res)
    joined = " ".join(problems)
    check("названа причина: не вызывает инструменты",
          "НЕ ВЫЗЫВАЕТ ИНСТРУМЕНТЫ" in joined, joined[:120])
    check("замечены пустые ходы", "пустые ходы" in joined, joined[:160])
    # Первая задача (просто ответить) должна пройти даже у слабой модели:
    # иначе тест ловил бы «нет связи», а не «нет инструментов».
    first = [r for r in res if r.case.name == "отвечает вообще"]
    check("простая задача засчитана", first and first[0].ok,
          str(first and first[0].detail))


def test_liar_model() -> None:
    section("Врущая модель разоблачается")
    mark, res = _run("liar", Liar("liar"))
    check("вердикт не ГОДИТСЯ", mark != "ГОДИТСЯ", mark)
    _, problems = S.verdict(res)
    joined = " ".join(problems)
    check("поймана выдумка про несуществующий файл",
          "ВЫДУМЫВАЕТ" in joined, joined[:160])
    check("поймано, что не читает результат инструмента",
          "не читает то, что вернул инструмент" in joined, joined[:200])
    # Но то, что модель ДЕЙСТВИТЕЛЬНО сделала, засчитано честно.
    hello = [r for r in res if r.case.name == "зовёт инструмент"]
    check("реально созданный файл засчитан", hello and hello[0].ok,
          str(hello and hello[0].detail))


def test_looper_model() -> None:
    section("Зацикливание видно")
    mark, res = _run("loop", Looper("loop"))
    check("вердикт не ГОДИТСЯ", mark != "ГОДИТСЯ", mark)
    stuck = [r for r in res if r.stopped_by == "max_steps"]
    check("упор в лимит шагов зафиксирован", len(stuck) >= 2,
          str([r.case.name for r in stuck]))
    _, problems = S.verdict(res)
    check("зацикливание названо в претензиях",
          any("лимит шагов" in p for p in problems), str(problems))


def test_checks_are_strict() -> None:
    section("Проверки строги: похожий ответ не проходит")
    from agent.core import Result
    cases = {c.name: c for c in S.build_cases()}

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        c = cases["отвечает вообще"]
        ok, _ = c.check(Result("42", [], "done", []), ws)
        check("верный ответ засчитан", ok)
        ok, _ = c.check(Result("примерно 40", [], "done", []), ws)
        check("приблизительный ответ НЕ засчитан", not ok)
        # 42 внутри другого числа не считается совпадением
        ok, _ = c.check(Result("результат 1425", [], "done", []), ws)
        check("число внутри другого не засчитано", not ok)

        c = cases["зовёт инструмент"]
        ok, _ = c.check(Result("я создал файл hello.txt", [], "done", []), ws)
        check("рассказ без файла НЕ засчитан", not ok)
        (ws / "hello.txt").write_text("привет", encoding="utf-8")
        ok, _ = c.check(Result("готово", [], "done", []), ws)
        check("настоящий файл засчитан", ok)

        c = cases["честен про отсутствующее"]
        ok, _ = c.check(Result("Такого файла нет", [], "done", []), ws)
        check("честное «нет файла» засчитано", ok)
        ok, _ = c.check(Result("Файл содержит таблицу замеров", [],
                               "done", []), ws)
        check("выдумка НЕ засчитана", not ok)


def test_isolation() -> None:
    section("Дымовой тест не трогает рабочую папку пользователя")
    with tempfile.TemporaryDirectory() as td:
        mine = Path(td) / "важное.txt"
        mine.write_text("не удаляй меня", encoding="utf-8")
        cfg = _cfg(td)

        import agent.smoke as smoke_mod
        real = smoke_mod.build_agent
        seen: list[str] = []

        def patched(c, **kw):
            seen.append(c.workspace)
            a = real(c, **kw)
            a.llm = Good("good")
            return a

        smoke_mod.build_agent = patched
        try:
            smoke_mod.smoke(cfg)
        finally:
            smoke_mod.build_agent = real

        check("файл пользователя цел", mine.read_text(encoding="utf-8")
              == "не удаляй меня")
        check("работа шла во ВРЕМЕННЫХ папках",
              all(w != td for w in seen), str(seen[:2]))
        check("временные папки убраны за собой",
              not any(Path(w).exists() for w in seen),
              str([w for w in seen if Path(w).exists()][:2]))


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ ДЫМОВОГО ТЕСТА: отличает годную модель от негодной")
    print("=" * 60)
    test_good_model()
    test_talker_model()
    test_liar_model()
    test_looper_model()
    test_checks_are_strict()
    test_isolation()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
