"""Конфигурация среды: провайдер LLM, ключи, каталоги."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("ACONSTRUCTOR_DATA", ROOT / "aconstructor" / "data"))
OUT_DIR = Path(os.getenv("ACONSTRUCTOR_OUT", ROOT / "out"))


@dataclass(frozen=True)
class Settings:
    """Настройки среды. Читаются из окружения, но можно передать явно."""

    provider: str = field(default_factory=lambda: os.getenv("ACONSTRUCTOR_PROVIDER", "fake"))
    model: str = field(default_factory=lambda: os.getenv("ACONSTRUCTOR_MODEL", ""))
    temperature: float = field(
        default_factory=lambda: float(os.getenv("ACONSTRUCTOR_TEMPERATURE", "0"))
    )
    base_url: str | None = field(default_factory=lambda: os.getenv("ACONSTRUCTOR_BASE_URL") or None)
    api_key: str | None = field(default_factory=lambda: os.getenv("ACONSTRUCTOR_API_KEY") or None)
    max_tokens: int = field(default_factory=lambda: int(os.getenv("ACONSTRUCTOR_MAX_TOKENS", "2048")))
    data_dir: Path = DATA_DIR
    out_dir: Path = OUT_DIR

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        return {
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-sonnet-latest",
            "ollama": "qwen2.5:14b",
            "fake": "fake-deterministic",
        }.get(self.provider, "gpt-4o-mini")


def settings() -> Settings:
    return Settings()
