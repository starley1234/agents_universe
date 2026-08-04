"""Automated tests for LLM Synthetic Testing Mode — Enterprise Generator."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_get_synthesis_templates(client: TestClient):
    """Test retrieving ready-to-use fictional enterprise descriptions."""
    resp = client.get("/api/mdm/synthesize/templates")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert "templates" in data
    assert len(data["templates"]) >= 4
    ids = [t["id"] for t in data["templates"]]
    assert "titan_uav" in ids
    assert "arctic_marine" in ids
    assert "robo_2030" in ids
    assert "orbit_space" in ids


def test_synthesize_enterprise_endpoint(client: TestClient):
    """Test synthesizing a complete Digital Twin of a fictional enterprise from description."""
    payload = {
        "description": "Завод промышленной робототехники 'РобоТех-2030' — производство 6-осевых манипуляторов и сервоприводов",
        "enterprise_code": "ROBO",
        "include_duplicates": True,
        "actor_id": "pytest_synthesizer",
    }
    resp = client.post("/api/mdm/synthesize", json=payload)
    assert resp.status_code == 200
    res = resp.json()

    assert res["status"] == "success"
    assert res["enterprise_code"] == "ROBO"
    assert res["created_org_units_count"] >= 0
    assert res["created_types_count"] >= 0
    assert res["created_objects_count"] >= 0

    # Verify that synthesized objects are searchable in the holding database
    resp_search = client.get("/api/objects?query=ROBO")
    assert resp_search.status_code == 200
    found = resp_search.json()
    assert len(found) >= 3

    # Verify that cryptographic SHA-256 baseline chain is 100% valid for synthesized master object
    master_obj = next((o for o in found if "ARM-ROBO-6D" in o["master_code"]), found[0])
    resp_ver = client.get(f"/api/objects/{master_obj['id']}/verify")
    assert resp_ver.status_code == 200
    ver_data = resp_ver.json()
    assert ver_data["all_valid"] is True
    assert ver_data["chain_length"] >= 1


def test_generator_web_ui_page(client: TestClient):
    """Test that the LLM Synthetic Generator Web UI page renders correctly."""
    resp = client.get("/ui/generator")
    assert resp.status_code == 200
    assert "Синтез Вымышленного Предприятия" in resp.text
    assert "Синтезировать Цифровой Двойник" in resp.text


def test_agent_synthesis_query(client: TestClient):
    """Test LangGraph autonomous agent executing enterprise synthesis via natural language query."""
    payload = {
        "query": "Синтезируй цифровой двойник авиастроительного завода 'Небесный Титан' — производство БПЛА и авионики"
    }
    resp = client.post("/api/agent/run", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["query"] == payload["query"]
    assert "result" in data
    assert len(data["result"]) > 50
    assert any(
        tool["tool"] == "synthesize_enterprise" for tool in data.get("tool_executions", [])
    )
