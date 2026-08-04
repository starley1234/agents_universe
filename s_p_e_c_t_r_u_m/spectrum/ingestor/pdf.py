"""Ингестор PDF: извлечение текста со страниц и метаданных."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import IngestResult, Ingestor, PageChunk, SourceType


class PDFIngestor(Ingestor):
    """Извлекает текст из PDF-файлов.

    Поддерживает:
    - Текстовые PDF (прямое извлечение)
    - Заголовок страницы и номер
    - Метаданные (автор, дата, количество страниц)
    """

    def can_handle(self, source: str | Path) -> bool:
        p = Path(source) if isinstance(source, str) else source
        return p.suffix.lower() == ".pdf"

    def ingest(self, source: str | Path, **kwargs) -> IngestResult:
        path = Path(source)
        data = self._read_bytes(path)
        file_hash = IngestResult.compute_hash(data)

        try:
            import fitz  # pymupdf
        except ImportError:
            return self._fallback_ingest(path, data, file_hash)

        doc = fitz.open(stream=data, filetype="pdf")
        chunks: list[PageChunk] = []
        total_pages = len(doc)
        metadata: dict[str, Any] = {}

        # Метаданные документа
        meta = doc.metadata or {}
        if meta:
            metadata["title"] = meta.get("title", "")
            metadata["author"] = meta.get("author", "")
            metadata["subject"] = meta.get("subject", "")
            metadata["creator"] = meta.get("creator", "")
            metadata["creation_date"] = meta.get("creationDate", "")

        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text("text").strip()
            if not text:
                continue

            # Координаты текстовых блоков для traceability
            blocks = page.get_text("blocks")
            first_block = blocks[0] if blocks else None
            bbox = None
            if first_block and len(first_block) >= 5:
                bbox = tuple(float(x) for x in first_block[:4])

            # Очистка артефактов
            text = _clean_pdf_text(text)

            chunks.append(PageChunk(
                text=text,
                page_number=page_num + 1,
                bbox=bbox,
                metadata={"page_width": float(page.rect.width),
                          "page_height": float(page.rect.height)},
            ))

        doc.close()

        return IngestResult(
            source_path=str(path),
            source_type=SourceType.PDF,
            chunks=chunks,
            file_hash=file_hash,
            total_pages=total_pages,
            metadata=metadata,
        )

    def _fallback_ingest(self, path: Path, data: bytes, file_hash: str) -> IngestResult:
        """Упрощённый парсинг без pymupdf (только если библиотека не установлена)."""
        text = data.decode("utf-8", errors="replace")
        # Извлекаем видимый текст из raw PDF (очень приблизительно)
        visible = re.sub(rb"[\x00-\x08\x0e-\x1f]", b"", data)
        try:
            text = visible.decode("latin-1", errors="replace")
        except Exception:
            text = ""

        chunks = [PageChunk(text=text[:5000], page_number=1)] if text.strip() else []

        return IngestResult(
            source_path=str(path),
            source_type=SourceType.PDF,
            chunks=chunks,
            file_hash=file_hash,
            total_pages=1,
            metadata={"fallback": True, "warning": "pymupdf not installed"},
        )


def _clean_pdf_text(text: str) -> str:
    """Убирает типичные артефакты PDF-парсинга."""
    # Убираем множественные пробелы
    text = re.sub(r"[ \t]+", " ", text)
    # Убираем множественные переносы строк
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Объединяем разорванные слова (дефис + перенос)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    return text.strip()
