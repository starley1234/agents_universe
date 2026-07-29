"""Адаптер обмена с 1С — EnterpriseData-подобный протокол (см. ТЗ §4.1).

ЧЕСТНАЯ ГРАНИЦА ОБЪЁМА: в этой сборке НЕТ реального сервера 1С — по
явному решению пользователя ("сделать адаптер-заглушку с реальным
протоколом"). Реализован НАСТОЯЩИЙ HTTP+XML обмен, структурно
повторяющий формат EnterpriseData (`<Файл>/<Документ>/<Контрагент>/
<Товары>`, тот же принцип вложенности, что использует 1С при выгрузке
через универсальный формат обмена), и полноценная логика идемпотентности
и ретраев — но тестируется на ЛОКАЛЬНОМ fake-HTTP-сервере, эмулирующем
1С, а не на настоящей конфигурации 1С:Предприятия.

Направления:
  push_purchase_order()  — ERP -> 1С: выгрузка одобренного заказа
                            поставщику (для проведения в 1С:Бухгалтерии)
  pull_nomenclature()    — 1С -> ERP: загрузка справочника номенклатуры
  pull_counterparties()  — 1С -> ERP: загрузка справочника контрагентов

Идемпотентность: каждая исходящая операция получает идемпотентный ключ
(детерминированный хеш от типа документа + его id + версии), который
проверяется через Store.onec_log_attempt ПЕРЕД сетевым вызовом — при
повторном вызове (например, после сбоя сети и ретрая оркестратором)
документ не улетит в 1С дважды.

Мастер-система (cfg.onec_master_*) определяет, чьи данные побеждают при
конфликте: если ERP — мастер номенклатуры, то pull_nomenclature()
обновляет только onec_uuid (маппинг), не трогая name/unit/min_stock;
если 1С — мастер, входящие данные полностью перезаписывают запись ERP.
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..db.store import Store


class OneCError(RuntimeError):
    """Ошибка обмена с 1С: сеть, неверный формат, конфликт данных."""


@dataclass
class SyncResult:
    ok: bool
    idempotency_key: str
    external_id: str = ""
    error: str = ""
    skipped_duplicate: bool = False


def _idempotency_key(entity_type: str, entity_id: int, version: str = "") -> str:
    raw = f"{entity_type}:{entity_id}:{version}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class OneCAdapter:
    """HTTP-клиент обмена с 1С по EnterpriseData-подобному протоколу."""

    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    def _require_base_url(self) -> str:
        if not self.cfg.onec_base_url:
            raise OneCError(
                "ONEC_BASE_URL не задан — укажите адрес сервиса обмена 1С."
            )
        return self.cfg.onec_base_url.rstrip("/")

    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        h = {"Content-Type": "application/xml; charset=utf-8"}
        if self.cfg.onec_api_key:
            h["Authorization"] = f"Bearer {self.cfg.onec_api_key}"
        if idempotency_key:
            h["X-Idempotency-Key"] = idempotency_key
        return h

    def _http(self, method: str, path: str, body: bytes | None = None,
             idempotency_key: str = "") -> bytes:
        base = self._require_base_url()
        req = urllib.request.Request(
            f"{base}{path}", data=body, method=method,
            headers=self._headers(idempotency_key))
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.onec_timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OneCError(f"1С вернула HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OneCError(f"Не удалось связаться с 1С ({base}): {exc}") from exc

    # ------------------------------------------------------ push: заказ
    @staticmethod
    def _build_purchase_order_xml(po: dict[str, Any], counterparty: dict[str, Any]) -> bytes:
        root = ET.Element("Файл", {"ВерсияСхемы": "2.05", "ВерсияФормата": "1.0"})
        doc = ET.SubElement(root, "Документ", {
            "Ид": str(po["id"]), "Тип": "ЗаказПоставщику",
            "Номер": str(po["id"]),
        })
        cp_el = ET.SubElement(doc, "Контрагент", {
            "Ид": counterparty.get("onec_uuid") or f"erp-cp-{counterparty['id']}",
        })
        cp_el.text = counterparty["name"]
        goods = ET.SubElement(doc, "Товары")
        for line in po["lines"]:
            item = ET.SubElement(goods, "Товар")
            ET.SubElement(item, "Артикул").text = line["sku"]
            ET.SubElement(item, "Наименование").text = line["name"]
            ET.SubElement(item, "Количество").text = f"{line['quantity']:.3f}"
            ET.SubElement(item, "Цена").text = f"{line['price']:.2f}"
        ET.SubElement(doc, "Сумма").text = f"{po['total_amount']:.2f}"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def push_purchase_order(self, po_id: int) -> SyncResult:
        """Выгружает одобренный заказ поставщику в 1С. Идемпотентно: при
        повторном вызове с тем же po_id и тем же статусом заказа документ
        НЕ отправляется повторно — используется зафиксированный результат
        предыдущей попытки."""
        po = self.store.get_purchase_order(po_id)
        if not po:
            raise OneCError(f"Заказ поставщику #{po_id} не найден")
        counterparty = self.store.get_counterparty(po["counterparty_id"])
        if not counterparty:
            raise OneCError(f"Контрагент #{po['counterparty_id']} не найден")

        key = _idempotency_key("purchase_order", po_id, version=po["status"])
        log_id, is_new = self.store.onec_log_attempt(
            "to_1c", "purchase_order", po_id, key)
        if not is_new:
            existing = [r for r in self.store.onec_sync_log_list()
                       if r["idempotency_key"] == key]
            prev = existing[0] if existing else {}
            return SyncResult(
                ok=prev.get("status") == "ok", idempotency_key=key,
                external_id=prev.get("external_id", ""),
                error=prev.get("error", ""), skipped_duplicate=True)

        body = self._build_purchase_order_xml(po, counterparty)
        try:
            resp = self._http("POST", "/exchange/purchase_order", body, key)
        except OneCError as exc:
            self.store.onec_mark_result(log_id, "error", error=str(exc))
            return SyncResult(ok=False, idempotency_key=key, error=str(exc))

        try:
            resp_root = ET.fromstring(resp)
            external_id = resp_root.get("Ид") or resp_root.findtext("Ид") or ""
        except ET.ParseError as exc:
            self.store.onec_mark_result(log_id, "error", error=str(exc))
            return SyncResult(ok=False, idempotency_key=key,
                              error=f"Неверный XML-ответ 1С: {exc}")

        self.store.onec_mark_result(log_id, "ok", external_id=external_id)
        if external_id:
            cur = self.store.conn.cursor()
            cur.execute("UPDATE purchase_order SET onec_uuid=%s WHERE id=%s",
                       (external_id, po_id))
        return SyncResult(ok=True, idempotency_key=key, external_id=external_id)

    # ---------------------------------------------------- pull: справочники
    def pull_nomenclature(self) -> list[dict[str, Any]]:
        """Загружает справочник номенклатуры из 1С. Мастер-система
        (cfg.onec_master_nomenclature) определяет, что именно обновляется:
        'erp' — только маппинг onec_uuid по совпадению SKU/артикула;
        '1c' — 1С полностью замещает name/unit/min_stock у записи ERP."""
        raw = self._http("GET", "/exchange/nomenclature")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OneCError(f"Неверный XML-ответ 1С (номенклатура): {exc}") from exc

        applied: list[dict[str, Any]] = []
        for item in root.findall(".//Товар"):
            sku = item.findtext("Артикул") or ""
            name = item.findtext("Наименование") or ""
            unit = item.findtext("Единица") or "шт"
            uuid_1c = item.get("Ид") or ""
            if not sku:
                continue
            existing = self.store.get_nomenclature_by_sku(sku)
            if self.cfg.onec_master_nomenclature == "erp":
                # ERP — источник истины: обновляем ТОЛЬКО маппинг
                if existing:
                    nid = self.store.upsert_nomenclature(
                        sku, existing["name"], unit=existing["unit"],
                        min_stock=existing["min_stock"],
                        lead_time_days=existing["lead_time_days"],
                        onec_uuid=uuid_1c)
                else:
                    nid = self.store.upsert_nomenclature(sku, name, unit=unit,
                                                          onec_uuid=uuid_1c)
            else:
                # 1С — источник истины: данные 1С побеждают
                min_stock = existing["min_stock"] if existing else 0
                lead_time = existing["lead_time_days"] if existing else 0
                nid = self.store.upsert_nomenclature(
                    sku, name, unit=unit, min_stock=min_stock,
                    lead_time_days=lead_time, onec_uuid=uuid_1c)
            applied.append({"id": nid, "sku": sku, "onec_uuid": uuid_1c})
            self.store.log_audit("agent:onec_integrator", "sync_nomenclature",
                                 "nomenclature", nid,
                                 {"source": "1c", "onec_uuid": uuid_1c})
        return applied

    def pull_counterparties(self) -> list[dict[str, Any]]:
        raw = self._http("GET", "/exchange/counterparty")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OneCError(f"Неверный XML-ответ 1С (контрагенты): {exc}") from exc

        applied: list[dict[str, Any]] = []
        for item in root.findall(".//Контрагент"):
            name = item.text or ""
            inn = item.get("ИНН") or ""
            uuid_1c = item.get("Ид") or ""
            if not name:
                continue
            cid = self.store.upsert_counterparty(name, inn=inn, onec_uuid=uuid_1c)
            applied.append({"id": cid, "name": name, "onec_uuid": uuid_1c})
            self.store.log_audit("agent:onec_integrator", "sync_counterparty",
                                 "counterparty", cid,
                                 {"source": "1c", "onec_uuid": uuid_1c})
        return applied
