"""Тесты интеграции с системой управления требованиями Teamcenter API (tc.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.integrations.teamcenter import build_teamcenter_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Коннектор Teamcenter API (Система управления требованиями PLM)")
    tools = {t.name: t for t in build_teamcenter_tools()}
    check("зарегистрировано 7 инструментов teamcenter", len(tools) == 7)
    check("обновление требования помечено dangerous=True", tools["tc.update_requirement_property"].dangerous is True)

    res_login = tools["tc.login"].execute(endpoint_url="mock://tc.test", username="admin")
    check("tc.login авторизуется и выдаёт куку сессии", "JSESSIONID=MOCK_TC_SESSION_12345" in res_login)

    res_get = tools["tc.get_requirement_item"].execute(item_id="REQ-001")
    check("tc.get_requirement_item читает требование из базы PLM", "вибрацион" in res_get and "REQ-001" in res_get)

    res_search = tools["tc.search_requirements"].execute(query="температур", status_filter="Approved")
    check("tc.search_requirements находит требование по ключевому слову", "REQ-002" in res_search and "-60°C до +85°C" in res_search)

    res_upd = tools["tc.update_requirement_property"].execute(
        item_id="REQ-003", property_name="status", new_value="Approved", reason="Согласовано комиссией"
    )
    check("tc.update_requirement_property обновляет свойство", "Согласовано комиссией" in res_upd and "Approved" in res_upd)

    res_exp = tools["tc.export_requirements_spec"].execute(spec_id="SPEC-TEST", format_type="markdown")
    check("tc.export_requirements_spec выгружает спецификацию в Markdown", "Спецификация требований Teamcenter PLM" in res_exp and "| REQ-001 |" in res_exp)

    res_base = tools["tc.create_requirement_baseline"].execute(
        item_id="REQ-001", new_revision="B", reason="Утверждено главным конструктором"
    )
    check("tc.create_requirement_baseline создаёт базовую линию в mock-режиме", "REQ-001" in res_base and "mock" in res_base.lower())

    res_diff = tools["tc.compare_requirement_revisions"].execute(
        item_id="REQ-001", rev_old="A", rev_new="B"
    )
    check("tc.compare_requirement_revisions выводит разницу между ревизиями", "Сравнение ревизий" in res_diff and "- [A]" in res_diff and "+ [B]" in res_diff)

    return summary("Тесты Teamcenter API")


def test_teamcenter_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
