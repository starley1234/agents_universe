"""Application configuration: .env → Settings singleton.

Приоритет: переменные окружения > .env файл > дефолты.
Секреты ТОЛЬКО из окружения (не из JSON/файла конфига).
"""
from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Environment(str, Enum):
    DEV = "development"
    STAGING = "staging"
    PROD = "production"


class Provider(str, Enum):
    LOCAL = "local"
    OPENROUTER = "openrouter"


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # ── App ──────────────────────────────────────────────
    APP_ENV: Environment = Environment.DEV
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8112
    APP_SECRET_KEY: str = ""
    APP_API_TOKEN: str = ""

    # ── LLM ──────────────────────────────────────────────
    LLM_PROVIDER: Provider = Provider.LOCAL
    LLM_TEMPERATURE: float = 0.3
    OPENROUTER_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "google/gemini-2.0-flash-lite:preview"
    OPENROUTER_API_KEY: str = ""
    LOCAL_LLM_URL: str = "http://localhost:11434/v1"
    LOCAL_LLM_MODEL: str = "unsloth/gemma-4-12b-it"
    LOCAL_LLM_API_KEY: str = "sk-local"

    # ── Embeddings ───────────────────────────────────────
    EMBEDDING_PROVIDER: Provider = Provider.LOCAL
    EMBEDDING_MODEL: str = "text-embedding-qwen3-embedding-0.6b"
    EMBEDDING_DIMENSIONS: int = 1024

    # ── Database ─────────────────────────────────────────
    POSTGRES_DB: str = "agent_universe"
    POSTGRES_USER: str = "agent"
    POSTGRES_PASSWORD: str = ""
    DATABASE_URL: str = ""

    # ── Email ────────────────────────────────────────────
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@example.com"
    SMTP_TO: str = ""
    SMTP_USE_SSL: bool = True

    # ── Telegram ─────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""

    # ── MCP ──────────────────────────────────────────────
    MCP_SEARCH_URL: str = ""

    # ── Agent ────────────────────────────────────────────
    AGENT_MAX_ITERATIONS: int = 20
    AGENT_QUALITY_THRESHOLD: float = 0.8
    AGENT_MAX_HOURS: float = 2.0
    AGENT_REPORT_INTERVAL: int = 300

    # ── Workspace ────────────────────────────────────────
    WORKSPACE_PATH: str = "/data/workspace"

    # ── Derived ──────────────────────────────────────────
    @model_validator(mode="after")
    def _build_database_url(self) -> "Settings":
        if not self.DATABASE_URL and self.POSTGRES_PASSWORD:
            self.DATABASE_URL = (
                f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                f"@db:5432/{self.POSTGRES_DB}"
            )
        return self

    @property
    def llm_base_url(self) -> str:
        if self.LLM_PROVIDER == Provider.OPENROUTER:
            return self.OPENROUTER_URL
        return self.LOCAL_LLM_URL

    @property
    def llm_api_key(self) -> str:
        if self.LLM_PROVIDER == Provider.OPENROUTER:
            return self.OPENROUTER_API_KEY
        return self.LOCAL_LLM_API_KEY

    @property
    def llm_model(self) -> str:
        if self.LLM_PROVIDER == Provider.OPENROUTER:
            return self.OPENROUTER_MODEL
        return self.LOCAL_LLM_MODEL

    @property
    def emb_base_url(self) -> str:
        if self.EMBEDDING_PROVIDER == Provider.OPENROUTER:
            return self.OPENROUTER_URL
        return self.LOCAL_LLM_URL

    @property
    def emb_api_key(self) -> str:
        if self.EMBEDDING_PROVIDER == Provider.OPENROUTER:
            return self.OPENROUTER_API_KEY
        return self.LOCAL_LLM_API_KEY

    @property
    def is_prod(self) -> bool:
        return self.APP_ENV == Environment.PROD

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV == Environment.DEV

    def require_token(self) -> None:
        """Enforce: non-localhost host requires API token."""
        if self.APP_HOST not in ("127.0.0.1", "localhost") and not self.APP_API_TOKEN:
            raise SystemExit(
                "Отказ: сервер открыт наружу без токена. "
                "Задайте APP_API_TOKEN или слушайте 127.0.0.1."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
