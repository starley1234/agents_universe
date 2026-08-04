"""Ингестор текстовых файлов: .txt, .md, .log и другие текстовые форматы."""

from __future__ import annotations

from pathlib import Path

from .base import IngestResult, Ingestor, PageChunk, SourceType


class TextIngestor(Ingestor):
    """Извлекает текст из обычных текстовых файлов."""

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".log", ".rst", ".json", ".xml", ".yaml", ".yml"}

    def can_handle(self, source: str | Path) -> bool:
        p = Path(source) if isinstance(source, str) else source
        return p.suffix.lower() in self.SUPPORTED_EXTENSIONS

    def ingest(self, source: str | Path, **kwargs) -> IngestResult:
        path = Path(source)
        data = self._read_bytes(path)
        file_hash = IngestResult.compute_hash(data)

        text = data.decode("utf-8", errors="replace")
        chunks: list[PageChunk] = []

        if text.strip():
            chunks.append(PageChunk(
                text=text,
                page_number=1,
                metadata={"encoding": "utf-8"},
            ))

        return IngestResult(
            source_path=str(path),
            source_type=SourceType.TEXT,
            chunks=chunks,
            file_hash=file_hash,
            total_pages=1,
            metadata={"format": "text"},
        )
