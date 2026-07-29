"""Агент-Редактор: проверка формулировок требований (ТЗ п.3.2).

ЧТО ПРОВЕРЯЕТСЯ. Три свойства, которые в нормотворчестве по лётной
годности определяют, можно ли вообще доказать соответствие требованию:

  * ОДНОЗНАЧНОСТЬ — нет слов, допускающих разное прочтение
    («достаточный», «при необходимости», «как правило»);
  * ИЗМЕРИМОСТЬ — есть числовое значение с единицей измерения там, где
    речь о характеристике («высокая надёжность» недоказуема,
    «наработка на отказ не менее 10000 ч» — доказуема);
  * ПРОВЕРЯЕМОСТЬ — сформулировано так, что можно назначить метод
    подтверждения: есть субъект, модальный глагол и проверяемое условие.

ДВА РЕЖИМА, И ДЕТЕРМИНИРОВАННЫЙ — ОСНОВНОЙ.
`check_text()` работает БЕЗ модели: словари стоп-слов, поиск чисел с
единицами, структурные проверки. Результат воспроизводим, объясним и
бесплатен — его можно показать регулятору как формальный критерий.
LLM подключается опционально и только для ОДНОЙ задачи: предложить
переформулировку. Оценку она не ставит.

Почему так, а не «спросим модель, хорошее ли требование»: оценка,
меняющаяся от запуска к запуску, бесполезна как критерий приёмки, а
объяснить регулятору «модель посчитала» невозможно. Детерминированные
правила проверяемы и стабильны, а модель хороша там, где нужен язык.

ГЛАВНОЕ: НИ В ОДНОМ РЕЖИМЕ АГЕНТ НЕ МЕНЯЕТ ТЕКСТ. Он пишет оценку в
requirement.quality и создаёт предложение с diff «было/стало» (ТЗ п.6.2).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..llm.base import BaseLLM, LLMError
from .base import Agent, AgentReport
from .prompts import EDITOR_SYSTEM, EDITOR_TEMPLATE, extract_json

#: Двусмысленные слова: делают требование недоказуемым. Список
#: составлен по типовым замечаниям к формулировкам требований; каждая
#: запись — «слово: чем плохо».
VAGUE_WORDS = {
    "достаточн": "«достаточный» — не задан критерий достаточности",
    "при необходимости": "«при необходимости» — не определено, кто и как "
                         "определяет необходимость",
    "как правило": "«как правило» — допускает исключения, которые не заданы",
    "по возможности": "«по возможности» — требование становится необязательным",
    "минимальн возможн": "«минимально возможный» — нет измеримой границы",
    "оптимальн": "«оптимальный» — не задан критерий оптимальности",
    "надлежащ": "«надлежащий» — не задан стандарт",
    "соответствующ": "«соответствующий» без указания, чему именно",
    "приемлем": "«приемлемый» — не задан порог приемлемости",
    "высок": "«высокий» — качественная оценка вместо числовой",
    "низк": "«низкий» — качественная оценка вместо числовой",
    "быстр": "«быстрый» — нет числового значения времени",
    "надёжн": "«надёжный» без числового показателя надёжности",
    "надежн": "«надёжный» без числового показателя надёжности",
    "удобн": "«удобный» — субъективная оценка, не проверяется",
    "современн": "«современный» — привязка к моменту, не проверяется",
    "и т.д": "«и т.д.» — перечень не закрыт, состав требования неопределён",
    "и т.п": "«и т.п.» — перечень не закрыт, состав требования неопределён",
    "прочие": "«прочие» — состав не определён",
    "различн": "«различные» — состав не определён",
    "некотор": "«некоторый» — количество не определено",
}

#: Модальные глаголы: без них это описание, а не требование.
MODAL_WORDS = ("должен", "должна", "должно", "должны", "обязан", "обязана",
               "обязано", "обязаны", "не допускается", "запрещается",
               "shall", "must")

#: Слабые модальности: превращают требование в пожелание.
WEAK_MODALS = {
    "может": "«может» — это разрешение, а не требование; доказывать нечего",
    "желательно": "«желательно» — пожелание, не подлежит подтверждению",
    "рекомендуется": "«рекомендуется» — рекомендация, а не требование",
    "следует": "«следует» — слабая модальность, замените на «должен»",
    "should": "«should» — слабая модальность (рекомендация)",
}

#: Единицы измерения — признак измеримого требования.
UNITS = (r"мм|см|м|км|мкм|кг|г|т|н|кн|па|кпа|мпа|бар|атм|°с|к|"
         r"с|сек|мин|ч|час|часов|сут|гц|кгц|мгц|в|кв|а|ма|вт|квт|"
         r"%|дб|об/мин|м/с|км/ч|л|мл|м2|м3|мм2|мм3")

_NUMBER_WITH_UNIT = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:" + UNITS + r")\b", re.IGNORECASE)
_ANY_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
#: Признак того, что речь о характеристике, которую положено измерять.
_MEASURABLE_CONTEXT = re.compile(
    r"не\s+(?:менее|более|превыш|ниже|выше)|в\s+пределах|диапазон|"
    r"температур|давлени|масс|нагрузк|скорост|时间|время|наработк|ресурс|"
    r"вероятност|частот|напряжени|ток\b|мощност|погрешност|точност",
    re.IGNORECASE)


@dataclass
class QualityIssue:
    code: str
    message: str
    severity: str = "major"      # major | minor
    fragment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "severity": self.severity, "fragment": self.fragment}


@dataclass
class QualityResult:
    score: float
    issues: list[QualityIssue] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 3),
                "issues": [i.to_dict() for i in self.issues],
                "checks": self.checks}

    def brief(self) -> str:
        if not self.issues:
            return "Замечаний нет."
        return "; ".join(i.message for i in self.issues[:4])


def check_text(text: str) -> QualityResult:
    """Детерминированный разбор формулировки. Без модели, воспроизводимо."""
    original = (text or "").strip()
    low = original.lower()
    issues: list[QualityIssue] = []

    if not original:
        return QualityResult(0.0, [QualityIssue("empty", "Пустой текст требования")],
                             {"unambiguous": False, "measurable": False,
                              "verifiable": False, "atomic": False})

    # --- однозначность ---
    for marker, message in VAGUE_WORDS.items():
        idx = low.find(marker)
        if idx >= 0:
            issues.append(QualityIssue(
                "vague", message, "major",
                original[max(0, idx - 20):idx + len(marker) + 20].strip()))
    for marker, message in WEAK_MODALS.items():
        if re.search(rf"\b{re.escape(marker)}\b", low):
            issues.append(QualityIssue("weak_modal", message, "major", marker))

    # --- проверяемость ---
    has_modal = any(m in low for m in MODAL_WORDS)
    if not has_modal:
        issues.append(QualityIssue(
            "no_modal",
            "Нет модального глагола («должен», «обязан», «не допускается») — "
            "это описание, а не требование", "major"))

    # --- измеримость ---
    has_number_unit = bool(_NUMBER_WITH_UNIT.search(original))
    needs_measure = bool(_MEASURABLE_CONTEXT.search(original))
    if needs_measure and not has_number_unit:
        issues.append(QualityIssue(
            "not_measurable",
            "Речь о количественной характеристике, но нет числа с единицей "
            "измерения — подтвердить соответствие нечем", "major"))

    # --- атомарность ---
    # Несколько требований в одном абзаце нельзя раздельно подтвердить:
    # один MoC закроет часть, а вторая останется незамеченной.
    modal_count = sum(len(re.findall(rf"\b{m}\b", low)) for m in MODAL_WORDS)
    atomic = modal_count <= 1
    if modal_count > 1:
        issues.append(QualityIssue(
            "not_atomic",
            f"В одном требовании {modal_count} модальных глаголов — похоже на "
            "несколько требований в одном; их нельзя подтвердить раздельно",
            "minor"))

    if len(original) > 700:
        issues.append(QualityIssue(
            "too_long",
            f"Очень длинная формулировка ({len(original)} символов) — "
            "разбейте на отдельные требования", "minor"))
    if len(original) < 25:
        issues.append(QualityIssue(
            "too_short",
            "Слишком короткая формулировка — вероятно, требование неполное",
            "minor"))

    # --- итоговая оценка ---
    # Штраф пропорционален тяжести: major дороже minor. Формула простая
    # и объяснимая — инженер должен понимать, откуда взялась цифра.
    penalty = sum(0.25 if i.severity == "major" else 0.1 for i in issues)
    score = max(0.0, 1.0 - penalty)

    checks = {
        "unambiguous": not any(i.code in ("vague", "weak_modal") for i in issues),
        "measurable": has_number_unit or not needs_measure,
        "verifiable": has_modal,
        "atomic": atomic,
    }
    return QualityResult(score, issues, checks)


class EditorAgent(Agent):
    """Проверяет формулировки, предлагает исправления (ТЗ п.3.2)."""

    name = "editor"

    def __init__(self, cfg, store, llm: BaseLLM | None = None) -> None:
        super().__init__(cfg, store)
        self.llm = llm

    def run(self, *, requirement_ids: Sequence[int] | None = None,
            owner: str = "", node_code: str = "", limit: int = 200,
            suggest_rewrite: bool = False) -> AgentReport:
        """Проверить требования.

        suggest_rewrite=True требует настроенной LLM: агент попросит её
        предложить переформулировку. Без модели работает только
        детерминированный разбор — и это по-прежнему полезно.
        """
        report = self._report()
        requirements = self._select(requirement_ids, owner, node_code, limit)

        use_llm = suggest_rewrite and self.llm is not None
        if suggest_rewrite and self.llm is None:
            report.errors.append(
                "Переформулировка запрошена, но LLM не настроена "
                "(SAPS_LLM_PROVIDER=none). Выполнен только формальный разбор.")

        for req in requirements:
            report.processed += 1
            result = check_text(req["text"])
            self.store.set_quality(int(req["id"]), result.score,
                                   result.to_dict())
            if result.issues:
                report.findings.append({
                    "requirement_id": int(req["id"]),
                    "external_id": req["external_id"],
                    "score": round(result.score, 3),
                    "issues": [i.to_dict() for i in result.issues],
                })

            if not use_llm or result.score >= self.cfg.quality_min_score:
                continue

            improved = self._rewrite(req["text"], result)
            if not improved or improved.strip() == req["text"].strip():
                continue
            self._suggest(
                report, int(req["id"]), kind="text",
                text_before=req["text"], text_after=improved,
                rationale=("Формальный разбор: " + result.brief()
                           + " Переформулировка предложена моделью "
                           + (self.llm.describe() if self.llm else "")),
                score=result.score)

        self._log(report)
        return report

    def _select(self, ids: Sequence[int] | None, owner: str, node_code: str,
                limit: int) -> list[dict[str, Any]]:
        if ids:
            out = []
            for rid in ids:
                req = self.store.get_requirement(int(rid))
                if req:
                    out.append(req)
            return out
        return self.store.list_requirements(owner=owner, node_code=node_code,
                                            limit=limit)

    def _rewrite(self, text: str, result: QualityResult) -> str:
        """Попросить модель переформулировать. Ошибка — не повод падать."""
        if self.llm is None:
            return ""
        issues = "\n".join(f"- {i.message}" for i in result.issues)
        prompt = EDITOR_TEMPLATE.format(text=text, issues=issues or "—")
        try:
            reply = self.llm.chat([
                {"role": "system", "content": EDITOR_SYSTEM},
                {"role": "user", "content": prompt},
            ])
        except LLMError:
            # Молча пропускаем: формальный разбор уже сохранён, а падение
            # всего прогона из-за недоступной модели хуже, чем отсутствие
            # одной подсказки.
            return ""
        data = extract_json(reply.text)
        if not isinstance(data, dict):
            return ""
        improved = str(data.get("improved") or "").strip()
        # Защита от вырожденного ответа: модель иногда возвращает
        # укороченный пересказ. Требование не должно «худеть» вдвое.
        if improved and len(improved) < len(text) * 0.4:
            return ""
        return improved
