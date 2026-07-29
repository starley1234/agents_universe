"""Конфигурация: провайдер VLM, лимиты на изображения, каталоги."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


@dataclass(frozen=True)
class Settings:
    """Настройки среды. Читаются из окружения, но можно передать явно."""

    provider: str = field(default_factory=lambda: os.getenv("VLM_PROVIDER", "fake"))
    model: str = field(default_factory=lambda: os.getenv("VLM_MODEL", ""))
    api_key: str | None = field(default_factory=lambda: os.getenv("VLM_API_KEY") or None)
    base_url: str | None = field(default_factory=lambda: os.getenv("VLM_BASE_URL") or None)
    temperature: float = field(default_factory=lambda: _env_float("VLM_TEMPERATURE", 0.0))
    max_tokens: int = field(default_factory=lambda: _env_int("VLM_MAX_TOKENS", 2048))

    # Лимиты на изображения. Телефонное фото на 12 Мп — это лишние деньги за
    # токены при нулевой пользе: детали мельче 1024 px модель всё равно не
    # различает, поэтому уменьшаем до отправки.
    max_side_px: int = field(default_factory=lambda: _env_int("VLM_MAX_SIDE_PX", 1024))
    max_upload_mb: float = field(default_factory=lambda: _env_float("VLM_MAX_UPLOAD_MB", 20.0))
    max_images: int = field(default_factory=lambda: _env_int("VLM_MAX_IMAGES", 8))
    jpeg_quality: int = field(default_factory=lambda: _env_int("VLM_JPEG_QUALITY", 85))

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        return {
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-sonnet-latest",
            "ollama": "qwen2.5vl:7b",
            "fake": "fake-vlm",
        }.get(self.provider, "gpt-4o-mini")


def settings() -> Settings:
    return Settings()
