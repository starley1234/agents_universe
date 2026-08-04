"""Automated tests for Holding MDM, Certification, and Digital Twin CRUD."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_check(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "NexusTwin MDM"}

    resp_full = client.get("/api/health/full")
    assert resp_full.status_code == 200
    data = resp_full.json()
    assert data["status"] == "ok"
    assert "project_name" in data
    assert data["database"]["status"] == "ok"


def test_list_nsi_dictionaries(client: TestClient):
    # org-units
    resp_org = client.get("/api/org-units")
    assert resp_org.status_code == 200
    orgs = resp_org.json()
    assert any(o["id"] == "HOLDING" for o in orgs)

    # types
    resp_types = client.get("/api/types")
    assert resp_types.status_code == 200
    types = resp_types.json()
    assert any(t["id"] == "engine" for t in types)

    # sources
    resp_sources = client.get("/api/sources")
    assert resp_sources.status_code == 200
    sources = resp_sources.json()
    assert any(s["id"] == "plm" and s["trust"] == 90 for s in sources)

    # uom
    resp_uom = client.get("/api/uom")
    assert resp_uom.status_code == 200
    uoms = resp_uom.json()
    assert any(u["code"] == "796" for u in uoms)


def test_object_crud_and_trust_upsert(client: TestClient):
    # 1. Create a new MDM Object
    import uuid
    mc = f"TEST-PART-{uuid.uuid4().hex[:6]}"
    payload = {
        "type_id": "part",
        "org_id": "HOLDING",
        "master_code": mc,
        "name": "Тестовая деталь",
        "description": "Сборочный компонент для unit-тестов",
        "attributes": {"weight": 10.5},
        "source_id": "manual",
    }
    resp_create = client.post("/api/objects", json=payload)
    assert resp_create.status_code == 201
    obj = resp_create.json()
    assert obj["master_code"] == mc
    assert obj["display_name"] == "Тестовая деталь"
    obj_id = obj["id"]

    # 2. Get object detail
    resp_get = client.get(f"/api/objects/{obj_id}")
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == obj_id

    # 3. Upsert a property from high-trust source (plm trust=90) -> OK
    prop_plm = {
        "key": "attributes",
        "value": {"name": "Тестовая деталь PLM", "weight": 11.0},
        "source_id": "plm",
        "actor_id": "admin",
    }
    resp_prop1 = client.post(f"/api/objects/{obj_id}/properties", json=prop_plm)
    assert resp_prop1.status_code == 200
    assert resp_prop1.json()["status"] == "ok"
    assert resp_prop1.json()["object"]["display_name"] == "Тестовая деталь PLM"

    # 4. Try upserting from lower-trust source (ai trust=30 < 90) -> Should be rejected with 403!
    prop_ai = {
        "key": "attributes",
        "value": {"name": "Тестовая деталь AI", "weight": 9.0},
        "source_id": "ai",
        "actor_id": "admin",
    }
    resp_prop2 = client.post(f"/api/objects/{obj_id}/properties", json=prop_ai)
    assert resp_prop2.status_code == 403
    assert "trust score is lower" in resp_prop2.json()["detail"].lower()


def test_ebom_hierarchy(client: TestClient):
    # Get seeded ENG-500-MASTER demo object
    resp_objs = client.get("/api/objects?query=ENG-500-MASTER")
    assert resp_objs.status_code == 200
    objs = resp_objs.json()
    assert len(objs) >= 1
    master_id = objs[0]["id"]

    resp_bom = client.get(f"/api/objects/{master_id}/bom")
    assert resp_bom.status_code == 200
    data = resp_bom.json()
    assert data["master_code"] == "ENG-500-MASTER"
    assert len(data["bom_items"]) >= 1
    assert any(b["child_master_code"] == "TURBO-COMP-01" for b in data["bom_items"])


def test_baseline_hash_chain_verification(client: TestClient):
    resp_objs = client.get("/api/objects?query=ENG-500-MASTER")
    master_id = resp_objs.json()[0]["id"]

    # Check verification status
    resp_ver = client.get(f"/api/objects/{master_id}/verify")
    assert resp_ver.status_code == 200
    ver_data = resp_ver.json()
    assert ver_data["object_id"] == master_id
    assert ver_data["all_valid"] is True
    assert len(ver_data["chain"]) >= 1
    assert ver_data["chain"][0]["status"] == "OK"
