"""Оркестратор: решает после каждого шага, что делать дальше.

Чем отличается от того, что было. Планировщик составлял план ОДИН раз,
до начала работы, и назначал исполнителей заранее. Дальше система шла
по списку механически: пункт выполнен — берём следующий, что бы в нём
ни оказалось. Знание, добытое на третьем шаге, никак не влияло на
четвёртый.

Оркестратор смотрит на РЕЗУЛЬТАТ каждого шага и решает:

  дальше        — план в порядке, работаем как задумано;
  сменить агента — пункт не тому исполнителю (выяснилось по ходу);
  добавить шаг  — вскрылась работа, которой не было в плане;
  проверить     — результат сомнителен, нужен второй агент на сверку;
  пропустить    — пункт потерял смысл после того, что уже узнали;
  закончить     — цель достигнута, остальное лишнее.

Два принципа, на которых всё держится:

  РЕШАЕТ МОДЕЛЬ, НО В РАМКАХ. Что делать дальше — суждение, правилами
  его не заменить. Но каждое решение ограничено: сменить агента можно
  дважды на пункт, добавить — не больше трёх шагов за раз, всего не
  больше половины исходного плана. Без рамок оркестратор бесконечно
  добавляет работу и никогда не заканчивает.

  ВМЕШИВАЕТСЯ НЕ ВСЕГДА. Спрашивать модель после каждого шага дорого и
  бессмысленно: когда всё идёт по плану, решение всегда «дальше».
  Поэтому есть триггеры — шаг провалился, результат подозрительно
  пустой, агент топчется, пройдена треть плана.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm.base import BaseLLM, LLMError

#: Сколько раз можно переназначить исполнителя одному пункту. Больше —
#: значит, дело не в исполнителе, а в самом пункте.
MAX_REASSIGN = 2

#: Сколько шагов можно добавить за одно решение.
MAX_ADD_AT_ONCE = 3

#: Сколько шагов можно добавить за весь прогон, в долях исходного плана.
#: Иначе оркестратор растит работу быстрее, чем она выполняется.
GROWTH_LIMIT = 1.0

#: Короче этого результат считается подозрительным: агент отчитался,
#: но, похоже, ничего не сделал.
THIN_RESULT = 40

DECIDE = """Ты оркестратор. Работа идёт по плану, ты решаешь, что делать
дальше после только что законченного шага.

ЦЕЛЬ: {goal}

ТОЛЬКО ЧТО ЗАКОНЧЕН ШАГ: #{task_id} {task}
  исполнитель: {who}
  чем кончилось: {status}
  результат: {result}

ПЛАН ЦЕЛИКОМ:
{plan}

{facts}

Почему тебя спросили: {reason}
{limits}

Ответь ТОЛЬКО валидным JSON без пояснений:
{{"решение": "дальше|сменить|добавить|проверить|пропустить|закончить",
  "почему": "одной фразой",
  "кто": "имя исполнителя — для «сменить» и «проверить»",
  "шаги": [{{"что": "...", "кто": "...", "после": [номера]}}],
  "пункты": [номера — для «пропустить»]}}

Когда что выбирать:
- «дальше» — план в порядке. Выбирай его, если нет ясной причины
  вмешаться. Лишние вмешательства растягивают работу.
- «сменить» — шаг провалился, потому что достался не тому исполнителю.
  В «кто» — подходящий из списка ниже. Шаг вернётся в работу.
- «добавить» — вскрылась работа, без которой цель не достигнуть. Только
  то, что действительно необходимо: не больше {max_add} шагов.
- «проверить» — результат вызывает сомнения, нужен второй агент, чтобы
  сверить. В «кто» — кто проверит.
- «пропустить» — пункты потеряли смысл после того, что уже выяснилось.
- «закончить» — цель достигнута, оставшиеся пункты не нужны.

