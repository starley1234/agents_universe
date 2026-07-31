"""Инструменты аудита, журналирования и телеметрии вызовов (audit.*, telemetry.*)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


class AuditService:
    """Сервис аудита и телеметрии (потокобезопасный)."""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._lock = threading.RLock()

    def log_event(
        self, event_type: str, action: str, details: dict[str, Any], log_file: str
    ) -> str:
        with self._lock:
            p = self.ws.resolve(log_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            events: list[dict[str, Any]] = []
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        events = raw
                except (ValueError, OSError):
                    events = []
            now = time.time()
            record = {
                "timestamp": now,
                "event_type": event_type,
                "action": action,
                "details": details,
            }
            events.append(record)
            p.write_text(
                json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return f"Событие аудита [{event_type}] '{action}' записано в {self.ws.relative(p)}"

    def record_metrics(
        self,
        tool_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: float = 0.0,
        stats_file: str = "telemetry_metrics.json",
    ) -> str:
        with self._lock:
            p = self.ws.resolve(stats_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            stats: dict[str, Any] = {
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "tools": {},
            }
            if p.exists():
                try:
                    stats = json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    pass

            total_tok = max(0, prompt_tokens) + max(0, completion_tokens)
            cost = round(
                (max(0, prompt_tokens) / 1000.0) * 0.005
                + (max(0, completion_tokens) / 1000.0) * 0.015,
                6,
            )

            stats["total_tokens"] = int(stats.get("total_tokens", 0)) + total_tok
            stats["total_cost_usd"] = round(
                float(stats.get("total_cost_usd", 0.0)) + cost, 6
            )

            t_stats = stats.setdefault("tools", {}).setdefault(
                tool_name,
                {
                    "calls": 0,
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "total_duration_ms": 0.0,
                },
            )
            t_stats["calls"] = int(t_stats.get("calls", 0)) + 1
            t_stats["tokens"] = int(t_stats.get("tokens", 0)) + total_tok
            t_stats["cost_usd"] = round(float(t_stats.get("cost_usd", 0.0)) + cost, 6)
            t_stats["total_duration_ms"] = round(
                float(t_stats.get("total_duration_ms", 0.0)) + max(0.0, duration_ms), 2
            )

            p.write_text(
                json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return (
                f"Телеметрия '{tool_name}' учтена: +{total_tok} токенов "
                f"(~${cost:.6f} USD), всего по проекту: {stats['total_tokens']} токенов"
            )


def build_audit_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты аудита, логирования и телеметрии."""
    srv = AuditService(ws=ws)

    def log_event(
        event_type: str,
        action: str,
        details_json: str = "{}",
        log_file: str = "audit_log.json",
    ) -> str:
        if not event_type.strip() or not action.strip():
            raise ToolError("event_type и action не могут быть пустыми")
        try:
            details = json.loads(details_json) if details_json else {}
            if not isinstance(details, dict):
                raise ValueError("details_json должен быть JSON-объектом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON деталей: {exc}") from exc
        return srv.log_event(
            event_type=event_type.strip(),
            action=action.strip(),
            details=details,
            log_file=log_file or "audit_log.json",
        )

    def record_metrics(
        tool_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: float = 0.0,
    ) -> str:
        if not tool_name.strip():
            raise ToolError("Имя инструмента tool_name не может быть пустым")
        return srv.record_metrics(
            tool_name=tool_name.strip(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
        )

    return [
        Tool(
            name="audit.log_event",
            description="Записать событие в журнал аудита (принятое решение, обоснование, действие).",
            parameters={
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Тип события ('decision', 'security', 'error')",
                    },
                    "action": {
                        "type": "string",
                        "description": "Обозначение действия ('approved_send')",
                    },
                    "details_json": {
                        "type": "string",
                        "description": "JSON объект с подробностями",
                    },
                    "log_file": {
                        "type": "string",
                        "description": "Файл лога (по умолчанию 'audit_log.json')",
                    },
                },
                "required": ["event_type", "action"],
            },
            fn=log_event,
            skills=["audit", "log", "security", "local", "reporting"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "audit_log",
                "speed": "fast",
                "tags": ["audit", "log", "security", "event", "journal"],
            },
            example='audit.log_event(event_type="decision", action="select_vendor", details_json=\'{"vendor": "ACME"}\')',
        ),
        Tool(
            name="telemetry.record_metrics",
            description="Учесть расход токенов (Prompt / Completion) и вычислить стоимость вызова в USD.",
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Имя вызванного инструмента / модели",
                    },
                    "prompt_tokens": {
                        "type": "integer",
                        "description": "Количество токенов промпта",
                    },
                    "completion_tokens": {
                        "type": "integer",
                        "description": "Количество сгенерированных токенов",
                    },
                    "duration_ms": {
                        "type": "number",
                        "description": "Длительность в миллисекундах",
                    },
                },
                "required": ["tool_name"],
            },
            fn=record_metrics,
            skills=["telemetry", "metrics", "cost", "audit", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "telemetry",
                "speed": "fast",
                "tags": ["telemetry", "metrics", "cost", "tokens", "stat"],
            },
            example='telemetry.record_metrics(tool_name="vision.analyze_image", prompt_tokens=500, completion_tokens=50)',
        ),
    ]
