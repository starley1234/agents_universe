"""Тесты ингесторов: PDF, Excel, URL, Image."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_ingestor_factory():
    """Фабрика ингесторов."""
    from spectrum.ingestor.factory import get_ingestor, supported_extensions, source_type_for
    from spectrum.ingestor.base import SourceType

    # PDF
    assert get_ingestor(Path("test.pdf")) is not None or get_ingestor("test.pdf") is not None

    # Excel
    assert get_ingestor(Path("test.xlsx")) is not None

    # Image
    assert get_ingestor(Path("test.png")) is not None

    # URL
    assert get_ingestor("https://example.com") is not None

    # Неизвестный формат
    assert get_ingestor(Path("test.xyz")) is None

    # Расширения
    exts = supported_extensions()
    assert ".pdf" in exts
    assert ".xlsx" in exts
    assert ".csv" in exts
    assert ".png" in exts

    # Типы
    assert source_type_for("test.pdf") == SourceType.PDF
    assert source_type_for("test.xlsx") == SourceType.EXCEL
    assert source_type_for("https://example.com") == SourceType.URL


def test_csv_ingestor():
    """Парсинг CSV файлов."""
    from spectrum.ingestor.excel import ExcelIngestor

    ing = ExcelIngestor()

    # Создаём CSV
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("Name,Age,City\n")
        f.write("Alice,30,Moscow\n")
        f.write("Bob,25,Petersburg\n")
        f.flush()
        csv_path = Path(f.name)

    assert ing.can_handle(csv_path)
    result = ing.ingest(csv_path)

    assert result.source_type.value == "excel"
    assert result.chunk_count >= 1
    assert "Alice" in result.chunks[0].text
    assert "Bob" in result.chunks[0].text
    assert result.file_hash  # Не пустой

    csv_path.unlink()


def test_csv_markdown_format():
    """CSV конвертируется в markdown-таблицу."""
    from spectrum.ingestor.excel import ExcelIngestor

    ing = ExcelIngestor()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("Product,Price,Qty\n")
        f.write("Widget,100.50,5\n")
        f.flush()
        csv_path = Path(f.name)

    result = ing.ingest(csv_path)
    text = result.chunks[0].text

    # Проверяем markdown-формат
    assert "|" in text
    assert "Product" in text
    assert "---" in text
    assert "Widget" in text

    csv_path.unlink()


def test_text_ingestor():
    """Парсинг текстовых файлов через PDF ingestor (фолбэк)."""
    from spectrum.ingestor.factory import get_ingestor, source_type_for
    from spectrum.ingestor.base import SourceType

    # Текстовые файлы пока не поддерживаются напрямую
    assert source_type_for("test.txt") == SourceType.TEXT


def test_url_ingestor_can_handle():
    """URL ingestor: определение URL."""
    from spectrum.ingestor.url import URLIngestor

    ing = URLIngestor()
    assert ing.can_handle("https://example.com")
    assert ing.can_handle("http://test.org/page")
    assert not ing.can_handle("/local/path/file.txt")
    assert not ing.can_handle(Path("file.txt"))


def test_url_ingestor_regex_extraction():
    """URL ingestor: regex-парсинг HTML."""
    from spectrum.ingestor.url import URLIngestor

    ing = URLIngestor()

    html = "<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p></body></html>"
    text, meta = ing._extract_regex(html)

    assert "Hello" in text
    assert "World" in text
    assert meta.get("title") == "Test"


def test_image_ingestor_can_handle():
    """Image ingestor: определение изображений."""
    from spectrum.ingestor.image import ImageIngestor

    ing = ImageIngestor()
    assert ing.can_handle(Path("test.png"))
    assert ing.can_handle(Path("test.jpg"))
    assert ing.can_handle(Path("test.jpeg"))
    assert ing.can_handle(Path("test.tiff"))
    assert not ing.can_handle(Path("test.pdf"))
    assert not ing.can_handle(Path("test.txt"))


def test_pdf_ingestor_fallback():
    """PDF ingestor: фолбэк без pymupdf."""
    from spectrum.ingestor.pdf import PDFIngestor

    ing = PDFIngestor()

    # Создаём файл-заглушку
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 test content")
        f.flush()
        pdf_path = Path(f.name)

    assert ing.can_handle(pdf_path)

    pdf_path.unlink()


def test_ingest_result_hash():
    """IngestResult: хеширование."""
    from spectrum.ingestor.base import IngestResult

    h1 = IngestResult.compute_hash(b"hello world")
    h2 = IngestResult.compute_hash(b"hello world")
    h3 = IngestResult.compute_hash(b"different")

    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # SHA-256


def test_ingest_result_full_text():
    """IngestResult: конкатенация текста."""
    from spectrum.ingestor.base import IngestResult, PageChunk, SourceType

    result = IngestResult(
        source_path="test.txt",
        source_type=SourceType.TEXT,
        chunks=[
            PageChunk(text="First part."),
            PageChunk(text="Second part."),
            PageChunk(text=""),  # Пустой — не включается
        ],
    )
    assert "First part." in result.full_text
    assert "Second part." in result.full_text
    assert result.chunk_count == 3


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
