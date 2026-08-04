"""Тесты векторного хранилища: ChromaDB и поиск."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_chunks(n: int = 3):
    """Создаёт тестовые чанки."""
    from spectrum.processor.chunker import Chunk

    chunks = []
    for i in range(n):
        chunks.append(Chunk(
            chunk_id=f"test-chunk-{i}",
            text=f"Тестовый текст номер {i} с уникальным содержимым для проверки поиска.",
            source_path=f"test_{i}.txt",
            source_hash=f"hash_{i}",
            page_number=i + 1,
            token_count=10,
        ))
    return chunks


def test_chroma_store_add_and_count():
    """ChromaDB: добавление и подсчёт."""
    from spectrum.storage.vector import ChromaVectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ChromaVectorStore(
            collection_name="test_add",
            persist_dir=tmpdir,
        )
        assert store.count() == 0

        chunks = _make_chunks(3)
        added = store.add_chunks(chunks)
        assert added == 3
        assert store.count() == 3


def test_chroma_store_search():
    """ChromaDB: поиск по embedding."""
    from spectrum.storage.vector import ChromaVectorStore
    from spectrum.brain.rag import RAG

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ChromaVectorStore(
            collection_name="test_search",
            persist_dir=tmpdir,
        )

        chunks = _make_chunks(3)
        store.add_chunks(chunks)

        # Генерируем embedding для запроса
        query = "номер 1"
        embedding = RAG._hash_embed(query, dim=384)

        hits = store.search(embedding, top_k=2)
        assert len(hits) > 0
        assert hits[0].score >= 0
        assert hits[0].text
        assert hits[0].source_path


def test_chroma_store_delete_by_source():
    """ChromaDB: удаление по источнику."""
    from spectrum.storage.vector import ChromaVectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ChromaVectorStore(
            collection_name="test_delete",
            persist_dir=tmpdir,
        )

        chunks = _make_chunks(3)
        store.add_chunks(chunks)
        assert store.count() == 3

        deleted = store.delete_by_source("test_0.txt")
        assert deleted == 1
        assert store.count() == 2


def test_chroma_store_clear():
    """ChromaDB: полная очистка."""
    from spectrum.storage.vector import ChromaVectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ChromaVectorStore(
            collection_name="test_clear",
            persist_dir=tmpdir,
        )

        store.add_chunks(_make_chunks(5))
        assert store.count() == 5

        store.clear()
        assert store.count() == 0


def test_chroma_store_empty_search():
    """ChromaDB: поиск в пустом хранилище."""
    from spectrum.storage.vector import ChromaVectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ChromaVectorStore(
            collection_name="test_empty",
            persist_dir=tmpdir,
        )

        hits = store.search([0.1] * 384, top_k=5)
        assert hits == []


def test_chroma_store_add_empty():
    """ChromaDB: добавление пустого списка."""
    from spectrum.storage.vector import ChromaVectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ChromaVectorStore(
            collection_name="test_add_empty",
            persist_dir=tmpdir,
        )

        added = store.add_chunks([])
        assert added == 0
        assert store.count() == 0


def test_chroma_search_hit_citation():
    """SearchHit: форматирование цитаты."""
    from spectrum.storage.vector import SearchHit

    hit = SearchHit(
        chunk_id="test",
        text="some text",
        score=0.95,
        source_path="/path/to/contract.pdf",
        page_number=3,
        sheet_name=None,
    )
    citation = hit.citation()
    assert "contract.pdf" in citation
    assert "стр. 3" in citation


def test_chroma_search_hit_citation_excel():
    """SearchHit: цитата для Excel."""
    from spectrum.storage.vector import SearchHit

    hit = SearchHit(
        chunk_id="test",
        text="table data",
        score=0.88,
        source_path="/data/prices.xlsx",
        page_number=None,
        sheet_name="Prices",
    )
    citation = hit.citation()
    assert "prices.xlsx" in citation
    assert "Prices" in citation


def test_vector_store_factory():
    """Фабрика векторных хранилищ."""
    from spectrum.storage.vector import create_vector_store

    with tempfile.TemporaryDirectory() as tmpdir:
        store = create_vector_store(
            backend="chroma",
            collection_name="test_factory",
            persist_dir=tmpdir,
        )
        assert store.count() == 0


def test_chroma_store_persistence():
    """ChromaDB: персистентность (пересоздание)."""
    from spectrum.storage.vector import ChromaVectorStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store1 = ChromaVectorStore(
            collection_name="test_persist",
            persist_dir=tmpdir,
        )
        store1.add_chunks(_make_chunks(3))
        assert store1.count() == 3

        # Пересоздаём с тем же путём
        store2 = ChromaVectorStore(
            collection_name="test_persist",
            persist_dir=tmpdir,
        )
        assert store2.count() == 3


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {test_fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test_fn.__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
