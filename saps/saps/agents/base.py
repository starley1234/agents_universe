"""Общий контракт агента САПС (ТЗ п.2.2: Agent-Based Core).

ТРИ ПРАВИЛА, ОДИНАКОВЫЕ ДЛЯ ВСЕХ АГЕНТОВ.

1. АГЕНТ НИЧЕГО НЕ МЕНЯЕТ САМ. Результат работы — предложения
   (suggestion) со статусом pending и/или служебные пометки (оценка
   качества, неподтверждённая связь с пунктом АП). Изменить требование
   может только человек, приняв предложение. Это не перестраховка: в
   сертификации автор формулировки несёт ответственность, и «так решил
   агент» не является объяснением для регулятора.

2. АГЕНТЫ НЕ ВЫЗЫВАЮТ ДРУГ ДРУГА. Общая шина — база: классификатор
   читает требования и пишет связи, gap-аналитик читает связи и пишет
   находки. Нет «агента над агентами», порядок задаёт человек или
   команда CLI. Тот же принцип, что в соседних проектах репозитория.

3. АГЕНТ ОБЯЗАН ОБЪЯСНЯТЬ. У каждого предложения есть rationale —
   почему агент так считает. Инженер должен иметь возможность не
   согласиться осознанно, а не выбирать между «принять» и «отклонить»
   вслепую.

ЧТО ВОЗВРАЩАЕТ АГЕНТ. AgentReport: сколько обработано, что предложено,
что пропущено и почему. Пропуски важны не меньше находок: если агент
молча не обработал половину требований, инженер решит, что там всё
хорошо.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..db.store import Store


@dataclass
class AgentReport:
    """Итог прогона агента по набору требований."""
    agent: str
    processed: int = 0
    suggestions: list[int] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_skip(self, req_id: int, external_id: str, reason: str) -> None:
        self.skipped.append({"requirement_id": req_id,
                             "external_id": external_id, "reason": reason})

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "processed": self.processed,
            "suggestions": self.suggestions,
            "findings": self.findings,
            "skipped": self.skipped,
            "errors": self.errors,
            "counts": {"processed": self.processed,
                       "suggestions": len(self.suggestions),
                       "findings": len(self.findings),
                       "skipped": len(self.skipped),
                       "errors": len(self.errors)},
        }

    def summary(self) -> str:
        parts = [f"обработано {self.processed}"]
        if self.suggestions:
            parts.append(f"предложений {len(self.suggestions)}")
        if self.findings:
            parts.append(f"находок {len(self.findings)}")
        if self.skipped:
            parts.append(f"пропущено {len(self.skipped)}")
        if self.errors:
            parts.append(f"ошибок {len(self.errors)}")
        return f"{self.agent}: " + ", ".join(parts)


class Agent:
    """База для агентов. Наследник реализует run()."""

    name = "agent"
    #: Требуется ли этому агенту языковая модель. Проверяется до запуска,
    #: чтобы инженер получил внятный отказ, а не пустой отчёт.
    needs_llm = False

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    def run(self, **kwargs: Any) -> AgentReport:
        raise NotImplementedError

    # --- вспомогательное ---------------------------------------------
    def _report(self) -> AgentReport:
        return AgentReport(agent=self.name)

    def _suggest(self, report: AgentReport, req_id: int, *, kind: str,
                 text_before: str = "", text_after: str = "",
                 payload: dict[str, Any] | None = None, rationale: str = "",
                 score: float | None = None) -> int:
        sug_id = self.store.add_suggestion(
            req_id, self.name, kind=kind, text_before=text_before,
            text_after=text_after, payload=payload, rationale=rationale,
            score=score)
        report.suggestions.append(sug_id)
        return sug_id

    def _log(self, report: AgentReport, detail: str = "") -> None:
        self.store.log(f"agent:{self.name}", "agent_run",
                       detail=detail or report.summary(),
                       data=report.to_dict().get("counts", {}))
