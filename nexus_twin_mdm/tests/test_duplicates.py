"""Automated tests for MDM Deduplication & Merging Engine."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_detect_duplicates(client: TestClient):
    """Test algorithmic duplicate detection across Holding MDM objects."""
    resp = client.get("/api/mdm/duplicates/detect?threshold=0.70")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert "clusters" in data
    assert len(data["clusters"]) >= 1
    cluster = data["clusters"][0]
    assert "primary_candidate" in cluster
    assert "duplicates" in cluster
    assert len(cluster["duplicates"]) >= 1
    assert cluster["similarity"] >= 0.70
    assert len(cluster["reason"]) > 0
    assert len(cluster["primary_candidate"]["master_code"]) > 0


def test_merge_duplicates_and_baseline_chain_integrity(client: TestClient):
    """Test merging duplicate into primary object and verifying cryptographic SHA-256 chain."""
    # 1. Detect duplicates to get primary and duplicate IDs
    resp_det = client.get("/api/mdm/duplicates/detect?threshold=0.70")
    assert resp_det.status_code == 200
    clusters = resp_det.json()["clusters"]
    assert len(clusters) >= 1

    cl = clusters[0]
    primary_id = cl["primary_candidate"]["id"]
    duplicate_id = cl["duplicates"][0]["id"]

    # 2. Execute Merge
    merge_payload = {
        "primary_id": primary_id,
        "duplicate_id": duplicate_id,
        "strategy": "trust_based",
        "actor_id": "pytest_admin",
    }
    resp_merge = client.post("/api/mdm/duplicates/merge", json=merge_payload)
    assert resp_merge.status_code == 200
    res = resp_merge.json()

    assert res["status"] == "merged"
    assert res["primary_id"] == primary_id
    assert res["duplicate_id"] == duplicate_id
    assert "new_baseline" in res
    assert res["new_baseline"]["seq"] >= 1

    # 3. Verify duplicate is now marked 'merged'
    resp_dup = client.get(f"/api/objects/{duplicate_id}")
    assert resp_dup.status_code == 200
    dup_data = resp_dup.json()
    assert dup_data["state"] == "merged"

    # 4. Verify primary object's baseline hash chain is 100% valid
    resp_ver = client.get(f"/api/objects/{primary_id}/verify")
    assert resp_ver.status_code == 200
    ver_data = resp_ver.json()
    assert ver_data["all_valid"] is True
    assert ver_data["chain_length"] >= 1


def test_duplicates_ui_page(client: TestClient):
    """Test duplicates Web UI page rendering."""
    resp = client.get("/ui/duplicates")
    assert resp.status_code == 200
    assert "Управление Дубликатами" in resp.text
    assert "Сканировать дубликаты" in resp.text


def test_agent_duplicate_audit_task(client: TestClient):
    """Test LangGraph autonomous agent executing duplicate detection and quality audit."""
    payload = {
        "query": "Проверить базу Холдинга на наличие дубликатов и выполнить аудит качества данных НСИ"
    }
    resp = client.post("/api/agent/run", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["query"] == payload["query"]
    assert "result" in data
    assert "дубликат" in data["result"].lower() or "отсутствуют" in data["result"].lower()
    assert "data_quality_score" in data
    assert data["data_quality_score"] >= 50
    assert data["compliance_status"] == "PASSED_100_PERCENT"
