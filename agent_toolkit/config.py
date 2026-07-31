"""Конфигурация проекта agent_toolkit для продакшна и тестирования.

Поддерживает чтение настроек из переменных окружения (префикс AGENT_TOOLKIT_):
  * AGENT_TOOLKIT_WORKSPACE — корневой путь песочницы;
  * AGENT_TOOLKIT_ENV — "development", "test", "production";
  * AGENT_TOOLKIT_MOCK_MODE — режим заглушек (false для продакшна);
  * AGENT_TOOLKIT_ALLOW_DANGEROUS — разрешение опасных операций (delete/exec/send);
  * AGENT_TOOLKIT_READ_ONLY — режим только для чтения.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Settings:
    """Параметры запуска agent_toolkit (поддержка единого стандарта конфигурации монорепозитория)."""

    project_name: str = "Agent Toolkit"
    workspace_dir: str = "/tmp/agent_toolkit_ws"
    env: str = "development"
    app_host: str = "0.0.0.0"
    api_port: int = 8090
    app_secret_key: str = ""
    mock_mode: bool = True
    allow_dangerous: bool = False
    read_only: bool = False
    http_timeout: int = 10
    log_level: str = "INFO"

    # LLM & Embeddings (Unified standard)
    llm_default_provider: str = "local"
    openrouter_llm_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "google/gemini-2.0-flash-lite:preview"
    openrouter_api_key: str = ""
    local_llm_url: str = "https://my_lm_studio.ai/api/v1"
    local_llm_model: str = "unsloth/gemma-4-12b-it"
    local_llm_api_key: str = ""
    embedding_url: str = "https://my_lm_studio.ai/api/v1"
    embedding_model: str = "text-embedding-qwen3-embedding-0.6b"
    embedding_dimensions: int = 1024
    embedding_key: str = ""

    # База данных
    database_url: str = ""

    # Настройки интеграций (Почта SMTP/IMAP, Telegram, MAX, S3, 1C/ERP, Teamcenter PLM, MCP)
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_ssl: bool = True
    imap_host: str = "imap.example.com"
    imap_port: int = 993
    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""
    max_bot_token: str = ""
    max_api_url: str = "https://platform.max.ru/api"
    mcp_search_url: str = ""
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket_name: str = "agent-bucket"
    erp_odata_url: str = ""
    erp_user: str = ""
    erp_password: str = ""
    tc_endpoint_url: str = ""
    tc_user: str = "infodba"
    tc_password: str = ""

    def get_integrations_dict(self, mask_secrets: bool = True) -> dict[str, Any]:
        """Вернуть текущие настройки интеграций (с опциональным скрытием паролей и токенов)."""
        def mask(val: str) -> str:
            if not mask_secrets or not val:
                return val
            if len(val) <= 6:
                return "***"
            return val[:3] + "***" + val[-2:]

        return {
            "smtp": {
                "smtp_host": self.smtp_host,
                "smtp_port": self.smtp_port,
                "username": self.smtp_user,
                "password": mask(self.smtp_password),
                "from": self.smtp_from or self.smtp_user,
            },
            "telegram": {
                "bot_token": mask(self.telegram_bot_token),
                "default_chat_id": self.telegram_default_chat_id,
            },
            "max": {
                "bot_token": mask(self.max_bot_token),
                "api_url": self.max_api_url,
            },
            "s3": {
                "endpoint_url": self.s3_endpoint_url,
                "access_key": self.s3_access_key,
                "secret_key": mask(self.s3_secret_key),
                "bucket_name": self.s3_bucket_name,
            },
            "erp": {
                "odata_url": self.erp_odata_url,
                "username": self.erp_user,
            },
            "teamcenter": {
                "endpoint_url": self.tc_endpoint_url,
                "username": self.tc_user,
            },
        }

    def update_integrations(self, data: dict[str, Any]) -> dict[str, Any]:
        """Обновить параметры интеграций в памяти (из Web UI или API)."""
        if "smtp" in data and isinstance(data["smtp"], dict):
            sm = data["smtp"]
            if "smtp_host" in sm: self.smtp_host = str(sm["smtp_host"])
            if "smtp_port" in sm:
                try: self.smtp_port = int(sm["smtp_port"])
                except ValueError: pass
            if "username" in sm: self.smtp_user = str(sm["username"])
            if "password" in sm and sm["password"] != "***" and not str(sm["password"]).startswith("***"):
                self.smtp_password = str(sm["password"])
            if "from" in sm: self.smtp_from = str(sm["from"])

        if "telegram" in data and isinstance(data["telegram"], dict):
            tg = data["telegram"]
            if "bot_token" in tg and not str(tg["bot_token"]).startswith("***"):
                self.telegram_bot_token = str(tg["bot_token"])
            if "default_chat_id" in tg:
                self.telegram_default_chat_id = str(tg["default_chat_id"])

        if "s3" in data and isinstance(data["s3"], dict):
            s3d = data["s3"]
            if "endpoint_url" in s3d: self.s3_endpoint_url = str(s3d["endpoint_url"])
            if "bucket_name" in s3d: self.s3_bucket_name = str(s3d["bucket_name"])

        if "erp" in data and isinstance(data["erp"], dict):
            erpd = data["erp"]
            if "odata_url" in erpd: self.erp_odata_url = str(erpd["odata_url"])

        if "teamcenter" in data and isinstance(data["teamcenter"], dict):
            tcd = data["teamcenter"]
            if "endpoint_url" in tcd: self.tc_endpoint_url = str(tcd["endpoint_url"])

        return self.get_integrations_dict(mask_secrets=True)

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        """Загрузить конфигурацию из переменных окружения и аргументов."""
        def get_bool(key: str, default: bool) -> bool:
            val = os.environ.get(key)
            if val is None:
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        def get_int(key: str, default: int) -> int:
            val = os.environ.get(key)
            if val is None:
                return default
            try:
                return int(val.strip())
            except ValueError:
                return default

        env_val = os.environ.get("ENVIRONMENT", os.environ.get("AGENT_TOOLKIT_ENV", "development")).strip().lower()
        # В продакшне mock_mode по умолчанию отключён
        default_mock = False if env_val == "production" else True

        data = {
            "project_name": os.environ.get("PROJECT_NAME", "Agent Toolkit"),
            "workspace_dir": os.environ.get(
                "WORKSPACE_PATH",
                os.environ.get("AGENT_TOOLKIT_WORKSPACE", os.environ.get("WORKSPACE", "/tmp/agent_toolkit_ws")),
            ),
            "env": env_val,
            "app_host": os.environ.get("APP_HOST", "0.0.0.0"),
            "api_port": get_int("APP_PORT", get_int("AGENT_TOOLKIT_API_PORT", 8090)),
            "app_secret_key": os.environ.get("APP_SECRET_KEY", ""),
            "mock_mode": get_bool("AGENT_TOOLKIT_MOCK_MODE", default_mock),
            "allow_dangerous": get_bool("AGENT_TOOLKIT_ALLOW_DANGEROUS", False),
            "read_only": get_bool("AGENT_TOOLKIT_READ_ONLY", False),
            "http_timeout": get_int("AGENT_TOOLKIT_HTTP_TIMEOUT", 10),
            "log_level": os.environ.get("AGENT_TOOLKIT_LOG_LEVEL", "INFO").upper(),
            "llm_default_provider": os.environ.get("LLM_DEFAULT_PROVIDER", "local"),
            "openrouter_llm_url": os.environ.get("OPENROUTER_LLM_URL", "https://openrouter.ai/api/v1"),
            "openrouter_model": os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-lite:preview"),
            "openrouter_api_key": os.environ.get("OPENROUTER_API_KEY", ""),
            "local_llm_url": os.environ.get("LOCAL_LLM_URL", "https://my_lm_studio.ai/api/v1"),
            "local_llm_model": os.environ.get("LOCAL_LLM_MODEL", "unsloth/gemma-4-12b-it"),
            "local_llm_api_key": os.environ.get("LOCAL_LLM_API_KEY", ""),
            "embedding_url": os.environ.get("EMBEDDING_URL", "https://my_lm_studio.ai/api/v1"),
            "embedding_model": os.environ.get("EMBEDDING_MODEL", "text-embedding-qwen3-embedding-0.6b"),
            "embedding_dimensions": get_int("EMBEDDING_DIMENSIONS", 1024),
            "embedding_key": os.environ.get("EMBEDDING_KEY", ""),
            "database_url": os.environ.get("DATABASE_URL", ""),
            "smtp_host": os.environ.get("MAIL_SERVER", os.environ.get("SMTP_HOST", "smtp.example.com")),
            "smtp_port": get_int("MAIL_PORT", get_int("SMTP_PORT", 587)),
            "smtp_user": os.environ.get("MAIL_USERNAME", os.environ.get("SMTP_USER", "")),
            "smtp_password": os.environ.get("MAIL_PASSWORD", os.environ.get("SMTP_PASSWORD", "")),
            "smtp_from": os.environ.get("MAIL_FROM_ADDRESS", os.environ.get("SMTP_FROM", "")),
            "smtp_use_ssl": get_bool("SMTP_USE_SSL", True),
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "telegram_default_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
            "mcp_search_url": os.environ.get("MCP_SEARCH_URL", ""),
            "s3_endpoint_url": os.environ.get("S3_ENDPOINT_URL", ""),
            "s3_bucket_name": os.environ.get("S3_BUCKET_NAME", "agent-bucket"),
            "erp_odata_url": os.environ.get("ERP_ODATA_URL", ""),
            "tc_endpoint_url": os.environ.get("TC_ENDPOINT_URL", ""),
        }
        data.update(overrides)
        return cls(**data)


# Глобальный экземпляр настроек по умолчанию
settings = Settings.from_env()
