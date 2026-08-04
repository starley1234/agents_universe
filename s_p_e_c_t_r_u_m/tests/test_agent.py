"""Тесты автономного агента: индексация, задачи, статистика."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _create_test_csv(directory: Path, name: str = "test.csv") -> Path:
    f = directory / name
    f.write_text("Product,Price,Qty\nWidget,100,5\nGadget,200,3\n", encoding="utf-8")
    return f


def _create_test_txt(directory: Path, name: str, content: str) -> Path:
    f = directory / name
    f.write_text(content, encoding="utf-8")
    return f


def _make_agent():
    """Создаёт тестового агента."""
    from spectrum.brain.agent import Agent
    from spectrum.storage.vector import ChromaVectorStore
    from spectrum.storage.file_store import FileStore
    from spectrum.storage.graph import SemanticGraph
    from spectrum.processor.pipeline import Pipeline

    tmpdir = tempfile.mkdtemp()
    store = ChromaVectorStore(
        collection_name="test_agent",
        persist_dir=f"{tmpdir}/chroma",
    )
    file_store = FileStore(tmpdir)
    graph = SemanticGraph(persist_path=f"{tmpdir}/graph.json")
    pipeline = Pipeline(chunk_size=200, chunk_overlap=50)

    agent = Agent(
        vector_store=store,
        file_store=file_store,
        graph=graph,
        pipeline=pipeline,
    )
    return agent, tmpdir


def test_agent_ingest_file():
    """Агент: индексация одного файла."""
    agent, tmpdir = _make_agent()

    try:
        csv_path = _create_test_csv(Path(tmpdir))
        result = agent.ingest_file(str(csv_path))

        assert result.success
        assert result.chunk_count > 0
        assert agent._vector_store.count() > 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_ingest_directory():
    """Агент: индексация директории."""
    agent, tmpdir = _make_agent()

    try:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        _create_test_csv(data_dir, "file1.csv")
        _create_test_csv(data_dir, "file2.csv")
        _create_test_txt(data_dir, "readme.txt", "This is a test document with some content for indexing.")

        results = agent.ingest_directory(str(data_dir))
        assert len(results) >= 2

        total_chunks = sum(r.chunk_count for r in results)
        assert total_chunks > 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_stats():
    """Агент: статистика."""
    agent, tmpdir = _make_agent()

    try:
        csv_path = _create_test_csv(Path(tmpdir))
        agent.ingest_file(str(csv_path))

        stats = agent.stats()
        assert "vector_chunks" in stats
        assert stats["vector_chunks"] > 0
        assert "files_stored" in stats
        assert stats["files_stored"] >= 0
        assert "graph" in stats
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_ask():
    """Агент: вопрос-ответ."""
    agent, tmpdir = _make_agent()

    try:
        # Индексируем данные
        csv_path = _create_test_csv(Path(tmpdir))
        agent.ingest_file(str(csv_path))

        # Задаём вопрос
        response = agent.ask("Какие продукты?")
        assert response.question == "Какие продукты?"
        assert response.answer
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_delete_source():
    """Агент: удаление источника."""
    agent, tmpdir = _make_agent()

    try:
        csv_path = _create_test_csv(Path(tmpdir))
        agent.ingest_file(str(csv_path))

        count_before = agent._vector_store.count()
        assert count_before > 0

        deleted = agent.delete_source(str(csv_path))
        assert deleted >= 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_clear_all():
    """Агент: полная очистка."""
    agent, tmpdir = _make_agent()

    try:
        csv_path = _create_test_csv(Path(tmpdir))
        agent.ingest_file(str(csv_path))
        assert agent._vector_store.count() > 0

        agent.clear_all()
        assert agent._vector_store.count() == 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_task_classification():
    """Агент: классификация задач."""
    agent, tmpdir = _make_agent()

    try:
        assert agent._classify_task("Извлеки данные из договора") == "extract"
        assert agent._classify_task("Сравни договоры") == "compare"
        assert agent._classify_task("Суммаризируй документ") == "summarize"
        assert agent._classify_task("Составь отчёт") == "report"
        assert agent._classify_task("Что написано в договоре?") == "qa"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_execute_task():
    """Агент: выполнение задачи."""
    agent, tmpdir = _make_agent()

    try:
        csv_path = _create_test_csv(Path(tmpdir))
        agent.ingest_file(str(csv_path))

        result = agent.execute_task("Какие продукты указаны?")
        assert result.task == "Какие продукты указаны?"
        assert result.status in ("completed", "partial", "failed")
        assert result.result
        assert result.processing_time_s >= 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_task_result_to_dict():
    """TaskResult: сериализация."""
    from spectrum.brain.agent import TaskResult

    r = TaskResult(
        task="Test task",
        status="completed",
        result="Result text",
        data={"key": "value"},
        steps=["step1", "step2"],
        sources_used=["file.pdf"],
        processing_time_s=1.5,
    )

    d = r.to_dict()
    assert d["task"] == "Test task"
    assert d["status"] == "completed"
    assert d["steps"] == ["step1", "step2"]
    assert d["processing_time_s"] == 1.5


def test_agent_ingest_nonexistent():
    """Агент: индексация несуществующего файла."""
    agent, tmpdir = _make_agent()

    try:
        result = agent.ingest_file("/nonexistent/file.csv")
        assert not result.success
        assert len(result.errors) > 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


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
