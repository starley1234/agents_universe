"""Конфигурация C.O.R.T.E.X.

Все секреты читаются только из окружения. Этот модуль не логирует значения
ключей и умеет работать с минимальным набором переменных в development.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


_TRUE = {"1", "true", "yes", "y", "on", "да"}
_FALSE = {"0", "false", "no", "n", "off", "нет"}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _env(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = env.get(name)
        if value is not None and value != "":
            return value
    return default


@dataclass(frozen=True)
class Settings:
    """Runtime settings with safe local defaults.

    Названия переменных совместимы с предложенным шаблоном настроек и
    дополнены параметрами транспорта/оркестратора C.O.R.T.E.X.
    """

    project_name: str = "C.O.R.T.E.X."
    environment: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8117
    app_secret_key: str = ""

    llm_active_provider: str = "custom_remote"
    custom_remote_url: str = ""
    custom_remote_key: str = ""
    custom_remote_model: str = ""
    openrouter_api_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    embedding_url: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 1024
    embedding_key: str = ""

    database_url: str = ""
    event_bus_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"
    nats_url: str = "nats://localhost:4222"
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    orchestration_backend: str = "in_memory"
    litellm_api_url: str = ""
    langsmith_endpoint: str = ""
    langsmith_api_key: str = ""
    phoenix_endpoint: str = ""

    mail_server: str = ""
    mail_port: int = 465
    mail_username: str = ""
    mail_password: str = ""
    mail_from_address: str = ""
    smtp_use_ssl: bool = True
    telegram_bot_token: str = ""

    mcp_search_url: str = ""
    mcp_agent_toolkit: str = "http://localhost:8090/sse"
    mcp_agent_toolkit_key: str = ""
    workspace_path: str = "./workspace"
    toolkit_mode: str = "auto"  # auto, local, remote, disabled
    audit_native_diagnostics: bool = True
    audit_allow_network: bool = False
    audit_allow_side_effects: bool = True
    fetch_allow_hosts: tuple[str, ...] = ()
    max_event_history: int = 2000
    cors_origins: tuple[str, ...] = ("*",)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = dict(os.environ if env is None else env)
        origins_raw = _env(values, "CORTEX_CORS_ORIGINS", default="*")
        origins = tuple(item.strip() for item in origins_raw.split(",") if item.strip()) or ("*",)
        fetch_hosts_raw = _env(values, "CORTEX_FETCH_ALLOW_HOSTS", default="")
        fetch_hosts = tuple(item.strip() for item in fetch_hosts_raw.split(",") if item.strip())
        return cls(
            project_name=_env(values, "PROJECT_NAME", default="C.O.R.T.E.X."),
            environment=_env(values, "ENVIRONMENT", "CORTEX_ENVIRONMENT", default="development"),
            app_host=_env(values, "APP_HOST", default="0.0.0.0"),
            app_port=_int(_env(values, "APP_PORT", default="8117"), 8117),
            app_secret_key=_env(values, "APP_SECRET_KEY"),
            llm_active_provider=_env(values, "LLM_ACTIVE_PROVIDER", default="custom_remote"),
            custom_remote_url=_env(values, "CUSTOM_REMOTE_URL"),
            custom_remote_key=_env(values, "CUSTOM_REMOTE_KEY"),
            custom_remote_model=_env(values, "CUSTOM_REMOTE_MODEL"),
            openrouter_api_url=_env(values, "OPENROUTER_API_URL", default="https://openrouter.ai/api/v1"),
            openrouter_api_key=_env(values, "OPENROUTER_API_KEY"),
            openrouter_model=_env(values, "OPENROUTER_MODEL"),
            embedding_url=_env(values, "EMBEDDING_URL"),
            embedding_model=_env(values, "EMBEDDING_MODEL"),
            embedding_dimensions=_int(_env(values, "EMBEDDING_DIMENSIONS", default="1024"), 1024),
            embedding_key=_env(values, "EMBEDDING_KEY"),
            database_url=_env(values, "DATABASE_URL"),
            event_bus_backend=_env(values, "CORTEX_EVENT_BUS", "EVENT_BUS_BACKEND", default="memory"),
            redis_url=_env(values, "REDIS_URL", default="redis://localhost:6379/0"),
            nats_url=_env(values, "NATS_URL", default="nats://localhost:4222"),
            temporal_target=_env(values, "TEMPORAL_TARGET", default="localhost:7233"),
            temporal_namespace=_env(values, "TEMPORAL_NAMESPACE", default="default"),
            orchestration_backend=_env(values, "ORCHESTRATION_BACKEND", default="in_memory"),
            litellm_api_url=_env(values, "LITELLM_API_URL"),
            langsmith_endpoint=_env(values, "LANGSMITH_ENDPOINT"),
            langsmith_api_key=_env(values, "LANGSMITH_API_KEY"),
            phoenix_endpoint=_env(values, "PHOENIX_ENDPOINT"),
            mail_server=_env(values, "MAIL_SERVER"),
            mail_port=_int(_env(values, "MAIL_PORT", default="465"), 465),
            mail_username=_env(values, "MAIL_USERNAME"),
            mail_password=_env(values, "MAIL_PASSWORD"),
            mail_from_address=_env(values, "MAIL_FROM_ADDRESS"),
            smtp_use_ssl=_bool(_env(values, "SMTP_USE_SSL"), True),
            telegram_bot_token=_env(values, "TELEGRAM_BOT_TOKEN"),
            mcp_search_url=_env(values, "MCP_SEARCH_URL"),
            mcp_agent_toolkit=_env(values, "MCP_AGENT_TOOLKIT", "AGENT_TOOLKIT_MCP_URL", default="http://localhost:8090/sse"),
            mcp_agent_toolkit_key=_env(values, "MCP_AGENT_TOOLKIT_KEY"),
            workspace_path=_env(values, "WORKSPACE_PATH", "CORTEX_WORKSPACE_PATH", default="./workspace"),
            toolkit_mode=_env(values, "CORTEX_TOOLKIT_MODE", default="auto").lower(),
            audit_native_diagnostics=_bool(_env(values, "CORTEX_AUDIT_NATIVE"), True),
            audit_allow_network=_bool(_env(values, "CORTEX_AUDIT_ALLOW_NETWORK"), False),
            audit_allow_side_effects=_bool(_env(values, "CORTEX_AUDIT_ALLOW_SIDE_EFFECTS"), True),
            fetch_allow_hosts=fetch_hosts,
            max_event_history=_int(_env(values, "CORTEX_MAX_EVENT_HISTORY", default="2000"), 2000),
            cors_origins=origins,
            log_level=_env(values, "LOG_LEVEL", default="INFO"),
        )

    @property
    def workspace(self) -> Path:
        path = Path(self.workspace_path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def public_dict(self) -> dict[str, object]:
        """Безопасный снимок конфигурации для UI/API без секретов."""
        def safe_url(value: str) -> str:
            if not value:
                return ""
            try:
                parsed = urlsplit(value)
                if parsed.scheme and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            except ValueError:
                pass
            return value

        return {
            "project_name": self.project_name,
            "environment": self.environment,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "event_bus_backend": self.event_bus_backend,
            "orchestration_backend": self.orchestration_backend,
            "llm_active_provider": self.llm_active_provider,
            "custom_remote_url": safe_url(self.custom_remote_url),
            "custom_remote_model": self.custom_remote_model,
            "openrouter_api_url": safe_url(self.openrouter_api_url),
            "openrouter_model": self.openrouter_model,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "redis_url": safe_url(self.redis_url),
            "nats_url": safe_url(self.nats_url),
            "temporal_target": self.temporal_target,
            "mcp_agent_toolkit": safe_url(self.mcp_agent_toolkit),
            "toolkit_mode": self.toolkit_mode,
            "audit_native_diagnostics": self.audit_native_diagnostics,
            "audit_allow_network": self.audit_allow_network,
            "fetch_allow_hosts": list(self.fetch_allow_hosts),
            "workspace_path": str(self.workspace),
            "cors_origins": list(self.cors_origins),
        }


_SETTINGS: Settings | None = None


def get_settings(*, reload: bool = False) -> Settings:
    global _SETTINGS
    if _SETTINGS is None or reload:
        _SETTINGS = Settings.from_env()
    return _SETTINGS
