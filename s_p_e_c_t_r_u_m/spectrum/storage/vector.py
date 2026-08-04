"""Векторное хранилище: индексация и поиск по смыслу (Qdrant / ChromaDB)."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..processor.chunker import Chunk

logger = logging.getLogger("spectrum.vector")


@dataclass
class SearchHit:
    """Результат поиска: чанк + релевантность + traceability."""
    chunk_id: str
    text: str
    score: float
    source_path: str
    page_number: int | None = None
    sheet_name: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        """Форматирует источник для цитирования."""
        parts = [f"📄 {self.source_path}"]
        if self.page_number is not None:
            parts.append(f"стр. {self.page_number}")
        if self.sheet_name:
            parts.append(f"лист: {self.sheet_name}")
        return ", ".join(parts)


class VectorStore(ABC):
    """Абстрактный векторный индекс."""

    @abstractmethod
    def add_chunks(self, chunks: list[Chunk], embeddings: list[list[float]] | None = None) -> int:
        """Добавить чанки в индекс. Возвращает количество добавленных."""
        ...

    @abstractmethod
    def search(self, query_embedding: list[float], top_k: int = 5) -> list[SearchHit]:
        """Поиск по векторному представлению запроса."""
        ...

    @abstractmethod
    def delete_by_source(self, source_path: str) -> int:
        """Удалить все чанки из источника. Возвращает количество удалённых."""
        ...

    @abstractmethod
    def count(self) -> int:
        """Количество чанков в индексе."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Полностью очистить индекс."""
        ...


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    """Детерминированный embedding из хеша (для тестов и оффлайн-режима)."""
    h = hashlib.sha256(text.encode()).digest()
    vec: list[float] = []
    while len(vec) < dim:
        for byte in h:
            vec.append((byte / 255.0) * 2 - 1)
            if len(vec) >= dim:
                break
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm > 0 else vec


class _HashEmbeddingFunction:
    """ChromaDB-совместимая embedding function на основе хеша.

    Работает полностью оффлайн, без скачивания моделей.
    """

    def __init__(self, dim: int = 384):
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [_hash_embed(text, self._dim) for text in input]

    def name(self) -> str:
        return "hash-embed"


class ChromaVectorStore(VectorStore):
    """Векторный индекс на ChromaDB (встраиваемый, без Docker).

    Использует hash-based embedding function для работы в оффлайн-режиме
    (без необходимости скачивать модели).
    """

    def __init__(self, collection_name: str = "spectrum", persist_dir: str = "data/chroma"):
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
        except ImportError:
            raise ImportError("chromadb not installed: pip install chromadb")

        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False, is_persistent=True),
        )

        # Используем hash-based embedding function — работает оффлайн
        ef = _HashEmbeddingFunction(dim=384)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=ef,
        )
        logger.info("ChromaDB collection '%s' ready, %d vectors", collection_name, self.count())

    def add_chunks(self, chunks: list[Chunk], embeddings: list[list[float]] | None = None) -> int:
        if not chunks:
            return 0

        ids = [c.chunk_id for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = []
        for c in chunks:
            m = {
                "source_path": c.source_path,
                "source_hash": c.source_hash,
                "char_offset": c.char_offset,
                "token_count": c.token_count,
            }
            if c.page_number is not None:
                m["page_number"] = c.page_number
            if c.sheet_name:
                m["sheet_name"] = c.sheet_name
            metadatas.append(m)

        kwargs: dict[str, Any] = {
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
        }
        if embeddings:
            kwargs["embeddings"] = embeddings

        self._collection.add(**kwargs)
        logger.info("Added %d chunks to ChromaDB", len(chunks))
        return len(chunks)

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[SearchHit]:
        if self.count() == 0:
            return []

        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, self.count()),
            include=["documents", "metadatas", "distances"],
        )

        hits: list[SearchHit] = []
        for i in range(len(results["ids"][0])):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            dist = results["distances"][0][i] if results["distances"] else 0.0
            # ChromaDB возвращает distance, конвертируем в similarity
            score = 1.0 - dist

            bbox = None
            if "bbox" in meta:
                bbox = tuple(meta["bbox"])

            hits.append(SearchHit(
                chunk_id=results["ids"][0][i],
                text=results["documents"][0][i],
                score=score,
                source_path=meta.get("source_path", ""),
                page_number=meta.get("page_number"),
                sheet_name=meta.get("sheet_name"),
                bbox=bbox,
                metadata=meta,
            ))

        return hits

    def delete_by_source(self, source_path: str) -> int:
        results = self._collection.get(
            where={"source_path": source_path},
        )
        if results["ids"]:
            self._collection.delete(ids=results["ids"])
            return len(results["ids"])
        return 0

    def count(self) -> int:
        return self._collection.count()

    def clear(self) -> None:
        # Пересоздаём коллекцию
        name = self._collection.name
        self._client.delete_collection(name)
        ef = _HashEmbeddingFunction(dim=384)
        self._collection = self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=ef,
        )


