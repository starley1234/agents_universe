"""Тесты пайплайна обработки: полный цикл от файла до чанков."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_pipeline_csv():
    """Пайплайн: обработка CSV файла."""
    from spectrum.processor.pipeline import Pipeline

    p = Pipeline(chunk_size=200, chunk_overlap=50)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("Name,Age,City\n")
        for i in range(20):
            f.write(f"Person_{i},{20 + i},City_{i}\n")
        f.flush()
        csv_path = Path(f.name)

    result = p.process(csv_path)

    assert result.success
    assert result.chunk_count > 0
    assert result.processing_time_s >= 0
    assert result.ingest_result is not None

    csv_path.unlink()


def test_pipeline_csv_result_structure():
    """Пайплайн: структура результата CSV."""
    from spectrum.processor.pipeline import Pipeline

    p = Pipeline(chunk_size=500, chunk_overlap=50)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("Col1,Col2\nA,B\nC,D\n")
        f.flush()
        csv_path = Path(f.name)

    result = p.process(csv_path)

    assert result.source_path == str(csv_path)
    assert result.errors == []
    for chunk in result.chunks:
        assert chunk.source_path == str(csv_path)
        assert chunk.text.strip()

    csv_path.unlink()


def test_pipeline_nonexistent():
    """Пайплайн: обработка несуществующего файла."""
    from spectrum.processor.pipeline import Pipeline

    p = Pipeline()
    result = p.process("/nonexistent/file.pdf")

    assert not result.success
    assert len(result.errors) > 0


def test_pipeline_unsupported_format():
    """Пайплайн: неподдерживаемый формат."""
    from spectrum.processor.pipeline import Pipeline

    p = Pipeline()
    result = p.process(Path("test.xyz"))

    assert not result.success
    assert any("No ingestor" in e for e in result.errors)


def test_pipeline_batch():
    """Пайплайн: пакетная обработка."""
    from spectrum.processor.pipeline import Pipeline

    p = Pipeline(chunk_size=200, chunk_overlap=50)
    files = []

    for i in range(3):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
        f.write(f"Col1,Col2\nVal{i},Val{i}\n")
        f.flush()
        files.append(Path(f.name))
        f.close()

    results = p.process_batch(files)
    assert len(results) == 3

    for r in results:
        assert r.success

    for f in files:
        f.unlink()


def test_pipeline_directory():
    """Пайплайн: обработка директории."""
    from spectrum.processor.pipeline import Pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        # Создаём файлы
        for i in range(3):
            (Path(tmpdir) / f"data_{i}.csv").write_text(f"A,B\n{i},{i+1}\n")

        p = Pipeline(chunk_size=200, chunk_overlap=50)
        results = p.process_directory(tmpdir, recursive=False)

        assert len(results) == 3
        total_chunks = sum(r.chunk_count for r in results)
        assert total_chunks > 0


def test_pipeline_directory_not_found():
    """Пайплайн: несуществующая директория."""
    from spectrum.processor.pipeline import Pipeline

    p = Pipeline()
    results = p.process_directory("/nonexistent/dir")
    assert len(results) == 1
    assert not results[0].success


def test_pipeline_result_success():
    """PipelineResult: success property."""
    from spectrum.processor.pipeline import PipelineResult
    from spectrum.processor.chunker import Chunk

    r = PipelineResult(
        source_path="test.txt",
        chunks=[Chunk(
            chunk_id="c1", text="text", source_path="test.txt",
            source_hash="abc", char_offset=0, token_count=1,
        )],
    )
    assert r.success
    assert r.chunk_count == 1

    r2 = PipelineResult(source_path="test.txt", chunks=[], errors=["Error"])
    assert not r2.success


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
