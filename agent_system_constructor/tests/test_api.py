"""REST API: контракты, авторизация, обработка ошибок."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def make_client(tmp_path, token: str = "") -> TestClient:
    """Свежий модуль api на изолированной базе: он читает env при импорте."""
    import os

    os.environ["ACONSTRUCTOR_DB"] = str(tmp_path / "api.db")
    os.environ["ACONSTRUCTOR_API_TOKEN"] = token
    os.environ["ACONSTRUCTOR_PROVIDER"] = "fake"
    import aconstructor.api as api

    importlib.reload(api)
    return TestClient(api.app)


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def test_health_is_open_and_informative(client):
    d = client.get("/health").json()
    assert d["status"] == "ok"
    assert d["pipelines"] == 7
    assert d["provider"] == "fake"


def test_list_pipelines(client):
    d = client.get("/api/pipelines").json()
    assert len(d) == 7
    assert {p["slug"] for p in d} >= {"urban-scout", "patent-clearance"}
    assert all(p["title"] and p["agents"] for p in d)


def test_pipeline_detail_has_demo_and_graph(client):
    d = client.get("/api/pipelines/energy-hacker").json()
    assert "site" in d["demo_task"]
    assert "__start__" in d["graph_mermaid"]
    assert d["agents"] == ["prophet", "dispatcher"]


def test_unknown_pipeline_is_404(client):
    assert client.get("/api/pipelines/нетакого").status_code == 404
    assert client.post("/api/pipelines/нетакого/run", json={}).status_code == 404


def test_sync_run_returns_report(client):
    r = client.post("/api/pipelines/urban-scout/run", json={"sync": True})
    assert r.status_code == 202
    d = r.json()
    assert d["status"] == "done"
    assert d["report"].startswith("#")
    assert d["findings_n"] == 3


def test_async_run_is_queued_then_completes(client):
    d = client.post("/api/pipelines/urban-scout/run", json={}).json()
    assert d["status"] in ("queued", "running", "done")
    for _ in range(100):
        got = client.get(f"/api/runs/{d['id']}").json()
        if got["status"] in ("done", "failed"):
            break
        import time

        time.sleep(0.05)
    assert got["status"] == "done"
    assert got["report"]


def test_custom_task_is_used(client):
    task = {"parcels": [], "buildings": [], "hurdle_yield_pct": 12}
    d = client.post("/api/pipelines/urban-scout/run",
                    json={"task": task, "sync": True}).json()
    assert d["status"] == "done"
    assert d["findings_n"] == 0, "пустой список участков — пустой результат"


def test_run_history_and_filter(client):
    client.post("/api/pipelines/urban-scout/run", json={"sync": True})
    client.post("/api/pipelines/energy-hacker/run", json={"sync": True})
    assert len(client.get("/api/runs").json()["runs"]) == 2
    only = client.get("/api/runs?pipeline=energy-hacker").json()["runs"]
    assert len(only) == 1 and only[0]["pipeline"] == "energy-hacker"


def test_missing_run_is_404(client):
    assert client.get("/api/runs/нетакого").status_code == 404
    assert client.get("/api/runs/xx/report").status_code == 404


def test_report_endpoint_is_plain_text(client):
    d = client.post("/api/pipelines/urban-scout/run", json={"sync": True}).json()
    r = client.get(f"/api/runs/{d['id']}/report")
    assert r.status_code == 200
    assert r.text.startswith("# Urban-Scout")


def test_artifact_download(client):
    d = client.post("/api/pipelines/doc-restorer/run", json={"sync": True}).json()
    arts = client.get(f"/api/runs/{d['id']}").json()["artifacts"]
    names = {a["name"] for a in arts}
    assert "revit_script" in names
    r = client.get(f"/api/runs/{d['id']}/artifacts/revit_script")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    compile(r.text, "revit.py", "exec")


def test_cancel_running_run_conflicts(client):
    d = client.post("/api/pipelines/urban-scout/run", json={"sync": True}).json()
    r = client.post(f"/api/runs/{d['id']}/cancel")
    assert r.status_code == 409, "завершённый прогон отменить нельзя"


def test_stats_and_metrics(client):
    client.post("/api/pipelines/urban-scout/run", json={"sync": True})
    s = client.get("/api/stats").json()
    assert s["total"] >= 1 and s["success_rate"] == 1.0

    m = client.get("/metrics").text
    assert "aconstructor_runs_total" in m
    assert 'aconstructor_pipeline_runs{pipeline="urban-scout"}' in m


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Aconstructor" in r.text


# --- авторизация -----------------------------------------------------------
# Токен ходит в HTTP-заголовке, а тот обязан быть latin-1, поэтому
# в тестах и в проде токен ASCII (см. предупреждение в api.auth).
TOKEN = "s3cr3t-token"


def test_auth_blocks_without_token(tmp_path):
    with make_client(tmp_path, token=TOKEN) as c:
        assert c.get("/api/pipelines").status_code == 401
        assert c.get("/health").status_code == 200, "health открыт для оркестратора"


def test_auth_accepts_bearer(tmp_path):
    with make_client(tmp_path, token=TOKEN) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert c.get("/api/pipelines", headers=h).status_code == 200
        assert c.get("/api/pipelines",
                     headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_non_ascii_token_is_rejected_at_startup(tmp_path, capsys):
    """Кириллический токен нельзя передать в HTTP-заголовке — ловим на старте."""
    import os
    import importlib

    os.environ["ACONSTRUCTOR_DB"] = str(tmp_path / "x.db")
    os.environ["ACONSTRUCTOR_API_TOKEN"] = "секрет"
    import aconstructor.api as api

    with pytest.raises(SystemExit):
        importlib.reload(api)
    os.environ["ACONSTRUCTOR_API_TOKEN"] = ""


def test_auth_accepts_query_token_for_downloads(tmp_path):
    """Ссылка на скачивание артефакта открывается новой вкладкой без заголовков."""
    with make_client(tmp_path, token=TOKEN) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        d = c.post("/api/pipelines/doc-restorer/run", json={"sync": True}, headers=h).json()
        r = c.get(f"/api/runs/{d['id']}/artifacts/revit_script?token={TOKEN}")
        assert r.status_code == 200
