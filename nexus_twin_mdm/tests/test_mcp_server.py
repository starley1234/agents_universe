"""Automated tests for Model Context Protocol (MCP) server endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_mcp_list_tools_endpoint(client: TestClient):
    resp = client.get("/api/mcp/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert data["server"] == "NexusTwin MDM MCP Server"
    assert data["tools_count"] == 7
    names = [t["name"] for t in data["tools"]]
    assert "mdm.search_objects" in names
    assert "mdm.get_bom_graph" in names
    assert "mdm.verify_compliance" in names
    assert "mdm.upsert_property" in names
    assert "mdm.get_org_hierarchy" in names
    assert "mdm.synthesize_enterprise" in names
    assert "mdm.agent_query" in names


def test_mcp_jsonrpc_tools_list(client: TestClient):
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 101,
    }
    resp = client.post("/api/mcp/rpc", json=payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["jsonrpc"] == "2.0"
    assert res_data["id"] == 101
    assert "tools" in res_data["result"]
    assert len(res_data["result"]["tools"]) == 7


def test_mcp_jsonrpc_tools_call_verify_compliance(client: TestClient):
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "mdm.verify_compliance",
            "arguments": {"object_id": "ENG-500-MASTER"},
        },
        "id": 202,
    }
    resp = client.post("/api/mcp/rpc", json=payload)
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["id"] == 202
    assert "content" in res_data["result"]
    text_item = res_data["result"]["content"][0]["text"]
    assert "all_valid" in text_item
