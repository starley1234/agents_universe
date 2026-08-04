"""Configuration settings for NexusTwin MDM service."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration matching standard Holding MDM template."""

    # === APP SETTINGS ===
    project_name: str = Field(
        default="NexusTwin MDM — AI-driven Holding MDM, Certification & Digital Twin",
        alias="PROJECT_NAME",
    )
    environment: str = Field(default="development", alias="ENVIRONMENT")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8117, alias="APP_PORT")
    app_secret_key: str = Field(
        default="generate-secure-key-here", alias="APP_SECRET_KEY"
    )

    # === LLM CONFIGURATION ===
    llm_active_provider: str = Field(default="custom_remote", alias="LLM_ACTIVE_PROVIDER")

    # Custom Remote Profile
    custom_remote_url: str = Field(
        default="https://my_lm_studio.ai/api/v1", alias="CUSTOM_REMOTE_URL"
    )
    custom_remote_key: str = Field(default="sk-local", alias="CUSTOM_REMOTE_KEY")
    custom_remote_model: str = Field(
        default="unsloth/gemma-4-12b-it", alias="CUSTOM_REMOTE_MODEL"
    )

    # OpenRouter Profile
    openrouter_api_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_API_URL"
    )
    openrouter_api_key: str = Field(default="sk-or-...", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="google/gemini-2.0-flash-lite:preview", alias="OPENROUTER_MODEL"
    )

    # === EMBEDDINGS ===
    embedding_url: str = Field(
        default="https://my_lm_studio.ai/api/v1", alias="EMBEDDING_URL"
    )
    embedding_model: str = Field(
        default="text-embedding-qwen3-embedding-0.6b", alias="EMBEDDING_MODEL"
    )
    embedding_dimensions: int = Field(default=1024, alias="EMBEDDING_DIMENSIONS")
    embedding_key: str = Field(default="sk-or-...", alias="EMBEDDING_KEY")

    # === DATABASE ===
    # Fallback to sqlite+aiosqlite for local development/testing without running PostgreSQL
    database_url: str = Field(
        default="sqlite+aiosqlite:///./workspace/nexus_twin.db", alias="DATABASE_URL"
    )

    # === EMAIL (SMTP) ===
    mail_server: str = Field(default="smtp.test.com", alias="MAIL_SERVER")
    mail_port: int = Field(default=465, alias="MAIL_PORT")
    mail_username: str = Field(default="your-email@test.com", alias="MAIL_USERNAME")
    mail_password: str = Field(default="your-app-password", alias="MAIL_PASSWORD")
    mail_from_address: str = Field(
        default="noreply@example.com", alias="MAIL_FROM_ADDRESS"
    )
    smtp_use_ssl: bool = Field(default=True, alias="SMTP_USE_SSL")

    # === MESSENGERS ===
    telegram_bot_token: str = Field(
        default="123456789:ABCDefGhI...", alias="TELEGRAM_BOT_TOKEN"
    )

    # === MCP SERVERS (Model Context Protocol) ===
    mcp_search_url: str = Field(
        default="http://your-mcp-server:8001/sse", alias="MCP_SEARCH_URL"
    )
    mcp_agent_toolkit: str = Field(
        default="http://localhost:8090/sse", alias="MCP_AGENT_TOOLKIT"
    )

    # === WORKSPACE ===
    workspace_path: str = Field(default="./workspace", alias="WORKSPACE_PATH")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def is_sqlite(self) -> bool:
        """Check if active database engine is SQLite."""
        return "sqlite" in self.database_url.lower()

    @property
    def resolved_workspace_dir(self) -> Path:
        """Get absolute Path to workspace directory."""
        p = Path(self.workspace_path)
        if not p.is_absolute():
            # relative to current working directory or project root
            p = Path.cwd() / p
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
