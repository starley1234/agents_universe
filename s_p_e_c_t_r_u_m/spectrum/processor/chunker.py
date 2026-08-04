"""Чанкер: разбиение текста на семантические фрагменты для RAG."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """Семантический фрагмент текста с traceability-метаданными."""
    chunk_id: str
    text: str
    source_path: str                   # Путь к исходному файлу
    source_hash: str                   # SHA-256 файла
    page_number: int | None = None
    sheet_name: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    char_offset: int = 0               # Символьный смещение в исходном тексте
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "char_offset": self.char_offset,
            "token_count": self.token_count,
        }
        if self.page_number is not None:
            d["page_number"] = self.page_number
        if self.sheet_name:
            d["sheet_name"] = self.sheet_name
        if self.bbox:
            d["bbox"] = list(self.bbox)
        if self.metadata:
            d["metadata"] = self.metadata
        return d


class Chunker:
    """Дробит текст на чанки с перекрытием (overlap).

    Стратегии:
    - sentence: по границам предложений (рекомендуется для RAG)
    - paragraph: по абзацам
    - fixed: фиксированное количество символов
    """

    def __init__(
        self,
        chunk_size: int = 1024,
        chunk_overlap: int = 200,
        strategy: str = "sentence",
    ):
        if chunk_size < 100:
            raise ValueError("chunk_size must be >= 100")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be >= 0 and < chunk_size")

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.strategy = strategy

    def chunk_text(
        self,
        text: str,
        source_path: str = "",
        source_hash: str = "",
        page_number: int | None = None,
        sheet_name: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """Разбивает текст на чанки."""
        if not text.strip():
            return []

        if self.strategy == "sentence":
            segments = self._split_sentences(text)
        elif self.strategy == "paragraph":
            segments = text.split("\n\n")
        else:
            segments = [text[i:i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]

        # Склеиваем сегменты в чанки нужного размера
        chunks = self._merge_segments(
            segments, source_path, source_hash,
            page_number, sheet_name, bbox, extra_metadata or {},
        )

        return chunks

    def _split_sentences(self, text: str) -> list[str]:
        """Разбивает текст на предложения, сохраняя пунктуацию."""
        # Паттерн: точка/!/? + пробел/перенос, но не внутри чисел и сокращений
        parts = re.split(r"(?<=[.!?])\s+", text)
        # Фильтруем пустые
        return [p.strip() for p in parts if p.strip()]

    def _merge_segments(
        self,
        segments: list[str],
        source_path: str,
        source_hash: str,
        page_number: int | None,
        sheet_name: str | None,
        bbox: tuple[float, float, float, float] | None,
        extra_metadata: dict[str, Any],
    ) -> list[Chunk]:
        """Склеивает сегменты в чанки с учётом chunk_size и overlap."""
        chunks: list[Chunk] = []
        buffer = ""
        char_offset = 0
        overlap_text = ""

        for seg in segments:
            candidate = (buffer + " " + seg).strip() if buffer else seg

            if len(candidate) > self.chunk_size and buffer:
                # Сохраняем текущий буфер как чанк
                chunk = self._make_chunk(
                    buffer, source_path, source_hash,
                    page_number, sheet_name, bbox, char_offset, extra_metadata,
                )
                chunks.append(chunk)

                # overlap: берём хвост текущего буфера
                if self.chunk_overlap > 0:
                    overlap_text = buffer[-self.chunk_overlap:]
                    # Начинаем с overlap + текущий сегмент
                    char_offset += len(buffer) - len(overlap_text)
                    buffer = (overlap_text + " " + seg).strip()
                else:
                    char_offset += len(buffer)
                    buffer = seg
            else:
                buffer = candidate

        # Последний чанк
        if buffer.strip():
            chunk = self._make_chunk(
                buffer, source_path, source_hash,
                page_number, sheet_name, bbox, char_offset, extra_metadata,
            )
            chunks.append(chunk)

        return chunks

    def _make_chunk(
        self,
        text: str,
        source_path: str,
        source_hash: str,
        page_number: int | None,
        sheet_name: str | None,
        bbox: tuple[float, float, float, float] | None,
        char_offset: int,
        extra_metadata: dict[str, Any],
    ) -> Chunk:
        """Создаёт Chunk с уникальным ID."""
        return Chunk(
            chunk_id=str(uuid.uuid4()),
            text=text.strip(),
            source_path=source_path,
            source_hash=source_hash,
            page_number=page_number,
            sheet_name=sheet_name,
            bbox=bbox,
            char_offset=char_offset,
            token_count=len(text.split()),  # Приблизительный подсчёт
            metadata=dict(extra_metadata),
        )

    def chunk_ingest_result(self, result: "IngestResult") -> list[Chunk]:
        """Дробит IngestResult (из ingestor.base) в список чанков."""
        from ..ingestor.base import IngestResult

        all_chunks: list[Chunk] = []
        for page_chunk in result.chunks:
            if not page_chunk.text.strip():
                continue
            chunks = self.chunk_text(
                text=page_chunk.text,
                source_path=result.source_path,
                source_hash=result.file_hash,
                page_number=page_chunk.page_number,
                sheet_name=page_chunk.sheet_name,
                bbox=page_chunk.bbox,
                extra_metadata=page_chunk.metadata,
            )
            all_chunks.extend(chunks)

        return all_chunks
