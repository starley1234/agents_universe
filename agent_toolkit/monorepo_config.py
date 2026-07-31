"""Единый стандарт конфигурации и распределения портов для всех проектов монорепозитория agents_universe.

Обеспечивает унификацию переменных окружения (.env) для 10 автономных проектов лаборатории:
  1. PROJECT_NAME, ENVIRONMENT, APP_HOST, APP_PORT, APP_SECRET_KEY
  2. LLM_DEFAULT_PROVIDER, OPENROUTER_*, LOCAL_LLM_*
  3. EMBEDDING_*
  4. DATABASE_URL
  5. EMAIL (SMTP): MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM_ADDRESS, SMTP_USE_SSL
  6. TELEGRAM_BOT_TOKEN
  7. MCP SERVERS (MCP_SEARCH_URL, MCP_IMAGE_GEN_URL, MCP_TTS_URL)
  8. WORKSPACE_PATH

Каждому проекту выделен собственный уникальный порт APP_PORT, исключающий конфликты при параллельном запуске.
"""
from __future__ import annotations

import re
from typing import Any

# Единая матрица конфигурации 10 независимых проектов лаборатории agents_universe
MONOREPO_PROJECTS_CONFIG: dict[str, dict[str, Any]] = {
    "agent_toolkit": {
        "PROJECT_NAME": "Agent Toolkit",
        "APP_PORT": 8090,
        "description": "Универсальный многопротокольный реестр инструментов (API, MCP, Web UI, Docker)",
        "db_default": "sqlite:///agent_toolkit.db",
        "docker_service": "agent-toolkit",
        "port_env_var": "AGENT_TOOLKIT_API_PORT",
    },
    "agent_system": {
        "PROJECT_NAME": "Agent System",
        "APP_PORT": 8101,
        "description": "Автономная ОС одиночного агента на чистом stdlib (песочница, модульные навыки)",
        "db_default": "sqlite:///agent_system.db",
        "docker_service": "agent",
        "port_env_var": "AGENT_PORT",
    },
    "agent_system_constructor": {
        "PROJECT_NAME": "Agent Constructor",
        "APP_PORT": 8102,
        "description": "Фабрика и конфигурационный конструктор агентов из декларативных JSON-схем",
        "db_default": "sqlite:///aconstructor.db",
        "docker_service": "aconstructor",
        "port_env_var": "ACONSTRUCTOR_PORT",
    },
    "agent_system_simple": {
        "PROJECT_NAME": "Agent System Simple",
        "APP_PORT": 8103,
        "description": "Минималистичное ядро агента и движок оркестрации/дебатов",
        "db_default": "sqlite:///agent_simple.db",
        "docker_service": "agent",
        "port_env_var": "AGENT_PORT",
    },
    "agentic_workflow_os": {
        "PROJECT_NAME": "Agentic Workflow OS",
        "APP_PORT": 8104,
        "description": "Декларативная среда исполнения рабочих процессов (AWOS) с Human-in-the-Loop",
        "db_default": "sqlite:///awos.db",
        "docker_service": "awos",
        "port_env_var": "AWOS_PORT",
    },
    "data_forge": {
        "PROJECT_NAME": "Data Forge",
        "APP_PORT": 8105,
        "description": "Корпоративная платформа интеграции данных и MDM (1С/SQL, семантическая онтология)",
        "db_default": "postgresql+asyncpg://user:pass@localhost:5432/data_forge_db",
        "docker_service": "app",
        "port_env_var": "FORGE_PORT",
    },
    "erp_ai": {
        "PROJECT_NAME": "ERP AI",
        "APP_PORT": 8106,
        "description": "Интеллектуальный агент-снабженец для производства с объяснимостью решений (1С OData)",
        "db_default": "postgresql+asyncpg://user:pass@localhost:5432/erp_ai_db",
        "docker_service": "erp",
        "port_env_var": "ERP_PORT",
    },
    "multi_agent_system_ontology": {
        "PROJECT_NAME": "Multi-Agent System Ontology",
        "APP_PORT": 8107,
        "description": "Многоагентная система (MAOS): семантический роутер, трёхуровневая память и TTS",
        "db_default": "postgresql+asyncpg://user:pass@localhost:5432/maos_db",
        "docker_service": "maos",
        "port_env_var": "MAOS_PORT",
    },
    "saps": {
        "PROJECT_NAME": "SAPS Aviation Certification",
        "APP_PORT": 8108,
        "description": "Прикладная система авиационной сертификации (АП-25 / MoC, анализ дыр в Word/Excel)",
        "db_default": "postgresql+asyncpg://user:pass@localhost:5432/saps_db",
        "docker_service": "saps",
        "port_env_var": "SAPS_PORT",
    },
    "vlm_services": {
        "PROJECT_NAME": "VLM Services",
        "APP_PORT": 8109,
        "description": "Сервис визуальных языковых моделей для распознавания счетов, чертежей и ритейл-аудита",
        "db_default": "postgresql+asyncpg://user:pass@localhost:5432/vlm_services_db",
        "docker_service": "vlm",
        "port_env_var": "VLM_PORT",
    },
}

