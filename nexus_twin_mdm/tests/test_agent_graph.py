"""Automated tests for LangGraph Autonomous Agent in NexusTwin MDM."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_agent_run_synchronous(client: TestClient):
    payload = {
        "query": "Проверь целостность цепочки бейслайнов и статус сертификации АП-25 Холдинга"
    }
    resp = client.post("/api/agent/run", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["query"] == payload["query"]
    assert "result" in data
    assert len(data["result"]) > 50
    assert data["compliance_status"] == "PASSED_100_PERCENT"
    assert "iterations" in data
    assert isinstance(data["tool_executions"], list)


def test_agent_stream_endpoint(client: TestClient):
    payload = {
        "query": "Получи спецификацию EBOM для двигателя ENG-500-MASTER"
    }
    with client.stream("POST", "/api/agent/stream", json=payload) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        lines = []
        for line in resp.iter_lines():
            if line:
                lines.append(line)
        assert len(lines) > 0
        assert any("event: done" in l for l in lines)
