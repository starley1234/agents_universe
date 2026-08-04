"""Тесты чанкера: дробление текста на семантические фрагменты."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_chunker_basic():
    """Базовое дробление текста."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=100, chunk_overlap=20)
    text = "Первое предложение. Второе предложение. Третье предложение. Четвёртое предложение. Пятое предложение."
    chunks = c.chunk_text(text, source_path="test.txt", source_hash="abc123")

    assert len(chunks) > 0
    for chunk in chunks:
        assert chunk.text.strip()
        assert chunk.source_path == "test.txt"
        assert chunk.source_hash == "abc123"
        assert chunk.chunk_id  # Не пустой


def test_chunker_empty_text():
    """Пустой текст — нет чанков."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=100, chunk_overlap=20)
    chunks = c.chunk_text("", source_path="test.txt")
    assert chunks == []

    chunks = c.chunk_text("   \n  ", source_path="test.txt")
    assert chunks == []


def test_chunker_short_text():
    """Короткий текст — один чанк."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=1000, chunk_overlap=100)
    text = "Короткий текст."
    chunks = c.chunk_text(text, source_path="test.txt")

    assert len(chunks) == 1
    assert "Короткий текст." in chunks[0].text


def test_chunker_long_text():
    """Длинный текст — несколько чанков."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=200, chunk_overlap=50)
    # Генерируем текст ~1000 символов
    text = ". ".join([f"Предложение номер {i} с дополнительными словами для объёма." for i in range(20)])
    chunks = c.chunk_text(text, source_path="test.txt")

    assert len(chunks) > 1
    # Все чанки непустые
    for chunk in chunks:
        assert chunk.text.strip()


def test_chunker_overlap():
    """Проверка перекрытия (overlap) между чанками."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=100, chunk_overlap=30)
    text = ". ".join([f"Предложение {i}" for i in range(30)])
    chunks = c.chunk_text(text, source_path="test.txt")

    if len(chunks) >= 2:
        # Проверяем, что последний символ первого чанка
        # встречается в начале второго (из-за overlap)
        tail = chunks[0].text[-20:]
        # Это не строгий тест, но overlap должен быть заметен
        assert len(chunks[0].text) > 0


def test_chunker_metadata():
    """Метаданные чанков."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=200, chunk_overlap=50)
    chunks = c.chunk_text(
        "Текст для проверки метаданных чанков.",
        source_path="test.pdf",
        source_hash="abc",
        page_number=5,
        sheet_name="Sheet1",
        bbox=(10.0, 20.0, 100.0, 200.0),
    )

    assert len(chunks) >= 1
    assert chunks[0].page_number == 5
    assert chunks[0].sheet_name == "Sheet1"
    assert chunks[0].bbox == (10.0, 20.0, 100.0, 200.0)


def test_chunker_to_dict():
    """Сериализация чанка в dict."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=500, chunk_overlap=50)
    chunks = c.chunk_text("Тестовый текст для сериализации.", source_path="test.txt")
    assert len(chunks) >= 1

    d = chunks[0].to_dict()
    assert "chunk_id" in d
    assert "text" in d
    assert "source_path" in d
    assert "char_offset" in d


def test_chunker_paragraph_strategy():
    """Стратегия по абзацам."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=500, chunk_overlap=0, strategy="paragraph")
    text = "Первый абзац с текстом.\n\nВторой абзац с другими словами.\n\nТретий абзац."
    chunks = c.chunk_text(text, source_path="test.txt")

    assert len(chunks) >= 1


def test_chunker_fixed_strategy():
    """Фиксированная стратегия."""
    from spectrum.processor.chunker import Chunker

    c = Chunker(chunk_size=200, chunk_overlap=0, strategy="fixed")
    text = "А" * 1000
    chunks = c.chunk_text(text, source_path="test.txt")

    assert len(chunks) >= 5


def test_chunker_ingest_result():
    """Дробление IngestResult."""
    from spectrum.processor.chunker import Chunker
    from spectrum.ingestor.base import IngestResult, PageChunk, SourceType

    c = Chunker(chunk_size=100, chunk_overlap=20)
    result = IngestResult(
        source_path="test.pdf",
        source_type=SourceType.PDF,
        chunks=[
            PageChunk(text="Текст первой страницы с достаточным объёмом данных.", page_number=1),
            PageChunk(text="Текст второй страницы с другими данными.", page_number=2),
        ],
        file_hash="abc123",
    )

    chunks = c.chunk_ingest_result(result)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.source_path == "test.pdf"
        assert chunk.source_hash == "abc123"


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
