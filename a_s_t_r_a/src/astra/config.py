"""Application settings loaded from environment / .env file."""

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


class Settings(BaseSettings):
    """Root configuration — reads from ``.env`` automatically."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ──────────────────────────────────────────────────
    project_name: str = "A.S.T.R.A."
    environment: Environment = Environment.DEVELOPMENT
    app_host: str = "0.0.0.0"
    app_port: int = 8101
    app_secret_key: str = "change-me"
    log_level: str = "DEBUG"

    # ── LLM ──────────────────────────────────────────────────
    llm_default_provider: LLMProvider = LLMProvider.LOCAL

    openrouter_llm_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "google/gemini-2.0-flash-lite:preview"
    openrouter_api_key: str = ""

    local_llm_url: str = "http://localhost:1234/v1"
    local_llm_model: str = "unsloth/gemma-4-12b-it"
    local_llm_api_key: str = "sk-local"

    # ── Embeddings ───────────────────────────────────────────
    embedding_url: str = "http://localhost:1234/v1"
    embedding_model: str = "text-embedding-qwen3-embedding-0.6b"
    embedding_dimensions: int = 1024
    embedding_key: str = "sk-local"

    # ── Database ─────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://astra:astra@localhost:5432/astra"

    # ── Redis (TaskIQ broker) ────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Email ────────────────────────────────────────────────
    mail_server: str = ""
    mail_port: int = 465
    mail_username: str = ""
    mail_password: str = ""
    mail_from_address: str = ""
    smtp_use_ssl: bool = True

    # ── Telegram ─────────────────────────────────────────────
    telegram_bot_token: Optional[str] = None

    # ── MCP ──────────────────────────────────────────────────
    mcp_search_url: Optional[str] = None
    mcp_image_gen_url: Optional[str] = None
    mcp_tts_url: Optional[str] = None

    # ── Workspace ────────────────────────────────────────────
    workspace_path: Path = Field(default=Path("./workspace"))

    # ── Derived helpers ──────────────────────────────────────

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


settings = Settings()
