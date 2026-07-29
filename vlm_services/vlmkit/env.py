"""Загрузка `.env` без внешних зависимостей.

Файл `.env.example` есть, в README он упоминается — значит пользователь
вправе ожидать, что `.env` работает. Тянуть ради этого python-dotenv в
обязательные зависимости не хочется: формат простой, а лишняя зависимость
в проде — лишний риск.

Переменные, уже заданные в окружении, приоритетнее файла: так `docker run
-e VLM_HOST=...` и systemd `EnvironmentFile` не перебиваются случайно
забытым локальным `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path

# Ищем вверх от пакета: запуск возможен из любого каталога.
SEARCH_DEPTH = 3


def find_env(start: Path | None = None, filename: str = ".env") -> Path | None:
    here = (start or Path(__file__).resolve().parent).resolve()
    for parent in [here, *here.parents][:SEARCH_DEPTH + 1]:
        candidate = parent / filename
        if candidate.is_file():
            return candidate
    return None


def parse_env(text: str) -> dict[str, str]:
    """Разобрать содержимое .env.

    Поддерживает `KEY=value`, `export KEY=value`, кавычки и комментарии.
    Значение с `#` внутри кавычек не обрезается — иначе токен вида
    `abc#def` молча превратился бы в `abc`.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def load_env(path: str | Path | None = None, override: bool = False) -> dict[str, str]:
    """Прочитать .env в os.environ. Возвращает применённые пары."""
    target = Path(path) if path else find_env()
    if target is None or not Path(target).is_file():
        return {}
    try:
        data = parse_env(Path(target).read_text(encoding="utf-8"))
    except OSError:
        return {}
    applied: dict[str, str] = {}
    for key, value in data.items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
