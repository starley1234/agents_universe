"""Tests for web UI routes — pages and API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from astra.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_dashboard_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "A.S.T.R.A." in resp.text
    assert "Dashboard" in resp.text


def test_projects_page(client):
    resp = client.get("/ui/projects")
    assert resp.status_code == 200
    assert "Проекты" in resp.text


def test_api_stats(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "projects" in data
    assert "sessions" in data


def test_api_mcp_servers(client):
    resp = client.get("/api/mcp/servers")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_api_data_projects(client):
    resp = client.get("/api/data/projects")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
