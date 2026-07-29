"""Хранилище ERP AI: PostgreSQL.

ОБЯЗАТЕЛЬНОЕ хранилище (см. app/config.py: без DB_DSN приложение не
стартует). pgvector НЕ используется в этой сборке — ядро ERP работает
с обычными реляционными данными; агент-снабженец не занимается
семантическим поиском, поэтому лишняя зависимость не нужна.

Схема (в объёме реализованного контура "снабжение", см. README.md):
  nomenclature       — справочник номенклатуры (материалы/товары)
  counterparty       — справочник контрагентов (поставщики/покупатели)
  stock_balance      — остаток по номенклатуре (для расчёта дефицита)
  purchase_request   — заявка на закупку (создаётся агентом или вручную)
  purchase_order      — заказ поставщику (может быть создан из заявки)
  purchase_order_line — строка заказа: номенклатура/количество/цена

Агентский слой (сквозной для ВСЕХ будущих доменных агентов, не только
снабженца — намеренно вынесен в общие таблицы, а не дублируется на
каждый контур):
  agent_proposal     — предложение агента: что предлагается сделать,
                       обоснование (explainability), режим автономности,
                       статус (pending/approved/rejected/auto_executed/
                       rolled_back)
  audit_log          — НЕИЗМЕНЯЕМЫЙ журнал: кто (агент/человек) что
                       сделал, когда, с каким результатом. Только
                       INSERT, ни одного UPDATE/DELETE в коде Store —
                       это и есть гарантия неизменности на уровне
                       прикладного кода (полная защита потребовала бы
                       отдельного пользователя БД без прав UPDATE/DELETE
                       на таблицу — см. README.md, раздел "Что не
                       реализовано").
  onec_sync_log      — журнал обмена с 1С: направление, документ,
                       статус, ошибка (для идемпотентности и ретраев)

Импорт psycopg — ленивый, как в остальных проектах этого репозитория.
"""
from __future__ import annotations

import json
import time
from typing import Any


class StoreError(RuntimeError):
    """Ошибка работы с хранилищем: подключение и т.п."""


def _require_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise StoreError(
            "ERP AI требует psycopg. Установите: pip install \"psycopg[binary]\""
        ) from exc
    return psycopg


