"""Загрузка .env файла из корня проекта."""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Парсит .env файл и устанавливает переменные окружения.

    Переменные, уже заданные в окружении, **не** перезаписываются —
    реальные переменные окружения (docker, CI) имеют приоритет над файлом.
    """
    env_path = path or (_ROOT / ".env")
    if not env_path.is_file():
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
