"""Role-Based Collaboration: Исполнитель, Критик, Контролёр.

Роли — контракт СРЕДЫ, а не свойство агента. Один и тот же профиль
может быть Исполнителем на одном шаге и Критиком на другом: роль
определяет, ЧТО от него хотят и как разбирают ответ, профиль — КТО это
делает и каким промптом.
"""
from __future__ import annotations

from .critic import Verdict, parse_verdict, review
from .profile import (DEFAULT_SYSTEM, Profile, ProfileError, ROLES,
                      default_profile, describe_profiles, list_profiles,
                      load_profile, resolve_profile)
from .supervisor import DECISIONS, Decision, decide, decide_with_llm
from .worker import PauseForHuman, TurnResult, run_turn

__all__ = ["Profile", "ProfileError", "ROLES", "DEFAULT_SYSTEM",
           "load_profile", "list_profiles", "describe_profiles",
           "default_profile", "resolve_profile", "run_turn", "TurnResult",
           "PauseForHuman", "review", "Verdict", "parse_verdict", "decide",
           "decide_with_llm", "Decision", "DECISIONS"]