class QdrantVectorStore(VectorStore):
    """Векторный индекс на Qdrant (Docker-сервис)."""

    def __init__(
        self,
        collection_name: str = "spectrum",
        host: str = "localhost",
        port: int = 6333,
        vector_size: int = 384,
    ):
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError:
            raise ImportError("qdrant-client not installed: pip install qdrant-client")

        self._client = QdrantClient(host=host, port=port)
        self._collection_name = collection_name
        self._vector_size = vector_size

        # Создаём коллекцию, если не существует
        collections = [c.name for c in self._client.get_collections().collections]
        if collection_name not in collections:
            self._client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            logger.info("Created Qdrant collection '%s'", collection_name)

    def add_chunks(self, chunks: list[Chunk], embeddings: list[list[float]] | None = None) -> int:
        if not chunks:
            return 0

        from qdrant_client.models import PointStruct

        points = []
        for i, chunk in enumerate(chunks):
            vector = embeddings[i] if embeddings else self._hash_vector(chunk.text)
            payload = {
                "text": chunk.text,
                "source_path": chunk.source_path,
                "source_hash": chunk.source_hash,
                "char_offset": chunk.char_offset,
                "token_count": chunk.token_count,
            }
            if chunk.page_number is not None:
                payload["page_number"] = chunk.page_number
            if chunk.sheet_name:
                payload["sheet_name"] = chunk.sheet_name

            points.append(PointStruct(
                id=hashlib.md5(chunk.chunk_id.encode()).hexdigest()[:16],
                vector=vector,
                payload=payload,
            ))

        self._client.upsert(collection_name=self._collection_name, points=points)
        return len(chunks)

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[SearchHit]:
        results = self._client.search(
            collection_name=self._collection_name,
            query_vector=query_embedding,
            limit=top_k,
        )

        hits = []
        for r in results:
            p = r.payload or {}
            hits.append(SearchHit(
                chunk_id=str(r.id),
                text=p.get("text", ""),
                score=r.score,
                source_path=p.get("source_path", ""),
                page_number=p.get("page_number"),
                sheet_name=p.get("sheet_name"),
                metadata=p,
            ))
        return hits

    def delete_by_source(self, source_path: str) -> int:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        results = self._client.scroll(
            collection_name=self._collection_name,
            scroll_filter=Filter(must=[
                FieldCondition(key="source_path", match=MatchValue(value=source_path)),
            ]),
        )
        ids = [r.id for r in results[0]]
        if ids:
            self._client.delete(collection_name=self._collection_name, points_selector=ids)
        return len(ids)

    def count(self) -> int:
        info = self._client.get_collection(self._collection_name)
        return info.points_count or 0

    def clear(self) -> None:
        self._client.delete_collection(self._collection_name)
        from qdrant_client.models import Distance, VectorParams
        self._client.create_collection(
            collection_name=self._collection_name,
            vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
        )

    def _hash_vector(self, text: str) -> list[float]:
        """Детерминированный вектор из текста (для тестов без embedding-модели)."""
        return _hash_embed(text, self._vector_size)


def create_vector_store(
    backend: str = "chroma",
    collection_name: str = "spectrum",
    **kwargs: Any,
) -> VectorStore:
    """Фабрика векторных хранилищ."""
    if backend == "qdrant":
        return QdrantVectorStore(
            collection_name=collection_name,
            host=kwargs.get("host", "localhost"),
            port=kwargs.get("port", 6333),
            vector_size=kwargs.get("vector_size", 384),
        )
    # По умолчанию — ChromaDB
    return ChromaVectorStore(
        collection_name=collection_name,
        persist_dir=kwargs.get("persist_dir", "data/chroma"),
    )