PROJECT_EXTRA_ENV: dict[str, str] = {
    "agent_toolkit": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (AGENT_TOOLKIT) ===\n"
        "AGENT_TOOLKIT_ENV=production\n"
        "AGENT_TOOLKIT_WORKSPACE=/var/lib/agent_toolkit/workspace\n"
        "AGENT_TOOLKIT_CONFIG_PATH=/var/lib/agent_toolkit/workspace/toolkit_config.json\n"
        "AGENT_TOOLKIT_API_PORT=8090\n"
        "SMTP_HOST=smtp.test.com\n"
        "SMTP_PORT=465\n"
        "SMTP_USER=your-email@test.com\n"
        "SMTP_PASSWORD=your-app-password\n"
        "TELEGRAM_BOT_TOKEN=123456789:ABCDefGhI...\n"
    ),
    "agent_system": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (AGENT_SYSTEM) ===\n"
        "AGENT_PROVIDER=openai\n"
        "AGENT_MODEL=gpt-4o-mini\n"
        "AGENT_SANDBOX=auto\n"
        "AGENT_WORKSPACE=./workspace\n"
        "AGENT_HOST=0.0.0.0\n"
        "AGENT_PORT=8101\n"
        "AGENT_API_TOKEN=generate-secure-key-here\n"
        "AGENT_EMBEDDING_PROVIDER=openrouter\n"
        "AGENT_EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b\n"
        "AGENT_EMBEDDING_DIM=1024\n"
    ),
    "agent_system_constructor": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (AGENT_SYSTEM_CONSTRUCTOR) ===\n"
        "ACONSTRUCTOR_PROVIDER=fake\n"
        "ACONSTRUCTOR_MODEL=gpt-4o-mini\n"
        "ACONSTRUCTOR_HOST=0.0.0.0\n"
        "ACONSTRUCTOR_PORT=8102\n"
        "ACONSTRUCTOR_API_TOKEN=generate-secure-key-here\n"
        "ACONSTRUCTOR_DB=data/aconstructor.db\n"
    ),
    "agent_system_simple": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (AGENT_SYSTEM_SIMPLE) ===\n"
        "AGENT_PROVIDER=llamacpp\n"
        "AGENT_MODEL=qwen3-coder-30b\n"
        "AGENT_SANDBOX=auto\n"
        "AGENT_WORKSPACE=./workspace\n"
        "AGENT_HOST=0.0.0.0\n"
        "AGENT_PORT=8103\n"
        "AGENT_API_TOKEN=generate-secure-key-here\n"
    ),
    "agentic_workflow_os": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (AGENTIC_WORKFLOW_OS) ===\n"
        "AWOS_DB=awos.db\n"
        "AWOS_WORKSPACE=./workspace\n"
        "AWOS_PROVIDER=openai_like\n"
        "AWOS_MODEL=gpt-4o-mini\n"
        "AWOS_HITL=critical\n"
        "AWOS_HOST=0.0.0.0\n"
        "AWOS_PORT=8104\n"
        "AWOS_API_TOKEN=generate-secure-key-here\n"
    ),
    "data_forge": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (DATA_FORGE) ===\n"
        "DB_DSN=postgresql://forge:forge@localhost:5432/dataforge\n"
        "MATCH_AUTO_THRESHOLD=0.92\n"
        "MATCH_REVIEW_THRESHOLD=0.65\n"
        "QUALITY_DEFAULT_SEVERITY=error\n"
        "FORGE_HOST=0.0.0.0\n"
        "FORGE_PORT=8105\n"
        "FORGE_API_TOKEN=generate-secure-key-here\n"
    ),
    "erp_ai": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (ERP_AI) ===\n"
        "DB_DSN=postgresql://erp:erp@localhost:5432/erp_ai\n"
        "PROCUREMENT_MAX_AUTO_AMOUNT=50000\n"
        "PROCUREMENT_DEFAULT_AUTONOMY=draft\n"
        "ERP_HOST=0.0.0.0\n"
        "ERP_PORT=8106\n"
        "ERP_API_TOKEN=generate-secure-key-here\n"
    ),
    "multi_agent_system_ontology": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (MULTI_AGENT_SYSTEM_ONTOLOGY) ===\n"
        "DB_DSN=postgresql://maos:maos@localhost:5432/maos\n"
        "DEFAULT_LOCAL_MODEL=local::llama3\n"
        "DEFAULT_CLOUD_MODEL=openrouter::openai/gpt-4o-mini\n"
        "MAOS_EMBEDDING_PROVIDER=openrouter\n"
        "MAOS_EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b\n"
        "MAOS_EMBEDDING_DIM=1024\n"
        "MAOS_HOST=0.0.0.0\n"
        "MAOS_PORT=8107\n"
        "MAOS_API_TOKEN=generate-secure-key-here\n"
    ),
    "saps": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (SAPS) ===\n"
        "DB_DSN=postgresql://saps:saps@localhost:5432/saps\n"
        "SAPS_LLM_PROVIDER=local\n"
        "SAPS_LLM_MODEL=unsloth/gemma-4-12b-it\n"
        "SAPS_EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b\n"
        "SAPS_EMBEDDING_DIM=1024\n"
        "SAPS_HOST=0.0.0.0\n"
        "SAPS_PORT=8108\n"
        "SAPS_API_TOKEN=generate-secure-key-here\n"
    ),
    "vlm_services": (
        "# === ОСОБЕННОСТИ ПРОЕКТА (VLM_SERVICES) ===\n"
        "DB_DSN=postgresql://vlm:vlm@localhost:5432/vlmservices\n"
        "VLM_PROVIDER=openrouter\n"
        "VLM_MODEL=google/gemini-2.0-flash-lite:preview\n"
        "VLM_HOST=0.0.0.0\n"
        "VLM_PORT=8109\n"
        "VLM_API_TOKEN=generate-secure-key-here\n"
    ),
}


