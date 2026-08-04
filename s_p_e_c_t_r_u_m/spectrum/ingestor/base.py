"""Базовые типы и абстракция для ингесторов."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SourceType(Enum):
    """Тип источника данных."""
    PDF = "pdf"
    URL = "url"
    EXCEL = "excel"
    IMAGE = "image"
    TEXT = "text"
    WORD = "word"
    UNKNOWN = "unknown"


@dataclass
class PageChunk:
    """Кусок текста с привязкой к месту в документе (traceability)."""
    text: str
    page_number: int | None = None        # Номер страницы (PDF/Word)
    sheet_name: str | None = None          # Имя листа (Excel)
    row_range: tuple[int, int] | None = None  # Диапазон строк (Excel)
    bbox: tuple[float, float, float, float] | None = None  # Координаты (OCR/VLM)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    """Результат ингеста одного документа."""
    source_path: str                        # Путь или URL
    source_type: SourceType
    chunks: list[PageChunk]
    file_hash: str = ""                     # SHA-256 содержимого
    total_pages: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def compute_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @property
    def full_text(self) -> str:
        return "\n\n".join(c.text for c in self.chunks if c.text.strip())

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


class Ingestor(ABC):
    """Абстрактный ингестор: принимает путь/URL, возвращает IngestResult."""

    @abstractmethod
    def ingest(self, source: str | Path, **kwargs) -> IngestResult:
        """Извлечь текст и метаданные из источника."""
        ...

    @abstractmethod
    def can_handle(self, source: str | Path) -> bool:
        """Может ли данный ингестор обработать источник."""
        ...

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        return path.read_bytes()
