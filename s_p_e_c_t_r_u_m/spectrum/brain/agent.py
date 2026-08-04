"""Автономный агент: выполнение сложных задач на основе базы знаний."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..processor.pipeline import Pipeline, PipelineResult
from ..storage.file_store import FileStore
from ..storage.graph import SemanticGraph
from ..storage.vector import VectorStore
from .rag import RAG, RAGResponse
from .prompts import SystemPrompts

logger = logging.getLogger("spectrum.agent")


@dataclass
class TaskResult:
    """Результат выполнения задачи агентом."""
    task: str
    status: str                    # "completed", "partial", "failed"
    result: str                    # Текстовый результат
    data: dict[str, Any] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    processing_time_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status,
            "result": self.result,
            "data": self.data,
            "steps": self.steps,
            "sources_used": self.sources_used,
            "processing_time_s": round(self.processing_time_s, 2),
        }


class Agent:
    """Автономный агент S.P.E.C.T.R.U.M.

    Способен:
    - Индексировать файлы и директории
    - Отвечать на вопросы по базе знаний
    - Извлекать структурированные данные из документов
    - Сравнивать документы
    - Формировать отчёты
    """

    def __init__(
        self,
        vector_store: VectorStore,
        file_store: FileStore,
        graph: SemanticGraph | None = None,
        pipeline: Pipeline | None = None,
        rag: RAG | None = None,
    ):
        self._vector_store = vector_store
        self._file_store = file_store
        self._graph = graph
        self._pipeline = pipeline or Pipeline()
        self._rag = rag

    @classmethod
    def from_settings(cls, workspace_dir: str | None = None) -> Agent:
        """Создаёт агента из текущих настроек."""
        from ..config import settings
        from ..storage.vector import create_vector_store

        cfg = settings()
        ws = workspace_dir or cfg.workspace_dir

        file_store = FileStore(ws)
        graph = SemanticGraph(persist_path=f"{ws}/graph.json")

        vector_store = create_vector_store(
            backend=cfg.vector_store,
            collection_name=cfg.collection_name,
            host=cfg.qdrant_host,
            port=cfg.qdrant_port,
            persist_dir=cfg.chroma_persist_dir,
        )

        pipeline = Pipeline(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            use_vlm=cfg.use_vlm,
        )

        rag = RAG.from_settings(vector_store)

        return cls(
            vector_store=vector_store,
            file_store=file_store,
            graph=graph,
            pipeline=pipeline,
            rag=rag,
        )

    # --- Индексация ---

    def ingest_file(self, source: str, **kwargs) -> PipelineResult:
        """Индексирует один файл."""
        result = self._pipeline.process(source, **kwargs)

        if result.chunks:
            # Сохраняем в векторный индекс
            count = self._vector_store.add_chunks(result.chunks)
            logger.info("Indexed %d chunks from %s", count, source)

            # Добавляем в граф
            if self._graph and result.ingest_result:
                doc_node = self._graph.add_document(
                    result.ingest_result.source_path,
                    result.ingest_result.file_hash,
                )
                for chunk in result.chunks:
                    self._graph.add_chunk_node(
                        chunk.chunk_id, chunk.text, doc_node.node_id,
                    )
                self._graph.save()

            # Сохраняем файл
            try:
                self._file_store.store(source)
            except Exception as e:
                result.warnings.append(f"File store error: {e}")

        return result

    def ingest_directory(self, directory: str, recursive: bool = True, **kwargs) -> list[PipelineResult]:
        """Индексирует все файлы в директории."""
        results = self._pipeline.process_directory(directory, recursive=recursive, **kwargs)

        for result in results:
            if result.chunks:
                self._vector_store.add_chunks(result.chunks)
                if self._graph and result.ingest_result:
                    doc_node = self._graph.add_document(
                        result.ingest_result.source_path,
                        result.ingest_result.file_hash,
                    )
                    for chunk in result.chunks:
                        self._graph.add_chunk_node(
                            chunk.chunk_id, chunk.text, doc_node.node_id,
                        )

        if self._graph:
            self._graph.save()

        return results

    def ingest_url(self, url: str, render_js: bool = False, **kwargs) -> PipelineResult:
        """Индексирует веб-страницу."""
        result = self._pipeline.process(url, render_js=render_js, **kwargs)

        if result.chunks:
            self._vector_store.add_chunks(result.chunks)

        return result

    # --- Вопрос-Ответ ---

    def ask(self, question: str, **kwargs) -> RAGResponse:
        """Отвечает на вопрос по базе знаний."""
        if self._rag is None:
            from ..config import settings
            self._rag = RAG.from_settings(self._vector_store)
        return self._rag.ask(question, **kwargs)

    # --- Задачи ---

    def execute_task(self, task: str, **kwargs) -> TaskResult:
        """Выполняет комплексную задачу автономно."""
        start = time.time()
        steps: list[str] = []
        sources_used: list[str] = []

        try:
            # Шаг 1: Анализ задачи
            steps.append("Анализ задачи")
            task_type = self._classify_task(task)

            # Шаг 2: Поиск релевантных данных
            steps.append("Поиск в базе знаний")
            rag_response = self.ask(task, **kwargs)
            sources_used = list({s.source_path for s in rag_response.sources})

            # Шаг 3: Выполнение в зависимости от типа задачи
            if task_type == "extract":
                result = self._task_extract(task, rag_response)
            elif task_type == "compare":
                result = self._task_compare(task, rag_response)
            elif task_type == "summarize":
                result = self._task_summarize(task, rag_response)
            elif task_type == "report":
                result = self._task_report(task, rag_response)
            else:
                result = self._task_qa(task, rag_response)

            result.steps = steps
            result.sources_used = sources_used
            result.processing_time_s = time.time() - start
            return result

        except Exception as e:
            logger.exception("Task execution failed: %s", task)
            return TaskResult(
                task=task,
                status="failed",
                result=f"Ошибка выполнения: {e}",
                steps=steps,
                sources_used=sources_used,
                processing_time_s=time.time() - start,
            )

    def _classify_task(self, task: str) -> str:
        """Классифицирует тип задачи по ключевым словам."""
        task_lower = task.lower()

        if any(w in task_lower for w in ["извлеки", "extract", "заполни", "список полей", "структурируй"]):
            return "extract"
        if any(w in task_lower for w in ["сравни", "compare", "различия", "отличия"]):
            return "compare"
        if any(w in task_lower for w in ["суммаризируй", "summary", "кратко", "резюме"]):
            return "summarize"
        if any(w in task_lower for w in ["отчёт", "report", "сводк", "анализ"]):
            return "report"
        return "qa"

    def _task_qa(self, task: str, rag: RAGResponse) -> TaskResult:
        """Простой вопрос-ответ."""
        return TaskResult(
            task=task,
            status="completed",
            result=rag.answer,
            data=rag.to_dict(),
        )

    def _task_extract(self, task: str, rag: RAGResponse) -> TaskResult:
        """Извлечение структурированных данных."""
        return TaskResult(
            task=task,
            status="completed",
            result=rag.answer,
            data={"type": "extraction", **rag.to_dict()},
        )

    def _task_compare(self, task: str, rag: RAGResponse) -> TaskResult:
        """Сравнительный анализ."""
        return TaskResult(
            task=task,
            status="completed",
            result=rag.answer,
            data={"type": "comparison", **rag.to_dict()},
        )

    def _task_summarize(self, task: str, rag: RAGResponse) -> TaskResult:
        """Суммаризация."""
        return TaskResult(
            task=task,
            status="completed",
            result=rag.answer,
            data={"type": "summary", **rag.to_dict()},
        )

    def _task_report(self, task: str, rag: RAGResponse) -> TaskResult:
        """Формирование отчёта."""
        return TaskResult(
            task=task,
            status="completed",
            result=rag.answer,
            data={"type": "report", **rag.to_dict()},
        )

    # --- Управление базой ---

    def stats(self) -> dict[str, Any]:
        """Статистика базы знаний."""
        result: dict[str, Any] = {
            "vector_chunks": self._vector_store.count(),
            "files_stored": self._file_store.count(),
            "total_file_size_mb": round(self._file_store.total_size() / (1024 * 1024), 2),
        }
        if self._graph:
            result["graph"] = self._graph.stats()
        return result

    def clear_all(self) -> None:
        """Полная очистка базы знаний."""
        self._vector_store.clear()
        if self._graph:
            self._graph.clear()
            self._graph.save()

    def delete_source(self, source_path: str) -> int:
        """Удаляет все данные источника."""
        count = self._vector_store.delete_by_source(source_path)
        logger.info("Deleted %d chunks for %s", count, source_path)
        return count