class Store:
    """Единая точка доступа к PostgreSQL для ERP AI."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise StoreError(
                "Не задан DB_DSN. ERP AI не работает без PostgreSQL — "
                "укажите переменную окружения DB_DSN."
            )
        psycopg = _require_psycopg()
        self._psycopg = psycopg
        self.dsn = dsn
        try:
            self.conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:
            raise StoreError(f"Не удалось подключиться к PostgreSQL: {exc}") from exc
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _now() -> float:
        return time.time()

    # ------------------------------------------------------------ schema
    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nomenclature(
                id SERIAL PRIMARY KEY,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                unit TEXT DEFAULT 'шт',
                min_stock NUMERIC(18,3) DEFAULT 0,   -- страховой запас
                lead_time_days INTEGER DEFAULT 0,     -- срок поставки
                onec_uuid TEXT DEFAULT '',            -- ключ маппинга с 1С
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS counterparty(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                inn TEXT DEFAULT '',
                kind TEXT DEFAULT 'supplier',     -- supplier | customer | both
                reliability_score REAL DEFAULT 1.0,  -- 0..1, для агента
                onec_uuid TEXT DEFAULT '',
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION,
                UNIQUE(name, inn)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supplier_price(
                id SERIAL PRIMARY KEY,
                nomenclature_id INTEGER NOT NULL REFERENCES nomenclature(id)
                    ON DELETE CASCADE,
                counterparty_id INTEGER NOT NULL REFERENCES counterparty(id)
                    ON DELETE CASCADE,
                price NUMERIC(18,2) NOT NULL,
                currency TEXT DEFAULT 'RUB',
                updated DOUBLE PRECISION,
                UNIQUE(nomenclature_id, counterparty_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_balance(
                id SERIAL PRIMARY KEY,
                nomenclature_id INTEGER NOT NULL REFERENCES nomenclature(id)
                    ON DELETE CASCADE,
                warehouse TEXT DEFAULT 'основной',
                quantity NUMERIC(18,3) DEFAULT 0,
                reserved NUMERIC(18,3) DEFAULT 0,   -- зарезервировано под заказы
                updated DOUBLE PRECISION,
                UNIQUE(nomenclature_id, warehouse)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchase_request(
                id SERIAL PRIMARY KEY,
                nomenclature_id INTEGER NOT NULL REFERENCES nomenclature(id),
                quantity NUMERIC(18,3) NOT NULL,
                reason TEXT DEFAULT '',            -- почему нужна закупка
                status TEXT DEFAULT 'open',        -- open|ordered|cancelled
                created_by TEXT DEFAULT 'human',   -- 'human' | 'agent:<slug>'
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchase_order(
                id SERIAL PRIMARY KEY,
                counterparty_id INTEGER NOT NULL REFERENCES counterparty(id),
                status TEXT DEFAULT 'draft',
                -- draft|pending_approval|approved|sent|received|cancelled
                total_amount NUMERIC(18,2) DEFAULT 0,
                created_by TEXT DEFAULT 'human',
                approved_by TEXT DEFAULT '',
                onec_uuid TEXT DEFAULT '',
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchase_order_line(
                id SERIAL PRIMARY KEY,
                purchase_order_id INTEGER NOT NULL REFERENCES purchase_order(id)
                    ON DELETE CASCADE,
                nomenclature_id INTEGER NOT NULL REFERENCES nomenclature(id),
                quantity NUMERIC(18,3) NOT NULL,
                price NUMERIC(18,2) NOT NULL,
                purchase_request_id INTEGER REFERENCES purchase_request(id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_proposal(
                id SERIAL PRIMARY KEY,
                agent_slug TEXT NOT NULL,          -- 'procurement' и т.п.
                kind TEXT NOT NULL,                -- 'create_purchase_order' и т.п.
                payload JSONB NOT NULL,            -- предлагаемые данные документа
                explanation TEXT NOT NULL,         -- обоснование (explainability)
                sources JSONB DEFAULT '[]',        -- на основании чего решил
                autonomy_mode TEXT NOT NULL,
                -- suggest | draft | auto_with_review | full_auto
                status TEXT DEFAULT 'pending',
                -- pending|approved|rejected|auto_executed|rolled_back
                result_document_type TEXT DEFAULT '',
                result_document_id INTEGER,
                decided_by TEXT DEFAULT '',
                created DOUBLE PRECISION,
                decided DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_proposal_status "
            "ON agent_proposal(status)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log(
                id SERIAL PRIMARY KEY,
                actor TEXT NOT NULL,               -- 'human:<user>' | 'agent:<slug>'
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                details JSONB DEFAULT '{}',
                created DOUBLE PRECISION NOT NULL
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_audit_log_entity "
            "ON audit_log(entity_type, entity_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS onec_sync_log(
                id SERIAL PRIMARY KEY,
                direction TEXT NOT NULL,           -- 'to_1c' | 'from_1c'
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                external_id TEXT DEFAULT '',
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT DEFAULT 'pending',     -- pending|ok|error
                error TEXT DEFAULT '',
                attempts INTEGER DEFAULT 0,
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)

    # ------------------------------------------------------- nomenclature
    def upsert_nomenclature(self, sku: str, name: str, unit: str = "шт",
                            min_stock: float = 0, lead_time_days: int = 0,
                            onec_uuid: str = "") -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM nomenclature WHERE sku=%s", (sku,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE nomenclature SET name=%s, unit=%s, min_stock=%s, "
                "lead_time_days=%s, onec_uuid=%s, updated=%s WHERE id=%s",
                (name, unit, min_stock, lead_time_days, onec_uuid, now, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO nomenclature(sku,name,unit,min_stock,lead_time_days,"
            "onec_uuid,created,updated) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (sku, name, unit, min_stock, lead_time_days, onec_uuid, now, now))
        return int(cur.fetchone()[0])

    _NOM_COLS = ("id", "sku", "name", "unit", "min_stock", "lead_time_days",
                "onec_uuid", "created", "updated")

    def _nom_row(self, row: tuple) -> dict[str, Any]:
        d = dict(zip(self._NOM_COLS, row))
        d["min_stock"] = float(d["min_stock"])
        return d

    def get_nomenclature(self, nom_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._NOM_COLS)} FROM nomenclature WHERE id=%s",
            (nom_id,))
        row = cur.fetchone()
        return self._nom_row(row) if row else None

    def get_nomenclature_by_sku(self, sku: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._NOM_COLS)} FROM nomenclature WHERE sku=%s",
            (sku,))
        row = cur.fetchone()
        return self._nom_row(row) if row else None

    def list_nomenclature(self, limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._NOM_COLS)} FROM nomenclature "
            "ORDER BY sku LIMIT %s", (limit,))
        return [self._nom_row(r) for r in cur.fetchall()]

    # -------------------------------------------------------- counterparty
    def upsert_counterparty(self, name: str, inn: str = "", kind: str = "supplier",
                            reliability_score: float = 1.0,
                            onec_uuid: str = "") -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM counterparty WHERE name=%s AND inn=%s", (name, inn))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE counterparty SET kind=%s, reliability_score=%s, "
                "onec_uuid=%s, updated=%s WHERE id=%s",
                (kind, reliability_score, onec_uuid, now, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO counterparty(name,inn,kind,reliability_score,"
            "onec_uuid,created,updated) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (name, inn, kind, reliability_score, onec_uuid, now, now))
        return int(cur.fetchone()[0])

    _CP_COLS = ("id", "name", "inn", "kind", "reliability_score", "onec_uuid",
               "created", "updated")

    def get_counterparty(self, cp_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._CP_COLS)} FROM counterparty WHERE id=%s",
            (cp_id,))
        row = cur.fetchone()
        return dict(zip(self._CP_COLS, row)) if row else None

    def list_counterparties(self, kind: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if kind:
            cur.execute(
                f"SELECT {', '.join(self._CP_COLS)} FROM counterparty "
                "WHERE kind=%s OR kind='both' ORDER BY name LIMIT %s", (kind, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._CP_COLS)} FROM counterparty "
                "ORDER BY name LIMIT %s", (limit,))
        return [dict(zip(self._CP_COLS, r)) for r in cur.fetchall()]

    # ---------------------------------------------------------- prices
    def set_supplier_price(self, nomenclature_id: int, counterparty_id: int,
                           price: float, currency: str = "RUB") -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM supplier_price WHERE nomenclature_id=%s "
            "AND counterparty_id=%s", (nomenclature_id, counterparty_id))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE supplier_price SET price=%s, currency=%s, updated=%s "
                "WHERE id=%s", (price, currency, now, row[0]))
            return int(row[0])
        cur.execute(
            "INSERT INTO supplier_price(nomenclature_id,counterparty_id,price,"
            "currency,updated) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            (nomenclature_id, counterparty_id, price, currency, now))
        return int(cur.fetchone()[0])

    def supplier_prices_for(self, nomenclature_id: int) -> list[dict[str, Any]]:
        """Все известные цены поставщиков на номенклатуру, отсортированные
        по цене — основа для сравнения ценовых предложений агентом."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT sp.id, sp.counterparty_id, c.name, sp.price, sp.currency,
                   c.reliability_score
            FROM supplier_price sp
            JOIN counterparty c ON c.id = sp.counterparty_id
            WHERE sp.nomenclature_id = %s
            ORDER BY sp.price ASC
        """, (nomenclature_id,))
        return [{"id": r[0], "counterparty_id": r[1], "counterparty_name": r[2],
                "price": float(r[3]), "currency": r[4],
                "reliability_score": float(r[5])} for r in cur.fetchall()]

    # ------------------------------------------------------- stock_balance
    def set_stock(self, nomenclature_id: int, quantity: float,
                 warehouse: str = "основной", reserved: float = 0) -> None:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM stock_balance WHERE nomenclature_id=%s AND warehouse=%s",
            (nomenclature_id, warehouse))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE stock_balance SET quantity=%s, reserved=%s, updated=%s "
                "WHERE id=%s", (quantity, reserved, now, row[0]))
        else:
            cur.execute(
                "INSERT INTO stock_balance(nomenclature_id,warehouse,quantity,"
                "reserved,updated) VALUES(%s,%s,%s,%s,%s)",
                (nomenclature_id, warehouse, quantity, reserved, now))

    def stock_for(self, nomenclature_id: int) -> dict[str, Any]:
        """Суммарный остаток по всем складам минус резерв (доступно к
        использованию) — основа для расчёта дефицита агентом."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(quantity),0), COALESCE(SUM(reserved),0) "
            "FROM stock_balance WHERE nomenclature_id=%s", (nomenclature_id,))
        qty, reserved = cur.fetchone()
        return {"quantity": float(qty), "reserved": float(reserved),
               "available": float(qty) - float(reserved)}

    def low_stock_nomenclature(self) -> list[dict[str, Any]]:
        """Номенклатура, у которой доступный остаток ниже min_stock —
        основной источник задач для агента-снабженца."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT n.id, n.sku, n.name, n.unit, n.min_stock, n.lead_time_days,
                   COALESCE(SUM(sb.quantity),0) - COALESCE(SUM(sb.reserved),0)
                   AS available
            FROM nomenclature n
            LEFT JOIN stock_balance sb ON sb.nomenclature_id = n.id
            GROUP BY n.id, n.sku, n.name, n.unit, n.min_stock, n.lead_time_days
            HAVING COALESCE(SUM(sb.quantity),0) - COALESCE(SUM(sb.reserved),0)
                   < n.min_stock
            ORDER BY n.sku
        """)
        return [{"id": r[0], "sku": r[1], "name": r[2], "unit": r[3],
                "min_stock": float(r[4]), "lead_time_days": r[5],
                "available": float(r[6])} for r in cur.fetchall()]

    # ------------------------------------------------------ purchase_request
    def create_purchase_request(self, nomenclature_id: int, quantity: float,
                                reason: str = "", created_by: str = "human") -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO purchase_request(nomenclature_id,quantity,reason,"
            "created_by,created,updated) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (nomenclature_id, quantity, reason, created_by, now, now))
        return int(cur.fetchone()[0])

    _PR_COLS = ("id", "nomenclature_id", "quantity", "reason", "status",
               "created_by", "created", "updated")

    def get_purchase_request(self, pr_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._PR_COLS)} FROM purchase_request WHERE id=%s",
            (pr_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(zip(self._PR_COLS, row))
        d["quantity"] = float(d["quantity"])
        return d

    def list_purchase_requests(self, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if status:
            cur.execute(
                f"SELECT {', '.join(self._PR_COLS)} FROM purchase_request "
                "WHERE status=%s ORDER BY id DESC LIMIT %s", (status, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._PR_COLS)} FROM purchase_request "
                "ORDER BY id DESC LIMIT %s", (limit,))
        out = []
        for r in cur.fetchall():
            d = dict(zip(self._PR_COLS, r))
            d["quantity"] = float(d["quantity"])
            out.append(d)
        return out

    def set_purchase_request_status(self, pr_id: int, status: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE purchase_request SET status=%s, updated=%s WHERE id=%s",
            (status, self._now(), pr_id))
        return cur.rowcount > 0

    # -------------------------------------------------------- purchase_order
    def create_purchase_order(self, counterparty_id: int,
                              lines: list[dict[str, Any]],
                              created_by: str = "human",
                              status: str = "draft") -> int:
        """lines: [{"nomenclature_id","quantity","price","purchase_request_id"?}]"""
        now = self._now()
        total = sum(float(ln["quantity"]) * float(ln["price"]) for ln in lines)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO purchase_order(counterparty_id,status,total_amount,"
            "created_by,created,updated) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (counterparty_id, status, total, created_by, now, now))
        po_id = int(cur.fetchone()[0])
        for ln in lines:
            cur.execute(
                "INSERT INTO purchase_order_line(purchase_order_id,"
                "nomenclature_id,quantity,price,purchase_request_id) "
                "VALUES(%s,%s,%s,%s,%s)",
                (po_id, ln["nomenclature_id"], ln["quantity"], ln["price"],
                 ln.get("purchase_request_id")))
            if ln.get("purchase_request_id"):
                self.set_purchase_request_status(ln["purchase_request_id"], "ordered")
        return po_id

    _PO_COLS = ("id", "counterparty_id", "status", "total_amount",
               "created_by", "approved_by", "onec_uuid", "created", "updated")

    def get_purchase_order(self, po_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._PO_COLS)} FROM purchase_order WHERE id=%s",
            (po_id,))
        row = cur.fetchone()
        if not row:
            return None
        result = dict(zip(self._PO_COLS, row))
        result["total_amount"] = float(result["total_amount"])
        result["lines"] = self.purchase_order_lines(po_id)
        return result

    def purchase_order_lines(self, po_id: int) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT pol.id, pol.nomenclature_id, n.sku, n.name, pol.quantity,
                   pol.price, pol.purchase_request_id
            FROM purchase_order_line pol
            JOIN nomenclature n ON n.id = pol.nomenclature_id
            WHERE pol.purchase_order_id = %s
        """, (po_id,))
        return [{"id": r[0], "nomenclature_id": r[1], "sku": r[2], "name": r[3],
                "quantity": float(r[4]), "price": float(r[5]),
                "purchase_request_id": r[6]} for r in cur.fetchall()]

    def list_purchase_orders(self, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if status:
            cur.execute(
                f"SELECT {', '.join(self._PO_COLS)} FROM purchase_order "
                "WHERE status=%s ORDER BY id DESC LIMIT %s", (status, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._PO_COLS)} FROM purchase_order "
                "ORDER BY id DESC LIMIT %s", (limit,))
        out = []
        for r in cur.fetchall():
            d = dict(zip(self._PO_COLS, r))
            d["total_amount"] = float(d["total_amount"])
            out.append(d)
        return out

    def set_purchase_order_status(self, po_id: int, status: str,
                                  approved_by: str = "") -> bool:
        now = self._now()
        cur = self.conn.cursor()
        if approved_by:
            cur.execute(
                "UPDATE purchase_order SET status=%s, approved_by=%s, "
                "updated=%s WHERE id=%s", (status, approved_by, now, po_id))
        else:
            cur.execute(
                "UPDATE purchase_order SET status=%s, updated=%s WHERE id=%s",
                (status, now, po_id))
        return cur.rowcount > 0

    def delete_purchase_order(self, po_id: int) -> bool:
        """Для rollback полностью автоматических действий агента."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM purchase_order WHERE id=%s", (po_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------ agent_proposal
    def create_proposal(self, agent_slug: str, kind: str, payload: dict[str, Any],
                        explanation: str, autonomy_mode: str,
                        sources: list[dict[str, Any]] | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO agent_proposal(agent_slug,kind,payload,explanation,"
            "sources,autonomy_mode,created) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (agent_slug, kind, json.dumps(payload, ensure_ascii=False),
             explanation, json.dumps(sources or [], ensure_ascii=False),
             autonomy_mode, now))
        return int(cur.fetchone()[0])

    _PROP_COLS = ("id", "agent_slug", "kind", "payload", "explanation",
                 "sources", "autonomy_mode", "status", "result_document_type",
                 "result_document_id", "decided_by", "created", "decided")

    def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._PROP_COLS)} FROM agent_proposal WHERE id=%s",
            (proposal_id,))
        row = cur.fetchone()
        return dict(zip(self._PROP_COLS, row)) if row else None

    def list_proposals(self, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if status:
            cur.execute(
                f"SELECT {', '.join(self._PROP_COLS)} FROM agent_proposal "
                "WHERE status=%s ORDER BY id DESC LIMIT %s", (status, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(self._PROP_COLS)} FROM agent_proposal "
                "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip(self._PROP_COLS, r)) for r in cur.fetchall()]

    def set_proposal_decision(self, proposal_id: int, status: str,
                              decided_by: str, result_document_type: str = "",
                              result_document_id: int | None = None) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE agent_proposal SET status=%s, decided_by=%s, decided=%s, "
            "result_document_type=%s, result_document_id=%s WHERE id=%s",
            (status, decided_by, self._now(), result_document_type,
             result_document_id, proposal_id))
        return cur.rowcount > 0

    # ------------------------------------------------------------- audit
    def log_audit(self, actor: str, action: str, entity_type: str,
                 entity_id: int | None, details: dict[str, Any] | None = None) -> int:
        """Добавляет запись в НЕИЗМЕНЯЕМЫЙ журнал аудита. В коде Store
        сознательно нет ни одного метода update/delete для audit_log —
        это прикладная гарантия неизменности (полная гарантия на уровне
        СУБД требует отдельного пользователя без прав UPDATE/DELETE,
        см. README.md)."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,"
            "details,created) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (actor, action, entity_type, entity_id,
             json.dumps(details or {}, ensure_ascii=False), self._now()))
        return int(cur.fetchone()[0])

    def audit_trail_for(self, entity_type: str, entity_id: int) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, actor, action, entity_type, entity_id, details, created "
            "FROM audit_log WHERE entity_type=%s AND entity_id=%s ORDER BY id",
            (entity_type, entity_id))
        return [{"id": r[0], "actor": r[1], "action": r[2], "entity_type": r[3],
                "entity_id": r[4], "details": r[5], "created": r[6]}
                for r in cur.fetchall()]

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, actor, action, entity_type, entity_id, details, created "
            "FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))
        return [{"id": r[0], "actor": r[1], "action": r[2], "entity_type": r[3],
                "entity_id": r[4], "details": r[5], "created": r[6]}
                for r in cur.fetchall()]

    # ------------------------------------------------------- onec_sync_log
    def onec_log_attempt(self, direction: str, entity_type: str,
                         entity_id: int | None, idempotency_key: str,
                         external_id: str = "") -> tuple[int, bool]:
        """Регистрирует попытку синхронизации. Возвращает (id, is_new) —
        is_new=False означает, что запись с таким idempotency_key уже
        была (повторный вызов не будет отправлен в 1С дважды)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, status FROM onec_sync_log WHERE idempotency_key=%s",
            (idempotency_key,))
        row = cur.fetchone()
        if row:
            return int(row[0]), False
        now = self._now()
        cur.execute(
            "INSERT INTO onec_sync_log(direction,entity_type,entity_id,"
            "external_id,idempotency_key,created,updated) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (direction, entity_type, entity_id, external_id, idempotency_key,
             now, now))
        return int(cur.fetchone()[0]), True

    def onec_mark_result(self, log_id: int, status: str, error: str = "",
                         external_id: str = "") -> None:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE onec_sync_log SET status=%s, error=%s, "
            "external_id=COALESCE(NULLIF(%s,''), external_id), "
            "attempts=attempts+1, updated=%s WHERE id=%s",
            (status, error, external_id, self._now(), log_id))

    def onec_sync_log_list(self, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cols = ("id", "direction", "entity_type", "entity_id", "external_id",
               "idempotency_key", "status", "error", "attempts", "created",
               "updated")
        if status:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM onec_sync_log WHERE status=%s "
                "ORDER BY id DESC LIMIT %s", (status, limit))
        else:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM onec_sync_log "
                "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------ metrics
    def dashboard_stats(self) -> dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM nomenclature")
        nom = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM counterparty")
        cp = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM purchase_request WHERE status='open'")
        open_pr = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM purchase_order")
        po = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM agent_proposal WHERE status='pending'")
        pending_proposals = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM audit_log")
        audit_count = cur.fetchone()[0]
        low_stock = len(self.low_stock_nomenclature())
        return {
            "nomenclature": int(nom), "counterparties": int(cp),
            "open_purchase_requests": int(open_pr),
            "purchase_orders": int(po),
            "pending_proposals": int(pending_proposals),
            "audit_entries": int(audit_count),
            "low_stock_items": low_stock,
        }