def list_monorepo_ports() -> list[dict[str, Any]]:
    """Вернуть список всех проектов монорепозитория, их имена и выделенные порты."""
    out: list[dict[str, Any]] = []
    for key, meta in MONOREPO_PROJECTS_CONFIG.items():
        out.append(
            {
                "project_key": key,
                "project_name": meta["PROJECT_NAME"],
                "app_port": meta["APP_PORT"],
                "description": meta["description"],
                "db_default": meta["db_default"],
            }
        )
    return sorted(out, key=lambda x: x["app_port"])


def generate_env_content(project_key: str, overrides: dict[str, Any] | None = None) -> str:
    """Сгенерировать стандартный конфигурационный файл .env по единому шаблону для указанного проекта."""
    key = project_key.strip().lower()
    meta = MONOREPO_PROJECTS_CONFIG.get(key)
    if not meta:
        raise KeyError(
            f"Проект {project_key!r} не найден в реестре монорепозитория. "
            f"Доступные проекты: {', '.join(MONOREPO_PROJECTS_CONFIG.keys())}"
        )

    ov = overrides or {}
    project_name = str(ov.get("PROJECT_NAME", meta["PROJECT_NAME"]))
    app_port = int(ov.get("APP_PORT", meta["APP_PORT"]))
    environment = str(ov.get("ENVIRONMENT", "development"))
    app_host = str(ov.get("APP_HOST", "0.0.0.0"))
    app_secret = str(ov.get("APP_SECRET_KEY", "generate-secure-key-here"))
    db_url = str(ov.get("DATABASE_URL", meta["db_default"]))
    workspace = str(ov.get("WORKSPACE_PATH", f"./workspace_{key}"))

    return (
        f"# === APP SETTINGS ===\n"
        f'PROJECT_NAME="{project_name}"\n'
        f"ENVIRONMENT={environment} # development, staging, production\n"
        f"APP_HOST={app_host}\n"
        f"APP_PORT={app_port} # У каждого приложения свой, чтобы не пересекались\n"
        f"APP_SECRET_KEY={app_secret} # Для localhost можно не указывать\n\n"
        f"# === LLM PROVIDERS ===\n"
        f"# по умолчанию: local, openrouter\n"
        f"LLM_DEFAULT_PROVIDER=local\n\n"
        f"# OpenRouter\n"
        f"OPENROUTER_LLM_URL=https://openrouter.ai/api/v1\n"
        f"OPENROUTER_MODEL=google/gemini-2.0-flash-lite:preview\n"
        f"OPENROUTER_API_KEY=sk-or-...\n\n"
        f"# Local LLM (lm_studio, Ollama, vLLM)\n"
        f"LOCAL_LLM_URL=https://my_lm_studio.ai/api/v1\n"
        f"LOCAL_LLM_MODEL=unsloth/gemma-4-12b-it\n"
        f"LOCAL_LLM_API_KEY=sk-local\n\n"
        f"# === EMBEDDINGS ===\n"
        f"EMBEDDING_URL=https://my_lm_studio.ai/api/v1\n"
        f"EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b\n"
        f"EMBEDDING_DIMENSIONS=1024\n"
        f"EMBEDDING_KEY=sk-or-...\n\n"
        f"# === DATABASE ===\n"
        f"DATABASE_URL={db_url}\n\n"
        f"# === EMAIL (SMTP) ===\n"
        f"MAIL_SERVER=smtp.test.com\n"
        f"MAIL_PORT=465\n"
        f"MAIL_USERNAME=your-email@test.com\n"
        f"MAIL_PASSWORD=your-app-password\n"
        f"MAIL_FROM_ADDRESS=noreply@example.com\n"
        f"SMTP_USE_SSL=true\n\n"
        f"# === MESSENGERS ===\n"
        f"TELEGRAM_BOT_TOKEN=123456789:ABCDefGhI...\n"
        f"# Другие настройки по необходимости\n\n"
        f"# === MCP SERVERS (Model Context Protocol) ===\n"
        f"# SSE или Stdio хосты для расширения возможностей LLM\n"
        f"MCP_SEARCH_URL=http://your-mcp-server:8001/sse\n"
        f"#MCP_IMAGE_GEN_URL=http://your-mcp-images:8002/sse\n"
        f"#MCP_TTS_URL=http://your-mcp-audio:8003/sse\n\n"
        f"WORKSPACE_PATH={workspace}\n"
        f"\n"
        f"{PROJECT_EXTRA_ENV.get(key, '')}"
    )


