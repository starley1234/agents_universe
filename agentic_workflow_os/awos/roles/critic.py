"""Роль Критик: разбор результата Исполнителя со СТРУКТУРИРОВАННЫМ вердиктом.

ПОЧЕМУ ВЕРДИКТ — JSON, А НЕ СВОБОДНЫЙ ТЕКСТ. Среда должна принять
машинное решение: пускать результат дальше, вернуть на доработку или
позвать человека. Свободный текст «в целом неплохо, но...» для этого
непригоден — придётся звать ещё одну модель, чтобы понять первую.
Поэтому Критик обязан вернуть объект с числовой оценкой и списком
дефектов, а среда сравнивает оценку с порогом.

ПОЧЕМУ РАЗБОР ОТВЕТА ТАКОЙ СНИСХОДИТЕЛЬНЫЙ. Модели регулярно
оборачивают JSON в ```json, дописывают «Вот мой разбор:» перед ним и
ставят запятую после последнего поля. Валить шаг из-за оформления —
худшее, что может сделать платформа: работа Исполнителя уже оплачена.
Поэтому `parse_verdict` вытаскивает объект из текста, чинит типовые
огрехи и лишь в безнадёжном случае возвращает «не разобрано» — с
осознанно НЕЙТРАЛЬНОЙ оценкой, чтобы неразборчивый Критик не блокировал
конвейер и не пропускал брак автоматически.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts import CRITIC_MARKER
from ..llm.base import BaseLLM, LLMError, Usage

#: Оценка, которую ставим, если вердикт не разобран. Ровно посередине:
#: при штатном пороге 0.7 это возврат на доработку (не пропускаем брак
#: молча), но не 0.0 — чтобы одна кривая реплика не отправляла шаг в
#: провал без единого шанса.
UNPARSED_SCORE = 0.5

_JSON_FENCE = re.compile(r"```(?:json)?\s*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


@dataclass
class Verdict:
    score: float = 0.0
    verdict: str = "revise"          # accept | revise | reject
    issues: list[str] = field(default_factory=list)
    summary: str = ""
    parsed: bool = True
    usage: Usage = field(default_factory=Usage)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "verdict": self.verdict,
                "issues": list(self.issues), "summary": self.summary,
                "parsed": self.parsed}

    def feedback(self) -> str:
        """Текст замечаний для доработки — то, что увидит Исполнитель."""
        lines = []
        if self.summary:
            lines.append(self.summary)
        for i, issue in enumerate(self.issues, 1):
            lines.append(f"{i}. {issue}")
        return "\n".join(lines) or "Критик не сформулировал замечаний."


CRITIC_TEMPLATE = """Задача, которую выполнял Исполнитель:
---
{task}
---

Результат Исполнителя:
---
{output}
---
{context}
Разбери результат и верни ТОЛЬКО JSON-объект, без пояснений вокруг:

{{
  "score": 0.0..1.0,          // насколько результат готов к использованию
  "verdict": "accept" | "revise" | "reject",
  "issues": ["конкретный дефект", "..."],
  "summary": "одно-два предложения по существу"
}}

Правила оценки:
- score >= 0.8 — можно использовать как есть;
- 0.5..0.8 — есть дефекты, но исправимые доработкой;
- < 0.5 — результат не решает задачу.
issues — только конкретика: что именно не так и где. Пустой список
допустим, если дефектов нет."""

# Контракт среды: маркер обязан присутствовать в шаблоне. Без этой
# проверки правка формулировки в шаблоне молча ломает опознание роли
# офлайн-провайдером и тестовыми двойниками — и обнаруживается это
# далеко от места правки. Проверка на импорте стоит наносекунды.
assert CRITIC_MARKER in CRITIC_TEMPLATE, (
    "CRITIC_MARKER разошёлся с CRITIC_TEMPLATE — см. awos/contracts.py")


def _extract_json(text: str) -> dict[str, Any] | None:
    for chunk in _fragments(text):
        chunk = _TRAILING_COMMA.sub(r"\1", chunk.strip())
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _fragments(text: str) -> list[str]:
    out: list[str] = []
    for m in _JSON_FENCE.finditer(text):
        out.append(m.group(1))
    # Плюс любые сбалансированные {...} — модель могла обойтись без ограды.
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
                start = -1
    return out


def parse_verdict(text: str) -> Verdict:
    data = _extract_json(text or "")
    if data is None:
        return Verdict(score=UNPARSED_SCORE, verdict="revise",
                       issues=["Критик вернул ответ, из которого не удалось "
                               "извлечь JSON-вердикт."],
                       summary="Вердикт не разобран.", parsed=False,
                       raw=text or "")
    try:
        score = float(data.get("score", UNPARSED_SCORE))
    except (TypeError, ValueError):
        score = UNPARSED_SCORE
    score = min(1.0, max(0.0, score))

    verdict = str(data.get("verdict", "") or "").strip().lower()
    if verdict not in ("accept", "revise", "reject"):
        # Модель не назвала вердикт словом — выводим его из оценки, это
        # надёжнее, чем считать отсутствие поля отказом.
        verdict = "accept" if score >= 0.8 else ("revise" if score >= 0.5
                                                 else "reject")

    raw_issues = data.get("issues", []) or []
    if isinstance(raw_issues, str):
        raw_issues = [raw_issues]
    issues = [str(i).strip() for i in raw_issues if str(i).strip()] \
        if isinstance(raw_issues, list) else []

    return Verdict(score=score, verdict=verdict, issues=issues,
                   summary=str(data.get("summary", "") or "").strip(),
                   parsed=True, raw=text or "")


def review(llm: BaseLLM, system: str, task: str, output: str,
           context: str = "") -> Verdict:
    """Один прогон Критика. Сбой модели — не приговор шагу.

    Если Критик недоступен, среда не имеет права ни принять работу
    молча, ни забраковать её: она сообщает о сбое проверки нейтральной
    оценкой, а решение остаётся за Контролёром (и, при HITL, за человеком).
    """
    ctx_block = f"\nДополнительный контекст:\n---\n{context}\n---\n" if context else ""
    prompt = CRITIC_TEMPLATE.format(task=task, output=output, context=ctx_block)
    try:
        reply = llm.chat([{"role": "system", "content": system},
                          {"role": "user", "content": prompt}])
    except LLMError as exc:
        return Verdict(score=UNPARSED_SCORE, verdict="revise",
                       issues=[f"Критик недоступен: {exc}"],
                       summary="Проверка не выполнена из-за сбоя модели.",
                       parsed=False)
    v = parse_verdict(reply.text)
    v.usage = reply.usage
    return v
