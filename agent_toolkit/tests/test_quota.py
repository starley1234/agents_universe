"""Тесты инструментов контроля квот ресурсов агента и лимитов частоты (policy.resource_quota_guard, quota.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import ToolError
from agent_toolkit.local.quota import build_quota_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Контроль квот ресурсов и защита от зацикливания (policy.* / quota.*)")
    tools = {t.name: t for t in build_quota_tools()}
    check("зарегистрировано 6 инструментов квот и лимитов частоты", len(tools) == 6)

    res_guard = tools["policy.resource_quota_guard"].execute(
        max_tokens=1000, max_usd=0.5, max_tool_calls=10
    )
    check("policy.resource_quota_guard устанавливает лимиты и выдаёт отчёт", "0 / 1000" in res_guard and "$0.0000 / $0.50" in res_guard)

    res_check_ok = tools["policy.check_quota"].execute(
        add_tokens=400, add_usd=0.1, add_calls=2
    )
    check("policy.check_quota успешно фиксирует расход в пределах лимита", "400 / 1000" in res_check_ok and "КВОТЫ В НОРМЕ" in res_check_ok)

    # Проверка перерасхода
    quota_error = False
    try:
        tools["policy.check_quota"].execute(add_tokens=2000, add_usd=1.0)
    except ToolError as exc:
        quota_error = "ПЕРЕРАСХОД" in str(exc)
    check("policy.check_quota блокирует превышение лимитов с ошибкой", quota_error is True)

    res_reset = tools["policy.reset_quota"].execute(
        max_tokens=100000, max_usd=5.0, max_tool_calls=50
    )
    check("policy.reset_quota сбрасывает счётчики и восстанавливает лимиты", "Сброс квот ресурсов выполнен" in res_reset and "100000 токенов" in res_reset)

    section("2. Ограничение частоты вызовов (Per-Tool Rate Limiting)")
    res_rl = tools["policy.set_tool_rate_limit"].execute(
        tool_name="web.search", max_calls=5, window_seconds=60
    )
    check("policy.set_tool_rate_limit устанавливает лимит частоты", "5 вызовов за 60 с." in res_rl and "web.search" in res_rl)

    res_rl_list = tools["policy.list_rate_limits"].execute()
    check("policy.list_rate_limits выводит список активных ограничений", "web.search" in res_rl_list)

    res_rl_reset = tools["policy.reset_rate_limits"].execute(tool_name="web.search")
    check("policy.reset_rate_limits сбрасывает индивидуальное ограничение", "Сброс лимитов частоты" in res_rl_reset)

    return summary("Тесты контроля квот и лимитов частоты")


def test_quota_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
