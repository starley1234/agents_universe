"""Рабочая папка агента: изоляция песочницы и безопасное разрешение путей."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Union


class WorkspaceError(Exception):
    """Ошибка рабочей области: попытка выхода за пределы или некорректный путь."""


class Workspace:
    """Изолированная рабочая область агента.

    Главная проверка безопасности: любой путь, полученный от модели,
    обязан остаться внутри корневой директории `root`.
    """

    #: Markdown-ссылки вида [file.txt](http://...) — модели часто оборачивают так имена файлов
    _MD_LINK = re.compile(r"^\[([^\]]+)\]\([^)]*\)$")

    def __init__(self, root: Union[str, os.PathLike[str]]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def clean(self, path: str) -> str:
        """Очистить путь от разметки Markdown, кавычек и лишних пробелов."""
        path = str(path).strip()
        m = self._MD_LINK.match(path)
        if m:
            path = m.group(1).strip()
        if len(path) >= 2 and (
            (path[0] == '"' and path[-1] == '"') or (path[0] == "'" and path[-1] == "'")
        ):
            path = path[1:-1].strip()
        return path

    def resolve(self, path: Union[str, os.PathLike[str]]) -> Path:
        """Разрешить путь и проверить, что он находится внутри рабочей области.

        Вызывает `WorkspaceError`, если путь ведёт за пределы root
        ('..', абсолютный путь за пределы root, симлинки).
        """
        cleaned = self.clean(str(path))
        if not cleaned:
            raise WorkspaceError("Передан пустой путь")

        p = Path(cleaned)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (self.root / p).resolve()

        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(
                f"Путь {path!r} выходит за пределы рабочей области {self.root}"
            ) from exc

        return resolved

    def relative(self, path: Union[str, os.PathLike[str]]) -> str:
        """Вернуть путь относительно корня рабочей области."""
        p = Path(path).resolve()
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    def exists(self, path: Union[str, os.PathLike[str]]) -> bool:
        """Проверить существование файла или директории внутри рабочей области."""
        try:
            return self.resolve(path).exists()
        except WorkspaceError:
            return False
