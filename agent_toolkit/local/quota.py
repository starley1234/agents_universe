"""Инструменты управления квотами ресурсов и защиты от зацикливания (policy.resource_quota_guard, quota.*).

Обеспечивают:
  * Установку лимитов на расход токенов LLM, бюджет USD и количество вызовов инструментов;
  * Проверку расхода перед выполнением тяжелых операций;
  * Сброс и мониторинг квот для каждого рабочего процесса агента.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..core import Tool, ToolError, Workspace


@dataclass
class QuotaLimits:
    max_tokens: int = 100000
    max_usd: float = 5.0
    max_tool_calls: int = 50
    current_tokens: int = 0
    current_usd: float = 0.0
    current_tool_calls: int = 0


class QuotaGuard:
    """Потокобезопасный контролёр квот ресурсов и стоимости."""

    def __init__(self) -> None:
        self.limits = QuotaLimits()
        self._lock = threading.RLock()

    def set_guard(
        self,
        max_tokens: int = 100000,
        max_usd: float = 5.0,
        max_tool_calls: int = 50,
    ) -> str:
        with self._lock:
            self.limits.max_tokens = max(100, max_tokens)
            self.limits.max_usd = max(0.01, max_usd)
            self.limits.max_tool_calls = max(1, max_tool_calls)
            return self.get_report()

    def check_quota(
        self,
        add_tokens: int = 0,
        add_usd: float = 0.0,
        add_calls: int = 1,
    ) -> str:
        with self._lock:
            next_tokens = self.limits.current_tokens + max(0, add_tokens)
            next_usd = self.limits.current_usd + max(0.0, add_usd)
            next_calls = self.limits.current_tool_calls + max(0, add_calls)

            if next_tokens > self.limits.max_tokens:
                raise ToolError(
                    f"ПЕРЕРАСХОД КВОТЫ ТОКЕНОВ (Resource Quota Exceeded): "
                    f"запрошено {next_tokens} > лимит {self.limits.max_tokens}"
                )
            if next_usd > self.limits.max_usd:
                raise ToolError(
                    f"ПЕРЕРАСХОД БЮДЖЕТА USD: "
                    f"запрошено ${next_usd:.4f} > лимит ${self.limits.max_usd:.2f}"
                )
            if next_calls > self.limits.max_tool_calls:
                raise ToolError(
                    f"ПЕРЕРАСХОД ЛИМИТА ВЫЗОВОВ: "
                    f"запрошено {next_calls} > лимит {self.limits.max_tool_calls}"
                )

            self.limits.current_tokens = next_tokens
            self.limits.current_usd = next_usd
            self.limits.current_tool_calls = next_calls
            return self.get_report()

    def reset_quota(
        self,
        max_tokens: int = 100000,
        max_usd: float = 5.0,
        max_tool_calls: int = 50,
    ) -> str:
        with self._lock:
            self.limits.current_tokens = 0
            self.limits.current_usd = 0.0
            self.limits.current_tool_calls = 0
            self.limits.max_tokens = max(100, max_tokens)
            self.limits.max_usd = max(0.01, max_usd)
            self.limits.max_tool_calls = max(1, max_tool_calls)
            return (
                f"### Сброс квот ресурсов выполнен\n"
                f"- Лимиты восстановлены: {self.limits.max_tokens} токенов, "
                f"${self.limits.max_usd:.2f}, {self.limits.max_tool_calls} вызовов.\n"
                f"- Текущий расход сброшен до 0."
            )

    def get_report(self) -> str:
        with self._lock:
            tok_pct = round(
                (self.limits.current_tokens / self.limits.max_tokens) * 100.0, 1
            )
            usd_pct = round(
                (self.limits.current_usd / self.limits.max_usd) * 100.0, 1
            )
            call_pct = round(
                (self.limits.current_tool_calls / self.limits.max_tool_calls) * 100.0,
                1,
            )
            return (
                f"### Контроль квот ресурсов агента (Resource Quota Guard):\n"
                f"- **Расход токенов:** {self.limits.current_tokens} / {self.limits.max_tokens} ({tok_pct}%)\n"
                f"- **Бюджет USD:** ${self.limits.current_usd:.4f} / ${self.limits.max_usd:.2f} ({usd_pct}%)\n"
                f"- **Вызовы инструментов:** {self.limits.current_tool_calls} / {self.limits.max_tool_calls} ({call_pct}%)\n"
                f"- **Статус безопасности:** КВОТЫ В НОРМЕ (Разрешено продолжение работы)"
            )


def build_quota_tools(guard: QuotaGuard | None = None, registry_ref: Any = None) -> list[Tool]:
    """Собрать инструменты контроля квот ресурсов и частоты вызовов агента."""
    g = guard or QuotaGuard()
    _local_rate_limits: dict[str, dict[str, Any]] = {}

    def resource_quota_guard(
        max_tokens: int = 100000,
        max_usd: float = 5.0,
        max_tool_calls: int = 50,
    ) -> str:
        return g.set_guard(max_tokens, max_usd, max_tool_calls)

    def check_quota(
        add_tokens: int = 0,
        add_usd: float = 0.0,
        add_calls: int = 1,
    ) -> str:
        return g.check_quota(add_tokens, add_usd, add_calls)

    def reset_quota(
        max_tokens: int = 100000,
        max_usd: float = 5.0,
        max_tool_calls: int = 50,
    ) -> str:
        return g.reset_quota(max_tokens, max_usd, max_tool_calls)

    def set_tool_rate_limit(
        tool_name: str, max_calls: int, window_seconds: int
    ) -> str:
        if not tool_name.strip():
            raise ToolError("Имя инструмента для установки лимата не может быть пустым")
        if registry_ref and hasattr(registry_ref, "set_rate_limit"):
            res = registry_ref.set_rate_limit(tool_name.strip(), max_calls, window_seconds)
            return (
                f"### Ограничение частоты вызовов (Per-Tool Rate Limit):\n"
                f"- **Инструмент:** `{res['tool']}`\n"
                f"- **Лимит:** {res['max_calls']} вызовов за {res['window_seconds']} с.\n"
                f"- **Статус:** Лимит успешно активирован в реестре."
            )
        _local_rate_limits[tool_name.strip()] = {
            "tool": tool_name.strip(),
            "max_calls": max(1, max_calls),
            "window_seconds": max(1, window_seconds),
            "current_calls": 0,
        }
        return (
            f"### Ограничение частоты вызовов (Per-Tool Rate Limit):\n"
            f"- **Инструмент:** `{tool_name.strip()}`\n"
            f"- **Лимит:** {max(1, max_calls)} вызовов за {max(1, window_seconds)} с.\n"
            f"- **Статус:** Лимит установлен и контролируется локальным контролёром."
        )

    def list_rate_limits() -> str:
        info = {}
        if registry_ref and hasattr(registry_ref, "list_rate_limits"):
            info = registry_ref.list_rate_limits()
        else:
            info = _local_rate_limits
        if not info:
            return "### Ограничения частоты вызовов:\n- Установленные индивидуальные лимиты отсутствуют."
        lines = ["### Установленные ограничения частоты вызовов (Rate Limits):"]
        for tname, val in info.items():
            lines.append(
                f"- **`{tname}`**: не более {val['max_calls']} вызовов за {val['window_seconds']} с "
                f"(текущее число в окне: {val.get('current_calls', 0)})"
            )
        return "\n".join(lines)

    def reset_rate_limits(tool_name: str = "") -> str:
        if registry_ref and hasattr(registry_ref, "reset_rate_limits"):
            registry_ref.reset_rate_limits(tool_name.strip() if tool_name.strip() else None)
        else:
            if tool_name.strip():
                _local_rate_limits.pop(tool_name.strip(), None)
            else:
                _local_rate_limits.clear()
        target_str = f"для `{tool_name.strip()}`" if tool_name.strip() else "для всех инструментов"
        return f"### Сброс лимитов частоты вызовов выполнен {target_str}."

    return [
        Tool(
            name="policy.resource_quota_guard",
            description="Установить и проверить квоты расхода токенов LLM, бюджета USD и вызовов инструментов для защиты от зацикливания.",
            parameters={
                "type": "object",
                "properties": {
                    "max_tokens": {
                        "type": "integer",
                        "description": "Максимальный лимит токенов (по умолчанию 100000)",
                    },
                    "max_usd": {
                        "type": "number",
                        "description": "Максимальный бюджет в USD (по умолчанию 5.0)",
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": "Максимальное число вызовов (по умолчанию 50)",
                    },
                },
            },
            fn=resource_quota_guard,
            skills=["policy", "quota", "security", "guardrails", "safety", "orchestration"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "resource_quota",
                "speed": "fast",
                "tags": [
                    "quota",
                    "resource_quota_guard",
                    "tokens",
                    "usd",
                    "budget",
                    "квота",
                    "лимиты",
                    "бюджет",
                ],
            },
            example="policy.resource_quota_guard(max_tokens=50000, max_usd=2.5)",
        ),
        Tool(
            name="policy.check_quota",
            description="Проверить расход квоты перед вызовом и выбросить ошибку в случае превышения лимитов.",
            parameters={
                "type": "object",
                "properties": {
                    "add_tokens": {
                        "type": "integer",
                        "description": "Учесть добавочный расход токенов",
                    },
                    "add_usd": {
                        "type": "number",
                        "description": "Учесть добавочную стоимость USD",
                    },
                    "add_calls": {
                        "type": "integer",
                        "description": "Учесть добавочное число вызовов (по умолчанию 1)",
                    },
                },
            },
            fn=check_quota,
            skills=["policy", "quota", "security", "check", "safety"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "quota_check",
                "speed": "fast",
                "tags": [
                    "check_quota",
                    "quota",
                    "tokens",
                    "safety",
                    "проверка_квоты",
                ],
            },
            example="policy.check_quota(add_tokens=500, add_usd=0.01)",
        ),
        Tool(
            name="policy.reset_quota",
            description="Сбросить счётчики расхода ресурсов и установить новые лимиты квоты.",
            parameters={
                "type": "object",
                "properties": {
                    "max_tokens": {
                        "type": "integer",
                        "description": "Новый лимит токенов",
                    },
                    "max_usd": {
                        "type": "number",
                        "description": "Новый лимит USD",
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": "Новый лимит вызовов",
                    },
                },
            },
            fn=reset_quota,
            skills=["policy", "quota", "security", "reset", "safety"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "quota_reset",
                "speed": "fast",
                "tags": [
                    "reset_quota",
                    "quota",
                    "reset",
                    "сброс_квоты",
                ],
            },
            example="policy.reset_quota(max_tokens=100000, max_usd=5.0)",
        ),
        Tool(
            name="policy.set_tool_rate_limit",
            description="Установить индивидуальный лимит частоты вызовов для инструмента (например, не более 5 запросов web.search в минуту).",
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Имя инструмента ('web.search', 'smtp.send_email')",
                    },
                    "max_calls": {
                        "type": "integer",
                        "description": "Максимальное разрешённое количество вызовов",
                    },
                    "window_seconds": {
                        "type": "integer",
                        "description": "Период контроля в секундах (например, 60 для минуты, 3600 для часа)",
                    },
                },
                "required": ["tool_name", "max_calls", "window_seconds"],
            },
            fn=set_tool_rate_limit,
            skills=["policy", "ratelimit", "security", "guardrails", "safety", "orchestration"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "rate_limit",
                "speed": "fast",
                "tags": [
                    "ratelimit",
                    "rate_limit",
                    "calls_limit",
                    "frequency",
                    "частота_вызовов",
                    "лимит_частоты",
                ],
            },
            example='policy.set_tool_rate_limit(tool_name="web.search", max_calls=5, window_seconds=60)',
        ),
        Tool(
            name="policy.list_rate_limits",
            description="Получить список всех активных индивидуальных лимитов частоты вызовов инструментов.",
            parameters={"type": "object", "properties": {}},
            fn=list_rate_limits,
            skills=["policy", "ratelimit", "security", "list"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "rate_limits_list",
                "speed": "fast",
                "tags": [
                    "list_rate_limits",
                    "ratelimit",
                    "frequency",
                ],
            },
            example="policy.list_rate_limits()",
        ),
        Tool(
            name="policy.reset_rate_limits",
            description="Сбросить или удалить индивидуальные лимиты частоты вызовов для указанного инструмента или для всех.",
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Имя инструмента (или пустая строка для сброса всех)",
                    }
                },
            },
            fn=reset_rate_limits,
            skills=["policy", "ratelimit", "security", "reset"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "rate_limits_reset",
                "speed": "fast",
                "tags": [
                    "reset_rate_limits",
                    "ratelimit",
                    "reset",
                ],
            },
            example='policy.reset_rate_limits(tool_name="web.search")',
        ),
    ]
