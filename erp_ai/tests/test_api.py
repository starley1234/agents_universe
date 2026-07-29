"""Тесты app.api.server (FastAPI): реальный HTTP-сервер (uvicorn в
отдельном потоке), реальный embedded PostgreSQL, реальный HTTP-клиент
(httpx) — без TestClient-моков, максимально приближено к тому, как
систему будет дёргать настоящий клиент.
"""
from __future__ import annotations

import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="erp_api_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"

try:
    import httpx  # type: ignore
    import uvicorn  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = SKIP_REASON or "httpx/uvicorn не установлены"


def _fresh_dsn() -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(_srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", _srv.get_uri())


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ApiServer:
    """Реальный uvicorn-сервер FastAPI-приложения в отдельном потоке."""

    def __init__(self, dsn: str, token: str | None = None):
        from app.api.server import app, configure
        from app.config import Config

        self.cfg = Config(db_dsn=dsn, procurement_max_auto_amount=10_000)
        configure(self.cfg, token)
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self._wait_ready()

    def _wait_ready(self, attempts: int = 40) -> None:
        for _ in range(attempts):
            try:
                r = httpx.get(f"{self.base_url}/health", timeout=1)
                if r.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("сервер не поднялся за отведённое время")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_api: тесты пропущены — {SKIP_REASON}")
        return 0

    section("Сервер поднимается и отвечает на /health без токена")
    srv = _ApiServer(_fresh_dsn())
    try:
        r = httpx.get(f"{srv.base_url}/health", timeout=5)
        check("health -> 200", r.status_code == 200 and r.json()["status"] == "ok")

        section("Nomenclature: CRUD через реальный HTTP")
        r = httpx.post(f"{srv.base_url}/v1/nomenclature",
                       json={"sku": "MTL-001", "name": "Сталь", "unit": "лист",
                            "min_stock": 100}, timeout=5)
        check("создание номенклатуры -> 200", r.status_code == 200)
        nid = r.json()["id"]

        r = httpx.get(f"{srv.base_url}/v1/nomenclature", timeout=5)
        check("список номенклатуры видит созданную позицию",
             any(n["id"] == nid for n in r.json()["items"]))

        section("Counterparty + price + stock")
        r = httpx.post(f"{srv.base_url}/v1/counterparties",
                       json={"name": "Поставщик 1", "reliability_score": 0.9}, timeout=5)
        cid = r.json()["id"]
        r = httpx.post(f"{srv.base_url}/v1/counterparties/{cid}/prices",
                       json={"nomenclature_id": nid, "price": 100.0}, timeout=5)
        check("установка цены -> 200", r.status_code == 200)
        r = httpx.post(f"{srv.base_url}/v1/counterparties/999999/prices",
                       json={"nomenclature_id": nid, "price": 1.0}, timeout=5)
        check("установка цены для несуществующего контрагента -> 404",
             r.status_code == 404)

        r = httpx.post(f"{srv.base_url}/v1/nomenclature/stock",
                       json={"nomenclature_id": nid, "quantity": 10}, timeout=5)
        check("установка остатка -> 200", r.status_code == 200)

        section("low-stock отражает реальный дефицит")
        r = httpx.get(f"{srv.base_url}/v1/nomenclature/low-stock", timeout=5)
        check("низкий остаток виден", any(n["id"] == nid for n in r.json()["items"]))

        section("Агент-снабженец: scan и propose через HTTP")
        r = httpx.post(f"{srv.base_url}/v1/agents/procurement/scan",
                       json={"autonomy_mode": "suggest"}, timeout=5)
        check("scan -> 200", r.status_code == 200)
        decisions = r.json()["decisions"]
        check("создано хотя бы одно предложение", len(decisions) >= 1)
        proposal_id = decisions[0]["proposal_id"]

        r = httpx.get(f"{srv.base_url}/v1/proposals", timeout=5)
        check("список предложений видит созданное",
             any(p["id"] == proposal_id for p in r.json()["items"]))

        r = httpx.get(f"{srv.base_url}/v1/proposals/{proposal_id}", timeout=5)
        check("детали предложения содержат audit_trail", "audit_trail" in r.json())
        check("explanation присутствует и непустой",
             len(r.json()["explanation"]) > 0)

        r = httpx.get(f"{srv.base_url}/v1/proposals/999999", timeout=5)
        check("несуществующее предложение -> 404", r.status_code == 404)

        section("Confirmation gate через HTTP: approve")
        r = httpx.post(f"{srv.base_url}/v1/proposals/{proposal_id}/approve",
                       json={"actor": "human:api-test"}, timeout=5)
        check("approve -> 200", r.status_code == 200)
        po_id = r.json()["purchase_order_id"]

        r = httpx.get(f"{srv.base_url}/v1/purchase-orders/{po_id}", timeout=5)
        check("заказ создан и виден", r.status_code == 200)
        check("создатель — человек, принявший решение",
             r.json()["created_by"] == "human:api-test")

        r = httpx.post(f"{srv.base_url}/v1/proposals/{proposal_id}/approve",
                       json={"actor": "human:api-test"}, timeout=5)
        check("повторный approve -> 400 (доменная ошибка, не 500)",
             r.status_code == 400)

        section("Reject через HTTP")
        r = httpx.post(f"{srv.base_url}/v1/agents/procurement/propose/{nid}",
                       json={"autonomy_mode": "suggest"}, timeout=5)
        check("propose для конкретной позиции -> 200", r.status_code == 200)
        proposal_id2 = r.json()["proposal_id"]
        r = httpx.post(f"{srv.base_url}/v1/proposals/{proposal_id2}/reject",
                       json={"actor": "human:api-test", "reason": "тест"}, timeout=5)
        check("reject -> 200", r.status_code == 200)

        section("Аудит через HTTP")
        r = httpx.get(f"{srv.base_url}/v1/audit", timeout=5)
        check("аудит содержит записи", len(r.json()["items"]) > 0)
        r = httpx.get(f"{srv.base_url}/v1/audit/purchase_order/{po_id}", timeout=5)
        check("аудит по конкретному документу", len(r.json()["items"]) > 0)

        section("Дашборд")
        r = httpx.get(f"{srv.base_url}/dashboard", timeout=5)
        check("дашборд отдаётся -> 200", r.status_code == 200)
        check("это HTML ERP AI", "<title>ERP AI" in r.text)
        r = httpx.get(f"{srv.base_url}/v1/dashboard/stats", timeout=5)
        check("статистика дашборда -> 200", r.status_code == 200)
        check("статистика отражает реальные данные",
             r.json()["nomenclature"] >= 1 and r.json()["purchase_orders"] >= 1)

        section("Неизвестный маршрут -> 404")
        r = httpx.get(f"{srv.base_url}/v1/nonexistent", timeout=5)
        check("неизвестный GET -> 404", r.status_code == 404)
    finally:
        srv.stop()

    section("Токен: без Authorization -> 401, с верным -> 200")
    srv2 = _ApiServer(_fresh_dsn(), token="secret-token")
    try:
        r = httpx.get(f"{srv2.base_url}/v1/nomenclature", timeout=5)
        check("без токена -> 401", r.status_code == 401)
        r = httpx.get(f"{srv2.base_url}/v1/nomenclature",
                      headers={"Authorization": "Bearer secret-token"}, timeout=5)
        check("с верным токеном -> 200", r.status_code == 200)
        r = httpx.get(f"{srv2.base_url}/health", timeout=5)
        check("/health не требует токена", r.status_code == 200)
        r = httpx.get(f"{srv2.base_url}/dashboard", timeout=5)
        check("/dashboard не требует токена", r.status_code == 200)
    finally:
        srv2.stop()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
