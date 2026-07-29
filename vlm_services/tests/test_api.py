"""REST API: контракты, загрузка файлов, ошибки, авторизация."""

from __future__ import annotations

import importlib
import io
import json
import os

import pytest
from fastapi.testclient import TestClient

from vlmkit.demo import png_bytes

TOKEN = "s3cr3t-token"  # ASCII: токен уходит в HTTP-заголовок


def make_client(token: str = "", **env: str) -> TestClient:
    os.environ["VLM_API_TOKEN"] = token
    os.environ["VLM_PROVIDER"] = "fake"
    os.environ["VLM_DB"] = ":memory:"  # журнал в памяти: тесты не пишут на диск
    for k, v in env.items():
        os.environ[k] = v
    import vlmkit.api as api

    importlib.reload(api)
    return TestClient(api.app)


@pytest.fixture()
def client():
    with make_client() as c:
        yield c


def test_health_is_open(client):
    d = client.get("/health").json()
    assert d["status"] == "ok" and d["services"] == 12


def test_list_services(client):
    d = client.get("/api/services").json()
    assert len(d) == 12
    assert all(s["title"] and s["slug"] for s in d)


def test_service_detail_exposes_schema(client):
    d = client.get("/api/services/nutrition-plate").json()
    assert "items" in d["schema"]
    assert d["max_images"] >= 1


def test_unknown_service_is_404(client):
    assert client.get("/api/services/нетакого").status_code == 404
    assert client.post("/api/services/нетакого/demo").status_code == 404


def test_demo_endpoint_runs(client):
    d = client.post("/api/services/retail-audit/demo").json()
    assert d["service"] == "retail-audit"
    assert d["report"].startswith("#")
    assert d["data"]["total_facings"] > 0


def test_json_run_with_data_uri(client):
    import base64

    uri = "data:image/png;base64," + base64.b64encode(png_bytes(40, 40)).decode()
    r = client.post("/api/services/ux-critic/run", json={"images": [uri], "params": {}})
    assert r.status_code == 200
    assert r.json()["images"][0]["width"] == 40


def test_json_run_passes_scene_for_offline(client):
    """Через JSON можно передать сцену — так работают демо и интеграционные тесты."""
    import base64

    uri = "data:image/png;base64," + base64.b64encode(png_bytes(40, 40)).decode()
    body = {"images": [{"data": uri, "name": "shelf.png",
                        "scene": {"facings": [{"brand": "A", "product": "x",
                                               "count": 4, "price_tag": True}],
                                  "empty_slots": 0}}],
            "params": {"our_brand": "A", "min_sos_pct": 50}}
    d = client.post("/api/services/retail-audit/run", json=body).json()
    assert d["data"]["total_facings"] == 4
    assert d["data"]["our_sos_pct"] == 100.0


def test_upload_multipart(client):
    files = [("files", ("a.png", png_bytes(64, 64), "image/png"))]
    r = client.post("/api/services/pim-cards/upload", files=files,
                    data={"params": json.dumps({"marketplace": "ozon"})})
    assert r.status_code == 200
    assert r.json()["data"]["marketplace"] == "ozon"


def test_upload_rejects_non_image(client):
    files = [("files", ("a.txt", "просто текст, не картинка".encode(), "text/plain"))]
    r = client.post("/api/services/pim-cards/upload", files=files)
    assert r.status_code == 400
    assert "формат" in r.json()["detail"]


def test_upload_rejects_bad_params_json(client):
    files = [("files", ("a.png", png_bytes(32, 32), "image/png"))]
    r = client.post("/api/services/pim-cards/upload", files=files,
                    data={"params": "{это не json}"})
    assert r.status_code == 400
    assert "JSON" in r.json()["detail"]


def test_too_many_images_is_400(client):
    files = [("files", (f"{i}.png", png_bytes(16, 16), "image/png")) for i in range(6)]
    r = client.post("/api/services/pim-cards/upload", files=files)  # max 3
    assert r.status_code == 400
    assert "слишком много" in r.json()["detail"]


