"""Тесты RAG-пайплайна: поиск, контекст, генерация."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _setup_store_with_data():
    """Создаёт векторный индекс с тестовыми данными."""
    from spectrum.storage.vector import ChromaVectorStore
    from spectrum.processor.chunker import Chunk

    tmpdir = tempfile.mkdtemp()
    store = ChromaVectorStore(
        collection_name="test_rag",
        persist_dir=tmpdir,
    )

    chunks = [
        Chunk(
            chunk_id="rag-1",
            text="Договор №001 от 15 января 2024 года. Поставщик: ООО ТехноСтрой. Сумма: 1 950 000 руб. Срок поставки: 30 дней.",
            source_path="contract_001.pdf",
            source_hash="hash1",
            page_number=1,
            token_count=20,
        ),
        Chunk(
            chunk_id="rag-2",
            text="Договор №002 от 20 января 2024 года. Поставщик: ООО МеталлГрупп. Сумма: 3 880 000 руб. Срок поставки: 45 дней.",
            source_path="contract_002.pdf",
            source_hash="hash2",
            page_number=1,
            token_count=20,
        ),
        Chunk(
            chunk_id="rag-3",
            text="Труба стальная ДУ 100. Наружный диаметр: 108 мм. Толщина стенки: 6 мм. Материал: Сталь 20.",
            source_path="specification.pdf",
            source_hash="hash3",
            page_number=2,
            token_count=15,
        ),
    ]

    store.add_chunks(chunks)
    return store, tmpdir


def test_rag_ask_offline():
    """RAG: вопрос в оффлайн-режиме (без LLM)."""
    from spectrum.brain.rag import RAG

    store, tmpdir = _setup_store_with_data()

    try:
        rag = RAG(
            vector_store=store,
            api_url="",  # Нет LLM
            top_k=3,
            similarity_threshold=0.0,
        )

        response = rag.ask("Какой поставщик у договора 001?")

        assert response.question == "Какой поставщик у договора 001?"
        assert response.answer  # Не пустой
        assert len(response.sources) > 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rag_sources_traceability():
    """RAG: трейсабилити — источники содержат пути и страницы."""
    from spectrum.brain.rag import RAG

    store, tmpdir = _setup_store_with_data()

    try:
        rag = RAG(
            vector_store=store,
            api_url="",
            top_k=5,
            similarity_threshold=0.0,
        )

        response = rag.ask("Договоры поставки")

        for source in response.sources:
            assert source.source_path  # Есть путь
            # score >= 0
            assert source.score >= 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rag_response_to_dict():
    """RAG: сериализация ответа."""
    from spectrum.brain.rag import RAG

    store, tmpdir = _setup_store_with_data()

    try:
        rag = RAG(
            vector_store=store,
            api_url="",
            top_k=2,
            similarity_threshold=0.0,
        )

        response = rag.ask("Труба ДУ 100")
        d = response.to_dict()

        assert "answer" in d
        assert "sources" in d
        assert "question" in d
        assert isinstance(d["sources"], list)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rag_no_results():
    """RAG: запрос к пустому хранилищу."""
    from spectrum.brain.rag import RAG
    from spectrum.storage.vector import ChromaVectorStore

    tmpdir = tempfile.mkdtemp()
    try:
        store = ChromaVectorStore(
            collection_name="test_rag_empty",
            persist_dir=tmpdir,
        )
        rag = RAG(
            vector_store=store,
            api_url="",
            top_k=5,
            similarity_threshold=0.0,
        )

        response = rag.ask("Любой вопрос")
        assert "не нашёл" in response.answer.lower() or "нет данных" in response.answer.lower()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rag_build_context():
    """RAG: формирование контекста."""
    from spectrum.brain.rag import RAG
    from spectrum.storage.vector import SearchHit

    rag = RAG(vector_store=None, api_url="")

    hits = [
        SearchHit(
            chunk_id="1",
            text="Текст первый",
            score=0.95,
            source_path="file1.pdf",
            page_number=1,
        ),
        SearchHit(
            chunk_id="2",
            text="Текст второй",
            score=0.88,
            source_path="file2.xlsx",
            sheet_name="Sheet1",
        ),
    ]

    context = rag._build_context(hits)

    assert "Источник 1" in context
    assert "Источник 2" in context
    assert "file1.pdf" in context
    assert "file2.xlsx" in context
    assert "Текст первый" in context
    assert "Текст второй" in context
    assert "0.95" in context


def test_rag_hash_embed():
    """RAG: детерминированный embedding."""
    from spectrum.brain.rag import RAG

    v1 = RAG._hash_embed("hello world", dim=384)
    v2 = RAG._hash_embed("hello world", dim=384)
    v3 = RAG._hash_embed("different text", dim=384)

    assert len(v1) == 384
    assert v1 == v2  # Детерминированный
    assert v1 != v3  # Разный текст → разный вектор


def test_rag_system_prompts():
    """RAG: системные промпты."""
    from spectrum.brain.prompts import SystemPrompts

    sys_prompt = SystemPrompts.rag_system()
    assert "S.P.E.C.T.R.U.M." in sys_prompt
    assert "источник" in sys_prompt.lower()

    context_prompt = SystemPrompts.rag_with_context("Вопрос", "Контекст")
    assert "Вопрос" in context_prompt
    assert "Контекст" in context_prompt


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
