"""Первый прикладной workflow C.O.R.T.E.X.: аудит agent_toolkit.

Он проверяет именно доступность выполнения, а не только наличие схемы:
* локально использует ProductionTester из agent_toolkit, который создаёт
  безопасные fixtures и классифицирует `ok/requires_config/error`;
* удалённый MCP-провайдер проверяет read-only инструменты с аргументами из
  JSON Schema;
* опасные/неопределённые вызовы не маскируются под успех, а получают статус
  `skipped_policy` с конкретной рекомендацией.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ..gateway.toolkit_client import LocalToolkitProvider, ToolkitUnavailable
from ..signals import AuditItem, AuditReport, ToolDescriptor

_CONFIG_WORDS = (
    "token", "api key", "api_key", "credentials", "password", "не настроен", "не установлен",
    "не найден", "connection", "host", "port", "mcp", "remote", "telegram", "smtp", "teamcenter",
    "teamcenter", "s3", "psycopg", "pymysql", "permission",
)


class ToolAuditWorkflow:
    name = "toolkit_audit"

    def __init__(self, provider: Any, *, workspace: str | Path, native_diagnostics: bool = True, allow_network: bool = False, allow_side_effects: bool = True) -> None:
        self.provider = provider
        self.workspace = Path(workspace)
        self.native_diagnostics = native_diagnostics
        self.allow_network = allow_network
        self.allow_side_effects = allow_side_effects
        self.latest: AuditReport | None = None

    async def __call__(self, context: Any) -> dict[str, Any]:
        await context.emit("toolkit.audit.started", {"provider": getattr(self.provider, "name", "unknown")})
        report = await asyncio.to_thread(self.run)
        self.latest = report
        await context.checkpoint("toolkit_audit", report.to_dict())
        for item in report.items:
            await context.emit("toolkit.audit.item", item.to_dict())
        await context.emit("toolkit.audit.completed", report.to_dict())
        return report.to_dict()

    def _native_report(self) -> AuditReport | None:
        if not self.native_diagnostics or not isinstance(self.provider, LocalToolkitProvider):
            return None
        # Native ProductionTester intentionally exercises write fixtures. When
        # an operator disables side effects, use the generic policy-aware path.
        if not self.allow_side_effects:
            return None
        started = time.perf_counter()
        try:
            raw = self.provider.run_native_diagnostics()
        except Exception:
            return None
        items: list[AuditItem] = []
        for result in raw.get("results", []):
            status = result.get("status", "error")
            if status == "ok":
                mapped, tested, recommendation = "passed", True, "Работает в локальном практическом прогоне."
            elif status == "requires_config":
                mapped, tested, recommendation = "requires_configuration", True, "Заполнить реквизиты/установить зависимость и повторить аудит."
            else:
                mapped, tested, recommendation = "failed", True, "Исправить ошибку инструмента и добавить regression test."
            items.append(AuditItem(
                name=result.get("name", ""), status=mapped,
                status_label={"passed": "✅ Работает", "requires_configuration": "⚠️ Требует настройки", "failed": "❌ Ошибка"}[mapped],
                preview=str(result.get("preview", "")), duration_ms=float(result.get("duration_ms", 0)),
                recommendation=recommendation, hint=result.get("requires_config_hint"),
                provider=self.provider.name, tested=tested,
            ))
        report = self._assemble(items, provider=self.provider.name, duration_ms=(time.perf_counter() - started) * 1000)
        report.notes.append("Использован native ProductionTester из agent_toolkit; fixtures изолированы в C.O.R.T.E.X. workspace.")
        return report

    @staticmethod
    def _safe_value(name: str, schema: dict[str, Any], workspace: Path) -> Any:
        definition = schema or {}
        if "default" in definition:
            return definition["default"]
        enum = definition.get("enum")
        if enum:
            return enum[0]
        typ = definition.get("type", "string")
        lower = name.lower()
        if typ == "boolean":
            return False
        if typ in ("integer", "number"):
            return 1
        if typ == "array":
            return []
        if typ == "object":
            return {}
        if any(token in lower for token in ("path", "file", "filename", "directory", "dir")):
            workspace.mkdir(parents=True, exist_ok=True)
            fixture = workspace / "audit_fixture.txt"
            if "write" in lower or "output" in lower:
                return "audit_output.txt"
            if not fixture.exists():
                fixture.write_text("C.O.R.T.E.X. audit fixture\n", encoding="utf-8")
            return str(fixture)
        if "url" in lower:
            return "mock://cortex.local"
        if "json" in lower:
            return "{}"
        if "query" in lower or "text" in lower or "content" in lower:
            return "C.O.R.T.E.X. audit"
        return "test"

    def _arguments_for(self, descriptor: ToolDescriptor) -> dict[str, Any]:
        schema = descriptor.input_schema or {}
        props = schema.get("properties", {})
        required = schema.get("required", list(props))
        return {
            name: self._safe_value(name, props.get(name, {}), self.workspace)
            for name in required
        }

    def _generic_report(self) -> AuditReport:
        started = time.perf_counter()
        try:
            descriptors = self.provider.list_tools()
        except Exception as exc:
            report = AuditReport(provider=getattr(self.provider, "name", "unknown"), success=False)
            report.notes.append(f"Не удалось получить tools/list: {exc}")
            report.recommendations.append({"priority": "high", "code": "provider_unavailable", "text": "Проверить MCP URL, сеть и авторизацию."})
            return report
        items: list[AuditItem] = []
        for descriptor in descriptors:
            if descriptor.dangerous or descriptor.attributes.get("dangerous"):
                items.append(AuditItem(
                    name=descriptor.name, status="skipped_policy", status_label="⏭ Пропущен политикой",
                    recommendation="Запускать только после явного HITL approval и отдельного sandbox профиля.",
                    provider=descriptor.provider, dangerous=True,
                ))
                continue
            if not self.allow_network and self._is_network_tool(descriptor):
                items.append(AuditItem(
                    name=descriptor.name, status="skipped_policy", status_label="⏭ Сеть запрещена",
                    recommendation="Повторить в staging с CORTEX_AUDIT_ALLOW_NETWORK=true.", provider=descriptor.provider,
                ))
                continue
            if not descriptor.attributes.get("read_only", False) and not self.allow_side_effects:
                items.append(AuditItem(
                    name=descriptor.name, status="skipped_policy", status_label="⏭ Побочные эффекты запрещены",
                    recommendation="Включить sandbox side effects или предоставить специализированный fixture.", provider=descriptor.provider,
                ))
                continue
            args = self._arguments_for(descriptor)
            started_tool = time.perf_counter()
            try:
                result = self.provider.call_tool(descriptor.name, args)
                preview = str(result)
                if len(preview) > 240:
                    preview = preview[:237] + "..."
                items.append(AuditItem(
                    name=descriptor.name, status="passed", status_label="✅ Работает", preview=preview,
                    duration_ms=(time.perf_counter() - started_tool) * 1000, recommendation="Работает с базовым smoke-набором.",
                    provider=descriptor.provider, tested=True,
                ))
            except Exception as exc:
                message = str(exc)
                configured = any(word in message.lower() for word in _CONFIG_WORDS)
                status = "requires_configuration" if configured else "failed"
                items.append(AuditItem(
                    name=descriptor.name, status=status,
                    status_label="⚠️ Требует настройки" if configured else "❌ Ошибка",
                    preview=message[:240], duration_ms=(time.perf_counter() - started_tool) * 1000,
                    recommendation="Настроить provider/зависимость и повторить." if configured else "Исправить исключение и добавить regression test.",
                    hint=message if configured else None, provider=descriptor.provider, tested=True,
                ))
        return self._assemble(items, provider=getattr(self.provider, "name", "unknown"), duration_ms=(time.perf_counter() - started) * 1000)

    @staticmethod
    def _is_network_tool(descriptor: ToolDescriptor) -> bool:
        text = " ".join([descriptor.name, descriptor.description, *descriptor.skills]).lower()
        return any(word in text for word in ("http", "web", "browser", "scrap", "search", "telegram", "smtp", "remote", "mcp", "s3"))

    def _assemble(self, items: list[AuditItem], *, provider: str, duration_ms: float) -> AuditReport:
        report = AuditReport(provider=provider, total=len(items), items=items, duration_ms=round(duration_ms, 2))
        report.tested = sum(1 for item in items if item.tested)
        report.passed = sum(1 for item in items if item.status == "passed")
        report.requires_configuration = sum(1 for item in items if item.status == "requires_configuration")
        report.failed = sum(1 for item in items if item.status == "failed")
        report.skipped = sum(1 for item in items if item.status.startswith("skipped"))
        report.success = report.failed == 0
        if report.failed:
            names = [item.name for item in items if item.status == "failed"][:12]
            report.recommendations.append({"priority": "high", "code": "failed_tools", "count": report.failed, "tools": names, "text": "Исправить ошибки выполнения; до этого отключить маршруты через policy profile."})
        if report.requires_configuration:
            names = [item.name for item in items if item.status == "requires_configuration"][:12]
            report.recommendations.append({"priority": "medium", "code": "configuration", "count": report.requires_configuration, "tools": names, "text": "Заполнить только нужные секреты/зависимости и запустить повторный staging-аудит."})
        if report.skipped:
            names = [item.name for item in items if item.status.startswith("skipped")][:12]
            report.recommendations.append({"priority": "low", "code": "policy_skips", "count": report.skipped, "tools": names, "text": "Не считать пропуски успешными: добавить sandbox fixtures или HITL approval."})
        report.recommendations.append({"priority": "info", "code": "coverage", "count": report.tested, "text": f"Практически проверено {report.tested}/{report.total} инструментов ({report.coverage_percent}%)."})
        return report

    def run(self) -> AuditReport:
        native = self._native_report()
        if native is not None:
            self.latest = native
            return native
        report = self._generic_report()
        self.latest = report
        return report


__all__ = ["ToolAuditWorkflow"]