def test_unknown_param_is_400_not_500(client):
    """Опечатка в параметре — ошибка клиента, а не падение сервиса."""
    r = client.post("/api/services/retail-audit/run",
                    json={"images": None, "params": {"нет_такого": 1}})
    assert r.status_code == 400


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "VLM Services" in r.text


# --- авторизация -----------------------------------------------------------
def test_auth_blocks_without_token():
    with make_client(TOKEN) as c:
        assert c.get("/api/services").status_code == 401
        assert c.get("/health").status_code == 200, "health открыт для оркестратора"


def test_auth_accepts_bearer():
    with make_client(TOKEN) as c:
        assert c.get("/api/services",
                     headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert c.get("/api/services",
                     headers={"Authorization": "Bearer wrong"}).status_code == 401


# --- эксплуатация ----------------------------------------------------------
def test_health_reports_cache_and_pillow(client):
    d = client.get("/health").json()
    assert "cache" in d and "pillow" in d


def test_request_id_is_returned(client):
    r = client.post("/api/services/retail-audit/demo")
    assert r.headers.get("X-Request-ID")


def test_request_id_is_preserved_from_client(client):
    """Свой идентификатор нужен, чтобы связать наш журнал с логами клиента."""
    r = client.post("/api/services/retail-audit/demo",
                    headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123"


def test_runs_journal_records_calls(client):
    client.post("/api/services/retail-audit/demo")
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) >= 1 and runs[0]["service"] == "retail-audit"


def test_stats_endpoint(client):
    client.post("/api/services/ux-critic/demo")
    s = client.get("/api/stats").json()
    assert s["total_runs"] >= 1
    assert "spent_24h_usd" in s and "cache" in s


def test_metrics_prometheus_format(client):
    client.post("/api/services/ux-critic/demo")
    m = client.get("/metrics").text
    assert "vlm_runs_total" in m
    assert "vlm_cost_usd_total" in m
    assert 'vlm_service_runs{service="ux-critic"}' in m


def test_cache_serves_repeat_request(client):
    """Повторная загрузка того же файла не должна стоить второго вызова."""
    import base64

    uri = "data:image/png;base64," + base64.b64encode(png_bytes(48, 48)).decode()
    body = {"images": [uri], "params": {}}
    first = client.post("/api/services/ux-critic/run", json=body).json()
    second = client.post("/api/services/ux-critic/run", json=body).json()
    assert first["cached"] is False and second["cached"] is True


def test_no_cache_flag_bypasses_cache(client):
    import base64

    uri = "data:image/png;base64," + base64.b64encode(png_bytes(48, 48)).decode()
    body = {"images": [uri], "params": {}}
    client.post("/api/services/ux-critic/run", json=body)
    again = client.post("/api/services/ux-critic/run",
                        json={**body, "no_cache": True}).json()
    assert again["cached"] is False


def test_budget_exhausted_returns_402():
    """402 отличает «кончились деньги» от «плохой запрос»."""
    with make_client(VLM_DAILY_BUDGET_USD="0.001") as c:
        import vlmkit.api as api

        api.store.log_run("x", "ok", cost_usd=1.0)
        r = c.post("/api/services/retail-audit/demo")
        assert r.status_code == 402
        assert "лимит" in r.json()["detail"]
    os.environ.pop("VLM_DAILY_BUDGET_USD", None)


def test_service_detail_lists_params(client):
    d = client.get("/api/services/retail-audit").json()
    assert set(d["params"]) == {"our_brand", "min_sos_pct", "planogram"}


def test_non_ascii_token_refused_at_startup():
    os.environ["VLM_API_TOKEN"] = "секрет"
    import vlmkit.api as api

    with pytest.raises(SystemExit):
        importlib.reload(api)
    os.environ["VLM_API_TOKEN"] = ""
