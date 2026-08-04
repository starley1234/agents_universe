"""Automated tests for NexusTwin MDM Web UI routes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_dashboard_page(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "NexusTwin MDM" in resp.text
    assert "Дашборд Холдинга" in resp.text


def test_objects_page(client: TestClient):
    resp = client.get("/ui/objects")
    assert resp.status_code == 200
    assert "Реестр Цифровых Двойников" in resp.text
    assert "ENG-500-MASTER" in resp.text


def test_object_detail_page(client: TestClient):
    # Retrieve object ID first
    resp_objs = client.get("/api/objects?query=ENG-500-MASTER")
    obj_id = resp_objs.json()[0]["id"]

    resp = client.get(f"/ui/objects/{obj_id}")
    assert resp.status_code == 200
    assert "ENG-500-MASTER" in resp.text
    assert "Атрибуты EAV" in resp.text
    assert "Спецификация EBOM" in resp.text
    assert "Цепочка Бейслайнов (SHA-256)" in resp.text


def test_agent_playground_page(client: TestClient):
    resp = client.get("/ui/agent")
    assert resp.status_code == 200
    assert "LangGraph Автономный Агент" in resp.text
    assert "Быстрые сценарии" in resp.text


def test_ontology_page(client: TestClient):
    resp = client.get("/ui/ontology")
    assert resp.status_code == 200
    assert "Справочники НСИ и Онтология" in resp.text
    assert "HOLDING" in resp.text


def test_mcp_docs_page(client: TestClient):
    resp = client.get("/ui/mcp")
    assert resp.status_code == 200
    assert "MCP Сервер (Model Context Protocol)" in resp.text
    assert "mdm.search_objects" in resp.text
    assert "mdm.verify_compliance" in resp.text
