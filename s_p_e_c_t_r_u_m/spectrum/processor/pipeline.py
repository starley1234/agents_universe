"""Пайплайн обработки: полный цикл от файла до чанков в хранилище."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingestor.base import IngestResult, Ingestor
from ..ingestor.factory import get_ingestor, source_type_for
from .chunker import Chunk, Chunker

logger = logging.getLogger("spectrum.pipeline")


@dataclass
class PipelineResult:
    """Результат обработки одного файла через пайплайн."""
    source_path: str
    chunks: list[Chunk]
    ingest_result: IngestResult | None = None
    processing_time_s: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.chunks) > 0 and len(self.errors) == 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


class Pipeline:
    """Оркестратор: принимает путь/URL → ингест → чанкинг → готовые чанки.

    Этапы:
    1. Определение типа источника
    2. Выбор ингестора (каскад)
    3. Извлечение текста (IngestResult)
    4. Дробление на чанки
    5. (Опционально) VLM-анализ сложных страниц
    """

    def __init__(
        self,
        chunk_size: int = 1024,
        chunk_overlap: int = 200,
        use_vlm: bool = False,
        chunking_strategy: str = "sentence",
    ):
        self.chunker = Chunker(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            strategy=chunking_strategy,
        )
        self.use_vlm = use_vlm
        self._vlm_analyzer = None

    def process(self, source: str | Path, **kwargs) -> PipelineResult:
        """Обрабатывает один источник: файл или URL."""
        start = time.time()
        source_str = str(source)
        errors: list[str] = []
        warnings: list[str] = []

        try:
            # 1. Определяем тип
            source_type = source_type_for(source)
            logger.info("Processing %s (type=%s)", source_str, source_type.value)

            # 2. Выбираем ингестор
            ingestor = get_ingestor(source)
            if ingestor is None:
                errors.append(f"No ingestor for source: {source_str}")
                return PipelineResult(
                    source_path=source_str,
                    chunks=[],
                    errors=errors,
                    processing_time_s=time.time() - start,
                )

            # 3. Извлекаем текст
            ingest_result = ingestor.ingest(source, **kwargs)
            if not ingest_result.chunks:
                warnings.append(f"No text extracted from {source_str}")

            # 4. VLM-анализ для сложных страниц (если включён)
            if self.use_vlm and ingest_result.chunks:
                self._apply_vlm_analysis(ingest_result, **kwargs)

            # 5. Дробим на чанки
            chunks = self.chunker.chunk_ingest_result(ingest_result)

            logger.info(
                "Processed %s: %d pages → %d chunks",
                source_str, ingest_result.total_pages, len(chunks),
            )

            return PipelineResult(
                source_path=source_str,
                chunks=chunks,
                ingest_result=ingest_result,
                processing_time_s=time.time() - start,
                errors=errors,
                warnings=warnings,
            )

        except Exception as e:
            logger.exception("Error processing %s", source_str)
            errors.append(str(e))
            return PipelineResult(
                source_path=source_str,
                chunks=[],
                errors=errors,
                processing_time_s=time.time() - start,
            )

    def process_batch(self, sources: list[str | Path], **kwargs) -> list[PipelineResult]:
        """Обрабатывает список источников последовательно."""
        results = []
        for source in sources:
            result = self.process(source, **kwargs)
            results.append(result)
        return results

    def process_directory(self, directory: str | Path, recursive: bool = True, **kwargs) -> list[PipelineResult]:
        """Обрабатывает все файлы в директории."""
        dir_path = Path(directory)
        if not dir_path.is_dir():
            return [PipelineResult(
                source_path=str(dir_path),
                chunks=[],
                errors=[f"Not a directory: {dir_path}"],
            )]

        from ..ingestor.factory import supported_extensions

        pattern = "**/*" if recursive else "*"
        files = [
            f for f in dir_path.glob(pattern)
            if f.is_file() and f.suffix.lower() in supported_extensions()
        ]

        logger.info("Found %d files in %s", len(files), dir_path)
        return self.process_batch(files, **kwargs)

    def _apply_vlm_analysis(self, ingest_result: IngestResult, **kwargs) -> None:
        """Применяет VLM-анализ к страницам без текста (пустые/сложные)."""
        from ..ingestor.image import ImageIngestor

        if self._vlm_analyzer is None:
            try:
                from .vlm_analyzer import VLMAnalyzer
                self._vlm_analyzer = VLMAnalyzer.from_settings()
            except Exception:
                return

        # Если PDF и есть страницы без текста — пробуем VLM
        if ingest_result.source_type.value == "pdf":
            empty_pages = [
                c for c in ingest_result.chunks
                if not c.text.strip() and c.page_number is not None
            ]
            if empty_pages:
                logger.info("VLM analysis for %d empty PDF pages", len(empty_pages))
                # Тут можно добавить конвертацию страниц PDF в изображения
                # и отправку в VLM — оставлено как расширение