def validate_env_settings(content: str) -> dict[str, Any]:
    """Проверить текст конфигурации .env на соответствие единому стандарту репозитория."""
    required_keys = [
        "PROJECT_NAME",
        "ENVIRONMENT",
        "APP_HOST",
        "APP_PORT",
        "LLM_DEFAULT_PROVIDER",
        "DATABASE_URL",
        "MAIL_SERVER",
        "MAIL_PORT",
        "TELEGRAM_BOT_TOKEN",
        "MCP_SEARCH_URL",
        "WORKSPACE_PATH",
    ]
    lines = [ln.strip() for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    found_keys: dict[str, str] = {}
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            found_keys[k.strip()] = v.split("#")[0].strip().strip('"').strip("'")

    missing = [k for k in required_keys if k not in found_keys]
    is_valid = len(missing) == 0

    port_val = found_keys.get("APP_PORT", "")
    port_int = None
    try:
        port_int = int(port_val)
    except ValueError:
        pass

    return {
        "valid": is_valid,
        "missing_keys": missing,
        "found_keys_count": len(found_keys),
        "project_name": found_keys.get("PROJECT_NAME", "Unknown"),
        "app_port": port_int,
        "environment": found_keys.get("ENVIRONMENT", "development"),
        "workspace_path": found_keys.get("WORKSPACE_PATH", "./workspace"),
    }


def generate_docker_compose_override(project_key: str, expose_public: bool = True) -> str:
    """Сгенерировать docker-compose.override.yml для синхронизации портов с единой матрицей (8090, 8101..8109)."""
    key = project_key.strip().lower()
    meta = MONOREPO_PROJECTS_CONFIG.get(key)
    if not meta:
        raise KeyError(
            f"Проект {project_key!r} не найден в реестре монорепозитория. "
            f"Доступные проекты: {', '.join(MONOREPO_PROJECTS_CONFIG.keys())}"
        )

    srv = meta.get("docker_service", "agent")
    port = meta["APP_PORT"]
    port_var = meta.get("port_env_var", "APP_PORT")
    host_bind = "0.0.0.0" if expose_public else "127.0.0.1"

    return (
        f"# docker-compose.override.yml для {meta['PROJECT_NAME']}\n"
        f"# Автоматически синхронизирует порты Docker с единой матрицей монорепозитория ({port})\n"
        f"services:\n"
        f"  {srv}:\n"
        f"    ports:\n"
        f"      - \"{host_bind}:{port}:{port}\"\n"
        f"    environment:\n"
        f"      {port_var}: \"{port}\"\n"
        f"      APP_PORT: \"{port}\"\n"
        f"      AGENT_HOST: \"0.0.0.0\"\n"
    )
