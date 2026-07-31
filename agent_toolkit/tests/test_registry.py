"""Тесты умного реестра инструментов, поиска, скилсов, атрибутов, политик, Rate Limit, телеметрии и IaC-конфигурации."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_toolkit
from agent_toolkit.api import ToolkitClient, create_api_app
from agent_toolkit.core import SecurityPolicy, ToolError, ToolPolicyError
from tests.harness import TempWorkspace, check, check_raises, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        reg = agent_toolkit.build_default_registry(tmp.path("ws"))

        section("1. Умный поиск инструментов (ToolRegistry.search)")
        hits_word = reg.search("create word document", limit=1)
        check("поиск 'create word document' находит office.create_docx", hits_word[0][0].name == "office.create_docx")

        hits_mail = reg.search("send an email", limit=1)
        check("поиск 'send an email' находит smtp.send_email", hits_mail[0][0].name == "smtp.send_email")

        hits_shelf = reg.search("audit shelf facings sos", limit=1)
        check("поиск 'audit shelf facings sos' находит inventory.audit_shelf", hits_shelf[0][0].name == "inventory.audit_shelf")

        hits_links = reg.search("check website links", limit=1)
        check("поиск 'check website links' находит site_qa.check_links", hits_links[0][0].name == "site_qa.check_links")

        section("2. Группировка по скилсам (skills) и атрибутам (attributes)")
        skills_group = reg.group_by_skill()
        check("реестр группирует инструменты по скилсам (>10 скилсов)", len(skills_group) >= 10)
        check("в скилсе 'office' есть create_docx и create_xlsx", len(skills_group.get("office", [])) >= 2)

        cats_group = reg.group_by_attribute("category")
        check("группировка по категории содержит local, office, devops, vision и др.", "local" in cats_group and "office" in cats_group)

        ro_tools = reg.filter_by_attributes(read_only=True)
        check("фильтрация по read_only=True находит только читающие инструменты", all(t.attributes.get("read_only") for t in ro_tools))

        section("3. Политика безопасности (SecurityPolicy)")
        pol = SecurityPolicy(allow_dangerous=False, read_only=False)
        check("безопасный инструмент разрешён политикой", pol.is_tool_allowed(reg.get("files.read_file")))
        check("опасный инструмент запрещён (allow_dangerous=False)", not pol.is_tool_allowed(reg.get("files.remove_file")))

        pol_ro = SecurityPolicy(allow_dangerous=True, read_only=True)
        check("в режиме read_only запись запрещена", not pol_ro.is_tool_allowed(reg.get("files.write_file")))

        check_raises("SSRF-защита блокирует localhost", ToolPolicyError, pol.validate_url, "http://localhost:8080/admin")
        check_raises("SSRF-защита блокирует 127.0.0.1", ToolPolicyError, pol.validate_url, "http://127.0.0.1/meta")

        section("4. Python SDK клиент и FastAPI приложение")
        client = ToolkitClient(registry=reg)
        res_search = client.search("email", limit=2)
        check("ToolkitClient.search возвращает список результатов", len(res_search) > 0 and "name" in res_search[0])

        skills_dict = client.list_skills()
        check("ToolkitClient.list_skills возвращает статистику", "files" in skills_dict and skills_dict["files"] > 0)

        try:
            app = create_api_app(reg)
            check("FastAPI приложение создано без ошибок", app.title == "Agent Toolkit API & MCP Server")
        except ImportError as exc:
            check("create_api_app возвращает инструкцию по установке без fastapi", "pip install fastapi" in str(exc))

        section("5. Rate Limiting, Телеметрия и Configuration as Code (IaC)")
        reg.set_rate_limit("crypto.generate_uuid", max_calls=2, window_seconds=60)
        u1 = reg.execute("crypto.generate_uuid")
        u2 = reg.execute("crypto.generate_uuid")
        check("первые 2 вызова инструмента в пределах лимата успешны", bool(u1 and u2))

        rl_error = False
        try:
            reg.execute("crypto.generate_uuid")
        except ToolError as exc:
            rl_error = "ПРЕВЫШЕН ЛИМИТ ЧАСТОТЫ" in str(exc)
        check("3-й вызов блокируется лимитом частоты вызовов (Rate Limit Exceeded)", rl_error is True)

        stats = reg.get_analytics("crypto.generate_uuid")
        check("телеметрия учитывает количество вызовов и время исполнения", stats["calls"] == 2 and "avg_time_ms" in stats)

        json_cfg = reg.export_config("json")
        check("export_config(json) экспортирует настройки и включённые инструменты", "enabled_tools" in json_cfg and "crypto.generate_uuid" in json_cfg)

        yaml_cfg = reg.export_config("yaml")
        check("export_config(yaml) экспортирует настройки в формате YAML", "enabled_tools:" in yaml_cfg)

        rep = reg.import_config(json_cfg)
        check("import_config успешно восстанавливает настройки реестра", rep["success"] is True and rep["enabled_count"] > 10)

    return summary("Тесты реестра, поиска, скилсов и политик")


def test_registry_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
