"""Инструменты единого стандарта конфигурации монорепозитория agents_universe (config.*).

Позволяют управлять .env-файлами и распределением непересекающихся портов APP_PORT
для 10 автономных проектов лаборатории.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace
from ..monorepo_config import (
    MONOREPO_PROJECTS_CONFIG,
    generate_docker_compose_override,
    generate_env_content,
    list_monorepo_ports,
    validate_env_settings,
)


def build_monorepo_config_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты управления единой конфигурацией монорепозитория."""

    def gen_env(project: str, path: str = ".env", overrides_json: str = "{}") -> str:
        key = project.strip().lower()
        if key not in MONOREPO_PROJECTS_CONFIG:
            raise ToolError(
                f"Неизвестный проект {project!r}. Доступные проекты: {', '.join(MONOREPO_PROJECTS_CONFIG.keys())}"
            )
        try:
            overrides = json.loads(overrides_json) if overrides_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в overrides_json: {exc}") from exc

        content = generate_env_content(key, overrides)
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        meta = MONOREPO_PROJECTS_CONFIG[key]
        return (
            f"### Сгенерирован единый файл конфигурации .env для {meta['PROJECT_NAME']} ({ws.relative(p)}):\n"
            f"- **Название проекта (PROJECT_NAME):** `{meta['PROJECT_NAME']}`\n"
            f"- **Выделенный порт (APP_PORT):** **{meta['APP_PORT']}**\n"
            f"- **Описание:** {meta['description']}\n"
            f"- **Размер файла:** {len(content)} символов (строк: {len(content.splitlines())})"
        )

    def list_ports() -> str:
        ports = list_monorepo_ports()
        lines = [
            "### Матрица распределения портов APP_PORT в репозитории agents_universe (10 проектов)",
            "| Проект (`project_key`) | Название (`PROJECT_NAME`) | Порт (`APP_PORT`) | База данных (`DATABASE_URL`) |",
            "| --- | --- | --- | --- |",
        ]
        for item in ports:
            lines.append(
                f"| `{item['project_key']}` | **{item['project_name']}** | **{item['app_port']}** | `{item['db_default']}` |"
            )
        return "\n".join(lines)

    def val_env(path: str = ".env") -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл конфигурации {path!r} не найден в рабочей области")
        content = p.read_text(encoding="utf-8", errors="replace")
        res = validate_env_settings(content)
        if res["valid"]:
            return (
                f"### Валидация файла конфигурации {ws.relative(p)}: **✓ УСПЕШНО**\n"
                f"- **PROJECT_NAME:** `{res['project_name']}`\n"
                f"- **APP_PORT:** `{res['app_port']}` (ENVIRONMENT: `{res['environment']}`)\n"
                f"- **WORKSPACE_PATH:** `{res['workspace_path']}`\n"
                f"- Найдено ключей конфигурации: {res['found_keys_count']} (все обязательные секции присутствуют)"
            )
        return (
            f"### Валидация файла конфигурации {ws.relative(p)}: **✗ ОБНАРУЖЕНЫ ОШИБКИ**\n"
            f"- Отсутствуют обязательные параметры: **{', '.join(res['missing_keys'])}**\n"
            f"- Найдено ключей: {res['found_keys_count']}\n"
            f"Используйте `config.generate_monorepo_env` для создания эталонного файла конфигурации."
        )

    def gen_docker_override(
        project: str,
        path: str = "docker-compose.override.yml",
        expose_public: bool = True,
    ) -> str:
        key = project.strip().lower()
        if key not in MONOREPO_PROJECTS_CONFIG:
            raise ToolError(f"Неизвестный проект {project!r}. Доступные проекты: {', '.join(MONOREPO_PROJECTS_CONFIG.keys())}")
        content = generate_docker_compose_override(key, expose_public=expose_public)
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        meta = MONOREPO_PROJECTS_CONFIG[key]
        return (
            f"### Сгенерирован docker-compose.override.yml для {meta['PROJECT_NAME']} ({ws.relative(p)}):\n"
            f"- **Проброшенный порт (ports):** `0.0.0.0:{meta['APP_PORT']}:{meta['APP_PORT']}`\n"
            f"- **Переменная порта:** `{meta['port_env_var']}={meta['APP_PORT']}`\n"
            f"- **Статус:** Готов к запуску через `docker compose up -d` без изменения исходного `docker-compose.yml`."
        )

    return [
        Tool(
            name="config.generate_monorepo_env",
            description="Сгенерировать стандартный файл конфигурации (.env) по единому шаблону для любого из 10 проектов монорепозитория с выделенным непересекающимся портом APP_PORT.",
            parameters={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Ключ проекта (agent_toolkit, agent_system, agentic_workflow_os, data_forge, erp_ai, maos, saps, vlm_services)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Имя сохраняемого файла (по умолчанию '.env')",
                    },
                    "overrides_json": {
                        "type": "string",
                        "description": "JSON со значениями переопределения (например, '{\"ENVIRONMENT\": \"production\"}')",
                    },
                },
                "required": ["project"],
            },
            fn=gen_env,
            skills=["config", "monorepo", "env", "local", "devops"],
            attributes={
                "category": "devops",
                "read_only": False,
                "dangerous": False,
                "resource_type": "env_config",
                "speed": "fast",
                "tags": ["config", "env", "monorepo", "port", "devops"],
            },
            example='config.generate_monorepo_env(project="agent_system", path=".env")',
        ),
        Tool(
            name="config.list_monorepo_ports",
            description="Вывести матрицу непересекающихся портов APP_PORT (8090, 8101..8109) и названий PROJECT_NAME для всех 10 проектов лаборатории agents_universe.",
            parameters={"type": "object", "properties": {}},
            fn=list_ports,
            skills=["config", "monorepo", "port", "devops", "local"],
            attributes={
                "category": "devops",
                "read_only": True,
                "dangerous": False,
                "resource_type": "port_matrix",
                "speed": "fast",
                "tags": ["config", "port", "matrix", "monorepo", "devops"],
            },
            example="config.list_monorepo_ports()",
        ),
        Tool(
            name="config.validate_env_settings",
            description="Проверить .env файл на соответствие единому стандарту конфигурации монорепозитория (PROJECT_NAME, APP_PORT, LLM, DATABASE, MAIL, TELEGRAM, MCP, WORKSPACE).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к проверяемому файлу .env",
                    }
                },
            },
            fn=val_env,
            skills=["config", "validation", "env", "devops", "local"],
            attributes={
                "category": "devops",
                "read_only": True,
                "dangerous": False,
                "resource_type": "env_config",
                "speed": "fast",
                "tags": ["config", "validation", "env", "check"],
            },
            example='config.validate_env_settings(path=".env")',
        ),
        Tool(
            name="config.generate_docker_override",
            description="Сгенерировать файл docker-compose.override.yml для любого проекта (agent_system, saps и др.) с правильным пробросом порта APP_PORT (8101..8109) и AGENT_HOST=0.0.0.0 для доступа из браузера.",
            parameters={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Ключ проекта (agent_system, agentic_workflow_os, saps, etc.)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Путь сохранения (по умолчанию 'docker-compose.override.yml')",
                    },
                    "expose_public": {
                        "type": "boolean",
                        "description": "Открывать ли порт на всех интерфейсах 0.0.0.0 (True) или только 127.0.0.1 (False)",
                    },
                },
                "required": ["project"],
            },
            fn=gen_docker_override,
            skills=["config", "docker", "compose", "devops", "local"],
            attributes={
                "category": "devops",
                "read_only": False,
                "dangerous": False,
                "resource_type": "docker_compose",
                "speed": "fast",
                "tags": ["config", "docker", "compose", "port", "devops"],
            },
            example='config.generate_docker_override(project="agent_system")',
        ),
    ]
