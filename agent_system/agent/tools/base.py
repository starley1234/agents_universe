"""Каркас инструментов: реестр, схема, безопасное разрешение путей."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class ToolError(Exception):
    """Ожидаемая ошибка инструмента.

    Такие ошибки НЕ роняют агента: текст возвращается модели как результат
    вызова, чтобы она могла исправиться сама.
    """


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]
    # Требует ли подтверждения оператора (для режима без песочницы).
    dangerous: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class Workspace:
    """Рабочая папка агента и защита от выхода за её пределы.

    Главная проверка системы: любой путь, пришедший от модели,
    обязан остаться внутри workspace. Проверяем ПОСЛЕ resolve(),
    поэтому '..', симлинки и абсолютные пути отсекаются одинаково.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    #: markdown-ссылка вида [main.py](http://main.py) — так модели
    #: иногда «украшают» путь, и файл переставал находиться
    _MD_LINK = re.compile(r"^\[([^\]]+)\]\([^)]*\)$")

    @classmethod
    def clean(cls, raw: str) -> str:
        """Убрать оформление, которое модели добавляют к пути.

        Реальный случай из лога LM Studio: Qwen прислал путь
        '[main.py](http://main.py)'. Строгий отказ здесь бесполезен —
        файл существует, модель просто оформила ссылку. Снимаем обёртку
        вместо того, чтобы валить задачу.
        """
        t = str(raw).strip().strip("`").strip()
        m = cls._MD_LINK.match(t)
        if m:
            t = m.group(1).strip()
        if t.startswith("<") and t.endswith(">"):
            t = t[1:-1].strip()
        for pref in ("file://",):
            if t.startswith(pref):
                t = t[len(pref):]
        return t.strip().strip("`").strip()

    def resolve(self, rel: str) -> Path:
        if rel is None:
            raise ToolError("Путь не задан")
        rel = self.clean(rel)
        if not rel:
            raise ToolError("Пустой путь")
        candidate = Path(rel).expanduser()
        full = (self.root / candidate).resolve() if not candidate.is_absolute() \
            else candidate.resolve()
        # strict-проверка вхождения: сравниваем разрешённые пути
        if full != self.root and self.root not in full.parents:
            raise ToolError(
                f"Путь {rel!r} выходит за пределы рабочей папки. "
                "Разрешена только работа внутри workspace."
            )
        return full

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:  # pragma: no cover - защитный случай
            return str(path)


class ToolRegistry:
    """Хранит инструменты и раздаёт их схемы модели."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Инструмент {tool.name!r} уже зарегистрирован")
        self._tools[tool.name] = tool

    def extend(self, tools: list[Tool]) -> None:
        for t in tools:
            self.add(t)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(
                f"Инструмента {name!r} нет. Доступны: {', '.join(self.names()) or '—'}"
            )
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
