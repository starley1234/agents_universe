"""Тесты инструментов аудита и телеметрии (audit.*, telemetry.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.audit import build_audit_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты аудита и телеметрии (audit.*, telemetry.*)")
        tools = {t.name: t for t in build_audit_tools(ws)}
        check("зарегистрировано 2 инструмента audit/telemetry", len(tools) == 2)

        res_log = tools["audit.log_event"].execute(
            event_type="security", action="allow_user", details_json='{"user": "admin"}'
        )
        check("audit.log_event записывает событие", "audit_log.json" in res_log)
        check("файл аудит-лога создан", ws.exists("audit_log.json"))

        res_met = tools["telemetry.record_metrics"].execute(
            tool_name="vision.analyze", prompt_tokens=1000, completion_tokens=200, duration_ms=150.0
        )
        check("telemetry.record_metrics считает стоимость", "$0.008" in res_met)
        check("файл метрик создан", ws.exists("telemetry_metrics.json"))

    return summary("Тесты аудита и телеметрии")


def test_audit_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