ИСПОЛНИТЕЛИ:
{profiles}"""


@dataclass
class Decision:
    action: str = "дальше"
    why: str = ""
    who: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    items: list[int] = field(default_factory=list)

    def explain(self) -> str:
        head = {
            "дальше": "продолжаем по плану",
            "сменить": f"передать «{self.who}»",
            "добавить": f"добавить шагов: {len(self.steps)}",
            "проверить": f"сверить результат — «{self.who}»",
            "пропустить": f"пропустить пунктов: {len(self.items)}",
            "закончить": "цель достигнута, останавливаемся",
        }.get(self.action, self.action)
        return f"{head}" + (f" — {self.why}" if self.why else "")


class Orchestrator:
    """Решает после шага. Без модели молчит и не мешает работать."""

    def __init__(self, llm: BaseLLM | None, profiles: dict[str, str],
                 every: int = 3, max_growth: int = 0) -> None:
        self.llm = llm
        self.profiles = profiles or {}
        self.every = max(1, every)
        self.max_growth = max_growth      # сколько шагов можно добавить
        self.added = 0
        self.reassigned: dict[int, int] = {}
        self.asked = 0
        self.unparsed = 0

    # ------------------------------------------------------------ повод
    def reason(self, task: dict[str, Any], result: str, status: str,
               done_count: int, stuck: bool) -> str:
        """Стоит ли спрашивать. Пустая строка — не стоит.

        Правилами решаем ТОЛЬКО момент вмешательства: он дёшев и
        воспроизводим. Само решение — за моделью.
        """
        if self.llm is None:
            return ""
        if status == "failed":
            return "шаг провалился"
        if stuck:
            return "агент топчется на месте"
        text = (result or "").strip()
        if len(text) < THIN_RESULT:
            return "результат подозрительно пустой"
        if done_count and done_count % self.every == 0:
            return f"пройдено шагов: {done_count}"
        return ""

    # ---------------------------------------------------------- решение
    def decide(self, goal: str, task: dict[str, Any], result: str,
               status: str, plan: list[dict[str, Any]], facts: list[str],
               reason: str) -> Decision:
        if self.llm is None:
            return Decision()
        limits = []
        if self.reassigned.get(task["id"], 0) >= MAX_REASSIGN:
            limits.append("Исполнителя этому шагу менять больше нельзя — "
                          "он уже менялся дважды.")
        if self.max_growth and self.added >= self.max_growth:
            limits.append("Добавлять шаги больше нельзя: план и так вырос "
                          "вдвое. Работай тем, что есть.")
        plan_text = "\n".join(
            f"  {'[x]' if t['status'] == 'done' else '[!]'
               if t['status'] == 'failed' else '[ ]'} "
            f"#{t['id']} {t['title']}"
            + (f" [{t['profile']}]" if t.get("profile") else "")
            for t in plan) or "  (пусто)"
        prompt = DECIDE.format(
            goal=goal, task_id=task["id"], task=task["title"],
            who=task.get("profile") or "не назначен",
            status=status, result=(result or "")[:900],
            plan=plan_text,
            facts=("ИЗВЕСТНО:\n" + "\n".join(f"- {f}" for f in facts[:6])
                   if facts else ""),
            reason=reason,
            limits=("ОГРАНИЧЕНИЯ: " + " ".join(limits)) if limits else "",
            max_add=MAX_ADD_AT_ONCE,
            profiles="\n".join(f"- {n}: {d}"
                               for n, d in self.profiles.items()))
        self.asked += 1
        try:
            reply = self.llm.chat([{"role": "user", "content": prompt}])
        except LLMError:
            return Decision(why="оркестратор недоступен")

        data = _json_block(reply.text or "")
        if data is None:
            # Не разобрали — работаем по плану. Молча «додумывать»
            # решение опаснее, чем просто не вмешаться.
            self.unparsed += 1
            return Decision(why="ответ оркестратора не разобран")
        return self._clean(data, task)

    # --------------------------------------------------------- проверка
    def _clean(self, data: dict[str, Any], task: dict[str, Any]) -> Decision:
        """Привести решение к допустимому. Ограничения — здесь, а не в
        промпте: модель их нарушает, а последствия необратимы."""
        raw = str(data.get("решение") or data.get("action") or "").lower()
        action = "дальше"
        for key in ("сменить", "добавить", "проверить", "пропустить",
                    "закончить", "дальше"):
            if key in raw:
                action = key
                break
        why = str(data.get("почему") or data.get("why") or "")[:200]
        who = str(data.get("кто") or data.get("who") or "").strip()

        if action in ("сменить", "проверить"):
            if who not in self.profiles:
                # Выдуманный исполнитель — не исполнитель.
                return Decision(why=f"назван неизвестный агент {who!r}")
            if action == "сменить":
                if self.reassigned.get(task["id"], 0) >= MAX_REASSIGN:
                    return Decision(why="исполнитель уже менялся дважды")
                if who == (task.get("profile") or ""):
                    return Decision(why="тот же исполнитель, смысла нет")

        steps: list[dict[str, Any]] = []
        if action == "добавить":
            if self.max_growth and self.added >= self.max_growth:
                return Decision(why="предел роста плана исчерпан")
            for item in (data.get("шаги") or data.get("steps") or [])[
                    :MAX_ADD_AT_ONCE]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("что") or item.get("title") or "").strip()
                if len(title) < 8:
                    continue
                w = str(item.get("кто") or item.get("profile") or "").strip()
                steps.append({"title": title,
                              "profile": w if w in self.profiles else "",
                              "needs": [n for n in
                                        (item.get("после") or [])
                                        if isinstance(n, int)]})
            if not steps:
                return Decision(why="добавлять оказалось нечего")
            if self.max_growth:
                steps = steps[:max(0, self.max_growth - self.added)]

        items = [n for n in (data.get("пункты") or data.get("items") or [])
                 if isinstance(n, int)]
        if action == "пропустить" and not items:
            return Decision(why="не сказано, что пропускать")

        return Decision(action, why, who, steps, items)

    # ------------------------------------------------------------ учёт
    def note_reassign(self, task_id: int) -> None:
        self.reassigned[task_id] = self.reassigned.get(task_id, 0) + 1

    def note_added(self, n: int) -> None:
        self.added += n

    def report(self) -> str:
        if not self.asked:
            return ""
        bits = [f"оркестратор вмешивался {self.asked} раз"]
        if self.added:
            bits.append(f"добавил шагов: {self.added}")
        if self.reassigned:
            bits.append(f"сменил исполнителя: {sum(self.reassigned.values())}")
        if self.unparsed:
            bits.append(f"не разобрано ответов: {self.unparsed}")
        return ", ".join(bits)


def _json_block(text: str) -> dict[str, Any] | None:
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(text[i:j + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
