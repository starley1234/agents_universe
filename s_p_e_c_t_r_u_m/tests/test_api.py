"""Тесты REST API: маршруты, модели, ответы."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_fastapi_import():
    """FastAPI: проверка доступности."""
    try:
        import fastapi
        assert True
    except ImportError:
        print("  ⚠️  FastAPI not installed, skipping API tests")
        return "skip"


def test_pydantic_models():
    """Pydantic модели: создание и валидация."""
    try:
        from spectrum.api.app import AskRequest, IngestURLRequest, TaskRequest
    except ImportError:
        print("  ⚠️  FastAPI not installed, skipping")
        return "skip"

    req = AskRequest(question="Test question?", top_k=3)
    assert req.question == "Test question?"
    assert req.top_k == 3

    url_req = IngestURLRequest(url="https://example.com", render_js=False)
    assert url_req.url == "https://example.com"

    task_req = TaskRequest(task="Analyze all contracts")
    assert task_req.task == "Analyze all contracts"


def test_create_app():
    """API: создание приложения."""
    try:
        from spectrum.api.app import create_app
    except ImportError:
        print("  ⚠️  FastAPI not installed, skipping")
        return "skip"

    app = create_app()
    assert app is not None
    assert app.title == "S.P.E.C.T.R.U.M."


def test_api_routes_exist():
    """API: проверка наличия маршрутов."""
    try:
        from spectrum.api.app import create_app
    except ImportError:
        print("  ⚠️  FastAPI not installed, skipping")
        return "skip"

    app = create_app()
    routes = [r.path for r in app.routes]

    assert "/health" in routes
    assert "/api/ask" in routes
    assert "/api/ingest/file" in routes
    assert "/api/ingest/url" in routes
    assert "/api/stats" in routes
    assert "/api/sources" in routes


def test_api_health_endpoint():
    """API: /health endpoint."""
    try:
        from fastapi.testclient import TestClient
        from spectrum.api.app import create_app
    except ImportError:
        print("  ⚠️  FastAPI/TestClient not installed, skipping")
        return "skip"

    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert "stats" in data


def test_api_stats_endpoint():
    """API: /api/stats endpoint."""
    try:
        from fastapi.testclient import TestClient
        from spectrum.api.app import create_app
    except ImportError:
        print("  ⚠️  FastAPI/TestClient not installed, skipping")
        return "skip"

    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert "vector_chunks" in data


def test_api_sources_endpoint():
    """API: /api/sources endpoint."""
    try:
        from fastapi.testclient import TestClient
        from spectrum.api.app import create_app
    except ImportError:
        print("  ⚠️  FastAPI/TestClient not installed, skipping")
        return "skip"

    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/sources")

    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "sources" in data


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    skipped = 0
    for test_fn in tests:
        try:
            result = test_fn()
            if result == "skip":
                skipped += 1
            else:
                print(f"  ✅ {test_fn.__name__}")
                passed += 1
        except Exception as e:
            print(f"  ❌ {test_fn.__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        sys.exit(1)
