"""Агентский слой аналитики (ТЗ п.3.2).

Три встроенных агента, независимых друг от друга и общающихся через БД:
  * EditorAgent    — качество формулировок;
  * ClassifierAgent— привязка к пунктам авиационных правил;
  * GapAgent       — дыры в покрытии + индикатор здоровья сертификации.
"""
from __future__ import annotations

from .base import Agent, AgentReport
from .classifier import ClassifierAgent, index_clauses, index_requirements
from .editor import (EditorAgent, QualityIssue, QualityResult, check_text,
                     MODAL_WORDS, VAGUE_WORDS)
from .gap import Gap, GapAgent, HEALTH_WEIGHTS, MOC_HINTS, suggest_moc

__all__ = ["Agent", "AgentReport", "EditorAgent", "check_text",
           "QualityResult", "QualityIssue", "VAGUE_WORDS", "MODAL_WORDS",
           "ClassifierAgent", "index_clauses", "index_requirements",
           "GapAgent", "Gap", "suggest_moc", "MOC_HINTS", "HEALTH_WEIGHTS"]
