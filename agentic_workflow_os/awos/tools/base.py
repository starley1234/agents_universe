"""Каркас инструментов: описание, гранты, рабочая папка, реестр.

ИНСТРУМЕНТ — ЭТО СИСТЕМНЫЙ ВЫЗОВ СРЕДЫ, А НЕ СПОСОБНОСТЬ АГЕНТА.
Разница принципиальная. Если бы инструменты принадлежали агенту,
профиль, присланный со стороны, мог бы выписать себе доступ к shell и
к внутренней сети. Поэтому здесь: набор инструментов создаёт СРЕДА по
своему конфигу (гранты), а профиль агента и шаг workflow могут только
СУЗИТЬ этот набор. Расширить — нельзя ни при каких значениях полей.

ОШИБКА ИНСТРУМЕНТА НЕ РОНЯЕТ ПРОГОН. ToolError возвращается модели
текстом: «файла нет», «хост запрещён» — модель обычно исправляется
сама. Падать имеет смысл только на сломанной конфигурации среды.

ОПАСНЫЕ ИНСТРУМЕНТЫ ПОМЕЧЕНЫ ЯВНО (`dangerous=True`). При включённом
Human-in-the-Loop среда останавливается ПЕРЕД таким вызовом и спрашивает
человека — это и есть «точка контроля» из описания платформы, только не
на результате шага, а на действии.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class ToolError(Exception):
    """Ожидаемая ошибка инструмента: текст уйдёт модели, прогон продолжится."""


@dataclass
class Tool:
    name: str
    description: str
    #: Схема аргументов: имя -> человекочитаемое описание. Схема попадает
    #: в системный промпт, поэтому она короткая и на языке задачи, а не
    #: JSON Schema на 40 строк, которую слабая модель не осилит.
    args: dict[str, str]
    fn: Callable[..., str]
    dangerous: bool = False
    #: Пример вызова — резко повышает долю корректных вызовов у слабых
    #: моделей. Проверено на 7B-моделях: без примера они выдумывают формат.
    example: str = ""

    def describe(self) -> str:
        args = ", ".join(f"{k} — {v}" for k, v in self.args.items()) or "без аргументов"
        line = f"- {self.name}({args}): {self.description}"
        if self.dangerous:
            line += " [ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ ЧЕЛОВЕКА]"
        if self.example:
            line += f"\n  пример: {self.example}"
        return line

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "args": dict(self.args), "dangerous": self.dangerous}


class Workspace:
    """Рабочая папка и защита от выхода за её пределы.

    Главная проверка: любой путь, пришедший от модели, обязан остаться
    внутри корня. Проверяем ПОСЛЕ resolve() — тогда '..', симлинки и
    абсолютные пути отсекаются одинаково, одной проверкой.
    """

    #: Модели любят «украшать» путь markdown-ссылкой: [main.py](main.py).
    _MD_LINK = re.compile(r"^\[([^\]]+)\]\([^)]*\)$")

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def clean(cls, raw: str) -> str:
        s = str(raw).strip().strip('"').strip("'")
        m = cls._MD_LINK.match(s)
        if m:
            s = m.group(1)
        return s.strip()

    def resolve(self, raw: str) -> Path:
        s = self.clean(raw)
        if not s:
            raise ToolError("Пустой путь")
        p = (self.root / s).expanduser()
        try:
            resolved = p.resolve()
        except OSError as exc:
            raise ToolError(f"Некорректный путь {s!r}: {exc}") from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolError(
                f"Путь {s!r} выходит за пределы рабочей папки {self.root}. "
                "Среда не выпускает файловые операции наружу.")
        return resolved

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


@dataclass
class ToolRegistry:
    """Набор инструментов, доступных конкретному шагу."""
    tools: dict[str, Tool] = field(default_factory=dict)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def subset(self, allowed: list[str]) -> "ToolRegistry":
        """Сузить набор. Только сузить: неизвестное имя молча отбрасывается,
        потому что профиль не должен уметь выдать себе новый инструмент."""
        if not allowed:
            return self
        return ToolRegistry({n: t for n, t in self.tools.items() if n in set(allowed)})

    def prompt(self) -> str:
        if not self.tools:
            return ""
        lines = [self.tools[n].describe() for n in self.names()]
        return "\n".join(lines)

    def to_list(self) -> list[dict[str, Any]]:
        return [self.tools[n].to_dict() for n in self.names()]
