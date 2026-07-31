"""Тесты единого стандарта конфигурации и портов монорепозитория agents_universe (MONOREPO_PROJECTS_CONFIG, config.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit import build_default_registry
from agent_toolkit.core.workspace import Workspace
from agent_toolkit.monorepo_config import (
    MONOREPO_PROJECTS_CONFIG,
    generate_env_content,
    list_monorepo_ports,
    validate_env_settings,
)
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Единая матрица конфигурации монорепозитория (MONOREPO_PROJECTS_CONFIG)")
    check("Реестр содержит 10 проектов монорепозитория", len(MONOREPO_PROJECTS_CONFIG) == 10)
    check("agent_toolkit присутствует с портом 8090", MONOREPO_PROJECTS_CONFIG["agent_toolkit"]["APP_PORT"] == 8090)
    check("agent_system присутствует с портом 8101", MONOREPO_PROJECTS_CONFIG["agent_system"]["APP_PORT"] == 8101)
    check("vlm_services присутствует с портом 8109", MONOREPO_PROJECTS_CONFIG["vlm_services"]["APP_PORT"] == 8109)

    # Проверка уникальности портов
    ports = [meta["APP_PORT"] for meta in MONOREPO_PROJECTS_CONFIG.values()]
    check("Все 10 портов приложений уникальны и не пересекаются", len(ports) == len(set(ports)))

    section("2. Генерация и валидация конфигурации .env по единому стандарту")
    env_content = generate_env_content("data_forge")
    check("Сгенерированный файл содержит PROJECT_NAME=Data Forge", 'PROJECT_NAME="Data Forge"' in env_content)
    check("Сгенерированный файл содержит APP_PORT=8105", "APP_PORT=8105" in env_content)
    check("Сгенерированный файл содержит секцию LLM_DEFAULT_PROVIDER=local", "LLM_DEFAULT_PROVIDER=local" in env_content)
    check("Сгенерированный файл содержит секцию EMBEDDINGS", "EMBEDDING_MODEL=" in env_content)
    check("Сгенерированный файл содержит секцию EMAIL", "MAIL_SERVER=" in env_content)
    check("Сгенерированный файл содержит секцию MESSENGERS", "TELEGRAM_BOT_TOKEN=" in env_content)
    check("Сгенерированный файл содержит секцию MCP SERVERS", "MCP_SEARCH_URL=" in env_content)

    val_res = validate_env_settings(env_content)
    check("Сгенерированная конфигурация проходит валидацию стандарта", val_res["valid"] is True)
    check("Определён порт 8105", val_res["app_port"] == 8105)

    section("3. Зарегистрированные инструменты config.*")
    ws = Workspace("/tmp/agent_toolkit_test_cfg_ws")
    reg = build_default_registry(ws)
    check("config.generate_monorepo_env зарегистрирован", "config.generate_monorepo_env" in reg._tools)
    check("config.list_monorepo_ports зарегистрирован", "config.list_monorepo_ports" in reg._tools)
    check("config.validate_env_settings зарегистрирован", "config.validate_env_settings" in reg._tools)
    check("config.generate_docker_override зарегистрирован", "config.generate_docker_override" in reg._tools)

    # Выполнение инструмента генерации .env
    res_gen = reg.execute("config.generate_monorepo_env", project="agentic_workflow_os", path="test_awos.env")
    check("config.generate_monorepo_env успешно создаёт файл", "8104" in res_gen)
    p_env = ws.resolve("test_awos.env")
    check("Файл конфигурации реально записан в Workspace", p_env.exists() and "8104" in p_env.read_text())

    # Выполнение инструмента генерации docker-compose.override.yml
    res_dock = reg.execute("config.generate_docker_override", project="agent_system", path="test_override.yml")
    check("config.generate_docker_override успешно создаёт файл override", "8101:8101" in res_dock)
    p_ov = ws.resolve("test_override.yml")
    check("docker-compose.override.yml содержит 0.0.0.0:8101:8101 и AGENT_PORT", p_ov.exists() and "0.0.0.0:8101:8101" in p_ov.read_text())

    # Выполнение инструмента валидации
    res_val = reg.execute("config.validate_env_settings", path="test_awos.env")
    check("config.validate_env_settings возвращает УСПЕШНО", "✓ УСПЕШНО" in res_val)

    return summary("Тесты единой конфигурации монорепозитория (Monorepo Config)")


def test_monorepo_config_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
