"""Маршрутизация моделей: дешёвая на рутину, сильная на сложное.

На длинном прогоне большинство шагов — рутина: прочитать файл, записать
факт, закрыть пункт плана. Гонять на них модель за 15 долларов за
миллион токенов расточительно, а на планировании и разборе ошибок
экономить нельзя — дешёвая модель там просто буксует.

Router — это обычный драйвер: снаружи он неотличим от одной модели,
внутри выбирает, к кому обратиться. Поэтому ядро агента о нём ничего
не знает и менять его не пришлось.

Как выбирается модель — по признакам самого запроса, без гадания:

  1. Явная пометка в задаче: [сложно] / [просто] — сильнее всего.
  2. Длина контекста: разговор перевалил за порог — берём сильную,
     дешёвые на длинном контексте теряют нить.
  3. Ошибки в последних результатах инструментов: если инструмент
     вернул ОШИБКУ, разбираться должна сильная.
  4. Иначе — дешёвая.

Учёт денег общий: usage складывается из обеих моделей, и в отчёте видно,
сколько ушло на каждую.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import BaseLLM, LLMReply, price_of

#: Разговор длиннее — задача уже не рутинная, дешёвая модель поплывёт.
LONG_CONTEXT = 12_000

#: Пометки, которыми можно задать модель прямо в тексте задачи.
FORCE_STRONG = re.compile(r"(?i)\[(сложно|hard|strong)\]")
FORCE_CHEAP = re.compile(r"(?i)\[(просто|easy|cheap)\]")

#: Признак того, что предыдущий шаг не удался.
ERROR_MARK = re.compile(r"(?i)\bОШИБКА\b|\bTraceback\b|\bFAILED?\b")


class Router(BaseLLM):
    """Два драйвера под одним лицом."""

    name = "router"

    def __init__(self, cheap: BaseLLM, strong: BaseLLM,
                 long_context: int = LONG_CONTEXT,
                 escalate_on_error: bool = True) -> None:
        # model — для отчётов; расход считаем по каждой модели отдельно
        super().__init__(f"{cheap.model} + {strong.model}")
        self.cheap = cheap
        self.strong = strong
        self.long_context = long_context
        self.escalate_on_error = escalate_on_error
        self.picks: dict[str, int] = {"cheap": 0, "strong": 0}
        self.reasons: dict[str, int] = {}
        self.billable = cheap.billable or strong.billable

    # ------------------------------------------------------------ выбор
    def choose(self, messages: list[dict[str, Any]]) -> tuple[BaseLLM, str]:
        """Какая модель и почему. Отдельным методом — чтобы проверять."""
        text = _flatten(messages)

        # 1. Явная воля человека или планировщика важнее эвристик.
        first_user = next((m for m in messages
                           if m.get("role") == "user"), None)
        head = str(first_user.get("content") or "") if first_user else ""
        if FORCE_STRONG.search(head):
            return self.strong, "помечено [сложно]"
        if FORCE_CHEAP.search(head):
            return self.cheap, "помечено [просто]"

        # 2. Длинный контекст.
        if len(text) > self.long_context:
            return self.strong, f"длинный контекст ({len(text):,} симв.)"

        # 3. Последние результаты инструментов с ошибкой.
        if self.escalate_on_error:
            tail = [m for m in messages if m.get("role") == "tool"][-3:]
            if any(ERROR_MARK.search(str(m.get("content") or ""))
                   for m in tail):
                return self.strong, "в последних шагах была ошибка"

        return self.cheap, "рутинный шаг"

    # ------------------------------------------------------------ вызов
    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None) -> LLMReply:
        llm, why = self.choose(messages)
        kind = "strong" if llm is self.strong else "cheap"
        self.picks[kind] += 1
        self.reasons[why] = self.reasons.get(why, 0) + 1
        reply = llm.chat(messages, tools)
        # Свой usage — всегда сумма обеих моделей. Не накапливаем сами:
        # вложенные драйверы уже посчитали, и сложение дало бы двойной учёт.
        self.usage = self.cheap.usage + self.strong.usage
        self.calls += 1
        self.retried = self.cheap.retried + self.strong.retried
        return reply

    def _chat_once(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> LLMReply:
        # Router не ходит в сеть сам: всё делают вложенные драйверы.
        return self.chat(messages, tools)

    # ------------------------------------------------------------ отчёт
    def cost(self) -> float | None:
        total = 0.0
        known = False
        for llm in (self.cheap, self.strong):
            if not llm.billable:
                continue
            p = price_of(llm.model)
            if p:
                known = True
                total += (llm.usage.prompt * p[0]
                          + llm.usage.completion * p[1]) / 1e6
        return total if known else None

    def spend_report(self) -> str:
        parts = []
        for kind, llm in (("дешёвая", self.cheap), ("сильная", self.strong)):
            k = "cheap" if kind == "дешёвая" else "strong"
            u = llm.usage
            p = price_of(llm.model) if llm.billable else None
            money = ""
            if p:
                money = f", ${(u.prompt * p[0] + u.completion * p[1]) / 1e6:.4f}"
            parts.append(f"{kind} {llm.model}: вызовов {self.picks[k]}, "
                         f"токенов {u.prompt + u.completion:,}{money}")
        why = "; ".join(f"{k} — {v}" for k, v in
                        sorted(self.reasons.items(), key=lambda x: -x[1]))
        saved = self._saved()
        tail = f"\nэкономия против только сильной: ~${saved:.4f}" if saved else ""
        return "\n".join(parts) + (f"\nвыбор: {why}" if why else "") + tail

    def _saved(self) -> float:
        """Сколько бы стоило, если бы всё гонялось на сильной."""
        ps = price_of(self.strong.model) if self.strong.billable else None
        pc = price_of(self.cheap.model) if self.cheap.billable else None
        if not ps or not pc:
            return 0.0
        u = self.cheap.usage
        as_strong = (u.prompt * ps[0] + u.completion * ps[1]) / 1e6
        as_is = (u.prompt * pc[0] + u.completion * pc[1]) / 1e6
        return max(0.0, as_strong - as_is)


def _flatten(messages: list[dict[str, Any]]) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.append(json.dumps(c, ensure_ascii=False))
        if m.get("tool_calls"):
            out.append(json.dumps(m["tool_calls"], ensure_ascii=False))
    return "\n".join(out)
