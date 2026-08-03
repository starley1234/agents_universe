"""Application settings — Postgres prod, with JWT, FalkorDB, Langfuse."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LLMProvider(str, Enum):
    LOCAL = "local"
    OPENROUTER = "openrouter"
    MOCK = "mock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    project_name: str = "A.S.T.R.A."
    environment: Environment = Environment.DEVELOPMENT
    app_host: str = "0.0.0.0"
    app_port: int = 8101
    app_secret_key: str = "change-me"
    log_level: str = "INFO"

    # LLM
    llm_default_provider: LLMProvider = LLMProvider.MOCK
    openrouter_llm_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "google/gemini-2.0-flash-lite:preview"
    openrouter_api_key: str = ""
    local_llm_url: str = "http://localhost:1234/v1"
    local_llm_model: str = "unsloth/gemma-4-12b-it"
    local_llm_api_key: str = "sk-local"

    # Embeddings
    embedding_url: str = "http://localhost:1234/v1"
    embedding_model: str = "text-embedding-qwen3-embedding-0.6b"
    embedding_dimensions: int = 1024
    embedding_key: str = "sk-local"

    # Database — Postgres
    database_url: str = "postgresql+asyncpg://astra:astra@postgres:5432/astra"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # FalkorDB — graph DB for large ontologies
    falkordb_host: str = "falkordb"
    falkordb_port: int = 6379
    falkordb_url: str = ""  # e.g. falkor://falkordb:6379  — if empty, derived from host/port
    # Enable FalkorDB? If false, use NetworkX in-memory
    use_falkordb: bool = False

    # JWT Auth
    jwt_secret_key: str = "super-secret-jwt-change-me-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24h
    auth_enabled: bool = False  # if false, auth is optional (dev mode)

    # Email
    mail_server: str = ""
    mail_port: int = 465
    mail_username: str = ""
    mail_password: str = ""
    mail_from_address: str = ""
    smtp_use_ssl: bool = True

    # Telegram
    telegram_bot_token: Optional[str] = None

    # MCP
    mcp_search_url: Optional[str] = None
    mcp_image_gen_url: Optional[str] = None
    mcp_tts_url: Optional[str] = None

    # Langfuse tracing
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_enabled: bool = False

    # Workspace
    workspace_path: Path = Field(default=Path("./workspace"))

    @property
    def active_llm_url(self) -> str:
        if self.llm_default_provider == LLMProvider.OPENROUTER:
            return self.openrouter_llm_url
        return self.local_llm_url

    @property
    def active_llm_model(self) -> str:
        if self.llm_default_provider == LLMProvider.OPENROUTER:
            return self.openrouter_model
        return self.local_llm_model

    @property
    def active_llm_api_key(self) -> str:
        if self.llm_default_provider == LLMProvider.OPENROUTER:
            return self.openrouter_api_key
        return self.local_llm_api_key

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    @property
    def resolved_workspace(self) -> Path:
        p = self.workspace_path
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve()

    @property
    def falkordb_full_url(self) -> str:
        if self.falkordb_url:
            return self.falkordb_url
        # FalkorDB uses Redis protocol, host:port
        return f"{self.falkordb_host}:{self.falkordb_port}"


settings = Settings()
