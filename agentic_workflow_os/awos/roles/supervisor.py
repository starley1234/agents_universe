"""Роль Контролёр: финальное решение по шагу — принять или вернуть.

ПОЧЕМУ КОНТРОЛЁР ОТДЕЛЁН ОТ КРИТИКА. Критик заинтересован находить
дефекты — это его работа, и он находит их всегда, включая косметические.
Если разрешить Критику самому решать судьбу шага, конвейер уходит в
бесконечную шлифовку: каждая новая версия снова получает три замечания.
Контролёр отвечает на другой вопрос: «мешают ли найденные дефекты
использовать результат?» — и у него есть право принять работу с
известными недостатками.

ПОЧЕМУ КОНТРОЛЁРОМ ЧАСТО ВЫСТУПАЕТ САМА СРЕДА. Вызов третьей модели
стоит денег и времени, а в типовом случае решение тривиально: оценка
выше порога — принять, ниже — вернуть, доработки исчерпаны —
эскалировать. Поэтому `decide()` работает БЕЗ модели, а модельный
Контролёр (`decide_with_llm`) подключается только там, где шаг объявил
профиль supervisor: спорные случаи, дорогая цена ошибки, требование
объяснить решение человеческим языком.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts import SUPERVISOR_MARKER
from ..llm.base import BaseLLM, LLMError, Usage
from .critic import Verdict, _extract_json

#: Что среда решила сделать с результатом шага.
DECISIONS = ("accept", "revise", "escalate", "fail")


@dataclass
class Decision:
    decision: str = "accept"
    reason: str = ""
    by: str = "engine"               # engine | llm | human
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "reason": self.reason, "by": self.by}


def decide(verdict: Verdict, *, min_score: float, revisions_left: int,
           hitl_enabled: bool) -> Decision:
    """Решение среды без обращения к модели — детерминированное и бесплатное.

    Порядок проверок важен: сначала явный отказ Критика, потом порог
    оценки, и только потом остаток доработок. Иначе шаг с verdict=reject
    и последней доработкой ушёл бы в accept просто потому, что чинить
    больше нечем.
    """
    if verdict.verdict == "accept" and verdict.score >= min_score:
        return Decision("accept", verdict.summary or "Оценка выше порога.")

    if verdict.score >= min_score and verdict.verdict != "reject":
        return Decision("accept",
                        f"Оценка {verdict.score:.2f} >= порога {min_score:.2f}.")

    if revisions_left > 0:
        return Decision(
            "revise",
            f"Оценка {verdict.score:.2f} ниже порога {min_score:.2f}; "
            f"осталось доработок: {revisions_left}.")

    # Доработки исчерпаны. Есть человек — отдаём ему; нет — честный провал
    # шага. Тихо принять брак было бы худшим из вариантов: платформа
    # обещает контроль качества, а не видимость контроля.
    if hitl_enabled:
        return Decision("escalate",
                        "Доработки исчерпаны, качество ниже порога — "
                        "нужно решение человека.")
    return Decision("fail",
                    f"Доработки исчерпаны, оценка {verdict.score:.2f} так и "
                    f"не достигла порога {min_score:.2f}.")


SUPERVISOR_TEMPLATE = """Задача шага:
---
{task}
---

Результат Исполнителя:
---
{output}
---

Разбор Критика:
---
{critic}
---

Осталось доработок: {revisions_left}.

Верни ТОЛЬКО JSON:
{{"decision": "accept" | "revise", "reason": "коротко, по существу"}}

accept — результат можно использовать, даже если он неидеален.
revise — дефект мешает использовать результат по назначению."""

# См. пояснение к такой же проверке в roles/critic.py.
assert SUPERVISOR_MARKER in SUPERVISOR_TEMPLATE, (
    "SUPERVISOR_MARKER разошёлся с SUPERVISOR_TEMPLATE — см. awos/contracts.py")


def decide_with_llm(llm: BaseLLM, system: str, task: str, output: str,
                    verdict: Verdict, *, revisions_left: int,
                    min_score: float, hitl_enabled: bool) -> Decision:
    """Контролёр-модель. При сбое — откат на решение среды, а не отказ."""
    critic_text = json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2)
    prompt = SUPERVISOR_TEMPLATE.format(task=task, output=output,
                                        critic=critic_text,
                                        revisions_left=revisions_left)
    try:
        reply = llm.chat([{"role": "system", "content": system},
                          {"role": "user", "content": prompt}])
    except LLMError as exc:
        fallback = decide(verdict, min_score=min_score,
                          revisions_left=revisions_left,
                          hitl_enabled=hitl_enabled)
        fallback.reason = f"Контролёр недоступен ({exc}); {fallback.reason}"
        return fallback

    data = _extract_json(reply.text or "") or {}
    raw = str(data.get("decision", "") or "").strip().lower()
    reason = str(data.get("reason", "") or "").strip()

    if raw not in ("accept", "revise"):
        # Не разобрали — не гадаем. Правило среды детерминированно и
        # объяснимо, а «наверное, он имел в виду accept» — нет.
        fallback = decide(verdict, min_score=min_score,
                          revisions_left=revisions_left,
                          hitl_enabled=hitl_enabled)
        fallback.reason = ("Ответ Контролёра не разобран; "
                           + fallback.reason)
        fallback.usage = reply.usage
        return fallback

    if raw == "revise" and revisions_left <= 0:
        decision = "escalate" if hitl_enabled else "fail"
        return Decision(decision,
                        reason or "Контролёр вернул работу, но доработки "
                                  "исчерпаны.", by="llm", usage=reply.usage)

    return Decision(raw, reason or "Решение Контролёра.", by="llm",
                    usage=reply.usage)
