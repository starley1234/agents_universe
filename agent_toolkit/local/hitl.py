"""Инструменты «Человек в контуре» (HITL): интерактивные вопросы и запрос подтверждения.

Обеспечивают связь агента с оператором/человеком для получения разрешений
на опасные действия или уточнений по задаче.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from ..core import Tool, ToolError, Workspace


@dataclass
class HitlConfig:
    mock_mode: bool = True
    auto_approve: bool = True  # В автономных тестов одобряем по умолчанию
    mock_human_answer: str = "Одобрено / Вариант 1"


class HitlService:
    """Сервис интерактивных запросов к человеку (HITL, потокобезопасный)."""

    def __init__(self, ws: Workspace | None = None, cfg: HitlConfig | None = None) -> None:
        self.ws = ws
        self.cfg = cfg or HitlConfig()
        self.pending_approvals: list[dict[str, Any]] = []
        self.answered_questions: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def ask_human(
        self, question: str, options: list[str] | None = None, allow_custom: bool = True
    ) -> str:
        with self._lock:
            if not question.strip():
                raise ToolError("Вопрос к человеку не может быть пустым")
            opts_str = f" [Варианты: {', '.join(options)}]" if options else ""
            if self.cfg.mock_mode:
                ans = (
                    options[0]
                    if options
                    else self.cfg.mock_human_answer
                )
                self.answered_questions.append({"question": question, "answer": ans})
                return f"[HITL MOCK] Вопрос: {question}{opts_str} -> Ответ оператора: {ans!r}"
            return f"Вопрос оператору отправлен: {question}"

    def request_approval(
        self, action: str, reason: str, details: dict[str, Any] | None = None
    ) -> str:
        with self._lock:
            if not action.strip() or not reason.strip():
                raise ToolError("Укажите действие (action) и причину (reason)")
            req = {
                "action": action,
                "reason": reason,
                "details": details or {},
                "status": "approved" if self.cfg.auto_approve else "pending",
            }
            self.pending_approvals.append(req)
            if self.cfg.mock_mode and self.cfg.auto_approve:
                return (
                    f"[HITL MOCK] Действие {action!r} ОДОБРЕНО оператором "
                    f"(причина: {reason!r})"
                )
            return f"Запрос на согласование действия {action!r} отправлен оператору"


def build_hitl_tools(
    ws: Workspace | None = None, service: HitlService | None = None
) -> list[Tool]:
    """Собрать инструменты взаимодействия с человеком в контуре (HITL)."""
    srv = service or HitlService(ws=ws)

    def ask_human(
        question: str, options_json: str = "[]", allow_custom: bool = True
    ) -> str:
        try:
            options = json.loads(options_json) if options_json else []
            if not isinstance(options, (list, tuple)):
                raise ValueError("options_json должен быть JSON-массивом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON вариантов options_json: {exc}") from exc
        return srv.ask_human(
            question=question,
            options=[str(o) for o in options],
            allow_custom=allow_custom,
        )

    def request_approval(
        action: str, reason: str, details_json: str = "{}"
    ) -> str:
        try:
            details = json.loads(details_json) if details_json else {}
            if not isinstance(details, dict):
                raise ValueError("details_json должен быть JSON-объектом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON details_json: {exc}") from exc
        return srv.request_approval(action=action, reason=reason, details=details)

    return [
        Tool(
            name="ask.human",
            description="Задать уточняющий вопрос человеку/оператору с опциональным списком вариантов.",
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Текст вопроса к человеку",
                    },
                    "options_json": {
                        "type": "string",
                        "description": 'JSON-массив вариантов (например, \'["Да", "Нет"]\')',
                    },
                    "allow_custom": {
                        "type": "boolean",
                        "description": "Разрешить свободный текст в ответе",
                    },
                },
                "required": ["question"],
            },
            fn=ask_human,
            skills=["hitl", "ask", "human", "interactive", "approval"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "hitl_dialog",
                "speed": "medium",
                "tags": ["hitl", "ask", "human", "question", "operator"],
            },
            example='ask.human(question="Удалить старые отчёты?", options_json=\'["Да", "Нет"]\')',
        ),
        Tool(
            name="hitl.request_approval",
            description="Запросить у человека разрешение на выполнение опасного действия.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Опасное действие (например, 'send_email')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Обоснование, почему действие необходимо",
                    },
                    "details_json": {
                        "type": "string",
                        "description": "JSON объект с деталями действия",
                    },
                },
                "required": ["action", "reason"],
            },
            fn=request_approval,
            skills=["hitl", "approval", "human", "security", "interactive"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "hitl_approval",
                "speed": "medium",
                "tags": ["hitl", "approval", "human", "permission", "security"],
            },
            example='hitl.request_approval(action="delete_bucket", reason="Очистка старых данных")',
        ),
    ]
