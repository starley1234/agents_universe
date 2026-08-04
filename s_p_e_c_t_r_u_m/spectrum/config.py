"""Конфигурация S.P.E.C.T.R.U.M.: все настройки из .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .env import load_env

ROOT = Path(__file__).resolve().parent.parent

# Загружаем .env до объявления Settings
load_env()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, "").lower()
    if val in ("0", "false", "no"):
        return False
    if val in ("1", "true", "yes"):
        return True
    return default


# ---------------------------------------------------------------------------
#  LLM Provider Profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMProfile:
    """Единый профиль LLM: URL, ключ, модель."""
    api_url: str
    api_key: str
    model: str


def _resolve_llm_profile() -> LLMProfile:
    """Читает LLM_ACTIVE_PROVIDER и возвращает соответствующий профиль."""
    provider = os.getenv("LLM_ACTIVE_PROVIDER", "fake").strip().lower()

    if provider == "openrouter":
        return LLMProfile(
            api_url=os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1"),
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            model=os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-lite:preview"),
        )
    if provider == "custom_remote":
        return LLMProfile(
            api_url=os.getenv("CUSTOM_REMOTE_URL", "http://localhost:1234/v1"),
            api_key=os.getenv("CUSTOM_REMOTE_KEY", "sk-local"),
            model=os.getenv("CUSTOM_REMOTE_MODEL", "unsloth/gemma-4-12b-it"),
        )
    if provider == "ollama":
        return LLMProfile(
            api_url=os.getenv("OLLAMA_URL", "http://localhost:11434/v1"),
            api_key=os.getenv("OLLAMA_KEY", "ollama"),
            model=os.getenv("OLLAMA_MODEL", "llama3"),
        )

    # Fake/offline для тестов
    return LLMProfile(api_url="", api_key="", model="fake")


# ---------------------------------------------------------------------------
#  Main Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """Все настройки среды S.P.E.C.T.R.U.M."""

    # --- Application ---
    project_name: str = field(
        default_factory=lambda: os.getenv("PROJECT_NAME", "SPECTRUM"))
    app_port: int = field(
        default_factory=lambda: _env_int("APP_PORT", 8118))
    workspace_dir: str = field(
        default_factory=lambda: os.getenv("WORKSPACE_DIR", "./knowledge_base"))
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    # --- Processing ---
    chunk_size: int = field(
        default_factory=lambda: _env_int("CHUNK_SIZE", 1024))
    chunk_overlap: int = field(
        default_factory=lambda: _env_int("CHUNK_OVERLAP", 200))
    use_vlm: bool = field(
        default_factory=lambda: _env_bool("USE_VLM", True))
    max_file_size_mb: float = field(
        default_factory=lambda: _env_float("MAX_FILE_SIZE_MB", 100.0))

    # --- LLM ---
    llm_profile: LLMProfile = field(
        default_factory=_resolve_llm_profile)

    # --- Vector Store ---
    vector_store: str = field(
        default_factory=lambda: os.getenv("VECTOR_STORE", "chroma").lower())
    qdrant_host: str = field(
        default_factory=lambda: os.getenv("QDRANT_HOST", "localhost"))
    qdrant_port: int = field(
        default_factory=lambda: _env_int("QDRANT_PORT", 6333))
    chroma_persist_dir: str = field(
        default_factory=lambda: os.getenv("CHROMA_PERSIST_DIR", "data/chroma"))
    collection_name: str = field(
        default_factory=lambda: os.getenv("COLLECTION_NAME", "spectrum"))

    # --- Task Queue ---
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379"))

    # --- OCR ---
    ocr_engine: str = field(
        default_factory=lambda: os.getenv("OCR_ENGINE", "tesseract").lower())
    tesseract_lang: str = field(
        default_factory=lambda: os.getenv("TESSERACT_LANG", "rus+eng"))

    # --- Scraping ---
    playwright_timeout: int = field(
        default_factory=lambda: _env_int("PLAYWRIGHT_TIMEOUT", 30000))

    def workspace_path(self) -> Path:
        p = Path(self.workspace_dir)
        if not p.is_absolute():
            p = ROOT / p
        return p.resolve()


def settings() -> Settings:
    """Фабрика: возвращает текущие настройки."""
    return Settings()
