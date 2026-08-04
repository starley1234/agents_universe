"""Фабрика ингесторов: автоматический выбор подходящего парсера."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .base import Ingestor, SourceType
from .excel import ExcelIngestor
from .image import ImageIngestor
from .pdf import PDFIngestor
from .text import TextIngestor
from .url import URLIngestor

# Глобальный реестр ингесторов
_INGESTORS: list[Ingestor] = [
    PDFIngestor(),
    ExcelIngestor(),
    ImageIngestor(),
    TextIngestor(),
    URLIngestor(),
]

# Маппинг расширений → тип источника
_EXTENSION_MAP: dict[str, SourceType] = {
    ".pdf": SourceType.PDF,
    ".xlsx": SourceType.EXCEL,
    ".xls": SourceType.EXCEL,
    ".csv": SourceType.EXCEL,
    ".png": SourceType.IMAGE,
    ".jpg": SourceType.IMAGE,
    ".jpeg": SourceType.IMAGE,
    ".bmp": SourceType.IMAGE,
    ".tiff": SourceType.IMAGE,
    ".tif": SourceType.IMAGE,
    ".webp": SourceType.IMAGE,
    ".txt": SourceType.TEXT,
    ".md": SourceType.TEXT,
    ".docx": SourceType.WORD,
    ".doc": SourceType.WORD,
}


def get_ingestor(source: str | Path) -> Ingestor | None:
    """Возвращает первый ингестор, который может обработать источник.

    Args:
        source: путь к файлу или URL

    Returns:
        Ingestor или None, если формат не поддерживается.
    """
    for ing in _INGESTORS:
        if ing.can_handle(source):
            return ing
    return None


def supported_extensions() -> set[str]:
    """Множество всех поддерживаемых расширений файлов."""
    return set(_EXTENSION_MAP.keys())


def source_type_for(source: str | Path) -> SourceType:
    """Определяет тип источника по расширению или URL."""
    if isinstance(source, str):
        parsed = urlparse(source)
        if parsed.scheme in ("http", "https"):
            return SourceType.URL

    p = Path(source) if isinstance(source, str) else source
    return _EXTENSION_MAP.get(p.suffix.lower(), SourceType.UNKNOWN)
