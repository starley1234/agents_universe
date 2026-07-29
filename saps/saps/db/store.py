"""Доступ к PostgreSQL. Здесь же — инварианты прослеживаемости.

ПОЧЕМУ ЭТО НЕ ТОНКАЯ ОБЁРТКА НАД SQL. Store отвечает за то, чтобы
данные нельзя было испортить в обход правил предметной области:

  * текст требования меняется ТОЛЬКО через update_requirement/
    apply_suggestion, и обе операции в ОДНОЙ транзакции пишут строку в
    requirement_revision. Правка без следа невозможна не по соглашению,
    а по устройству кода;
  * предложение агента можно применить один раз: повторный accept
    получает отказ, а не создаёт вторую ревизию с тем же содержанием;
  * связь требования с пунктом АП, предложенная агентом, приходит
    неподтверждённой (confirmed=false) — подтверждает человек.

psycopg импортируется ЛЕНИВО: `saps.db.store` должен импортироваться на
машине без драйвера (например, чтобы прочитать docstring или прогнать
тесты парсера), а падать — только при реальной попытке подключения.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable, Sequence

from .schema import (COMPLIANCE_STATUSES, MOC_CODES, REQUIREMENT_STATUSES,
                     SUGGESTION_STATUSES, schema_sql, vector_index_sql)


class StoreError(RuntimeError):
    """Ожидаемая ошибка работы с базой: нет объекта, нарушен инвариант."""


def _require_psycopg():
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
    except ImportError as exc:                                   # pragma: no cover
        raise StoreError(
            "Нужен драйвер PostgreSQL. Установите: pip install 'psycopg[binary]'. "
            "САПС не работает без базы — см. принцип Database-First в ТЗ п.2.3."
        ) from exc
    return psycopg, dict_row


def _vec(values: Sequence[float] | None) -> str | None:
    """Список float -> литерал pgvector '[1,2,3]'."""
    if values is None:
        return None
    return "[" + ",".join(f"{float(v):.6f}" for v in values) + "]"


class Store:
    """Соединение с базой САПС и операции над предметной моделью."""

    def __init__(self, dsn: str, *, schema: str = "saps", dim: int = 512,
                 autocommit: bool = True) -> None:
        psycopg, dict_row = _require_psycopg()
        self.dsn = dsn
        self.schema = schema
        self.dim = dim
        try:
            self.conn = psycopg.connect(dsn, autocommit=autocommit,
                                        row_factory=dict_row)
        except Exception as exc:                                 # noqa: BLE001
            raise StoreError(
                f"Не удалось подключиться к PostgreSQL: {exc}. Проверьте "
                "SAPS_DB_DSN и доступность сервера.") from exc

    # --- жизненный цикл ---------------------------------------------------
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:                                        # noqa: BLE001
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- устойчивость соединения (прод) ---------------------------------
    def ping(self) -> bool:
        """Живо ли соединение. Дешёвый запрос, без побочных эффектов."""
        try:
            self.conn.execute("SELECT 1")
            return True
        except Exception:                                        # noqa: BLE001
            return False

    def reconnect(self) -> None:
        """Пересоздать соединение.

        В проде БД перезапускают (обновление, отказ реплики, обрыв сети),
        и psycopg НЕ восстанавливает соединение сам: все последующие
        запросы падают до перезапуска приложения. Для рабочего места КБ
        это означало бы «после планового обслуживания базы САПС надо
        рестартовать» — неприемлемо, поэтому переподключаемся сами.
        """
        psycopg, dict_row = _require_psycopg()
        try:
            self.conn.close()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self.conn = psycopg.connect(self.dsn, autocommit=True,
                                        row_factory=dict_row)
        except Exception as exc:                                 # noqa: BLE001
            raise StoreError(
                f"Не удалось переподключиться к PostgreSQL: {exc}") from exc

    def ensure_alive(self) -> None:
        """Проверить соединение и восстановить, если оно оборвалось."""
        if not self.ping():
            self.reconnect()

    def health(self) -> dict[str, Any]:
        """Состояние хранилища для /health и `saps check`.

        Проверяется не «процесс жив», а «база отвечает и схема та,
        которую понимает код» — именно это должен знать мониторинг,
        чтобы не держать в ротации инстанс, который не может работать.
        """
        from .migrate import SCHEMA_VERSION, detect_version

        out: dict[str, Any] = {"database": "down", "schema_version": None,
                               "expected_schema_version": SCHEMA_VERSION,
                               "pgvector": False}
        try:
            self.ensure_alive()
        except StoreError as exc:
            out["error"] = str(exc)
            return out
        out["database"] = "ok"
        try:
            row = self.conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
            out["pgvector"] = row is not None
            out["schema_version"] = detect_version(self.conn, self.schema)
        except Exception as exc:                                 # noqa: BLE001
            out["error"] = str(exc)
        return out

    def init_schema(self) -> None:
        """Создать схему. Идемпотентно."""
        try:
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:                                 # noqa: BLE001
            raise StoreError(
                f"Не удалось включить расширение pgvector: {exc}. САПС ищет "
                "требования и пункты АП по смыслу, для этого нужен pgvector. "
                "Установите его в базу (CREATE EXTENSION vector) или "
                "попросите администратора.") from exc
        self.conn.execute(schema_sql(self.schema, self.dim))

    def build_vector_indexes(self) -> None:
        """Построить ivfflat-индексы. Вызывать после наполнения таблиц."""
        for sql in vector_index_sql(self.schema):
            self.conn.execute(sql)

    # --- служебное --------------------------------------------------------
    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return list(self.conn.execute(sql, args).fetchall())

    def _scalar(self, sql: str, args: tuple = ()) -> Any:
        row = self.conn.execute(sql, args).fetchone()
        if not row:
            return None
        return next(iter(row.values()))

    @property
    def s(self) -> str:
        return self.schema

    # ============ СЛОЙ ИМПОРТА ============================================
    def add_source_document(self, kind: str, name: str, uri: str = "",
                            content_hash: str = "", imported_by: str = "",
                            meta: dict[str, Any] | None = None) -> int:
        row = self._one(
            f"INSERT INTO {self.s}.source_document"
            "(kind, name, uri, content_hash, imported_by, meta) "
            "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (kind, name, uri, content_hash, imported_by,
             json.dumps(meta or {}, ensure_ascii=False)))
        return int(row["id"])

    def find_document_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        if not content_hash:
            return None
        return self._one(
            f"SELECT * FROM {self.s}.source_document WHERE content_hash=%s "
            "ORDER BY id DESC LIMIT 1", (content_hash,))

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all(
            f"SELECT d.*, "
            f"(SELECT COUNT(*) FROM {self.s}.staging_record r "
            f" WHERE r.document_id=d.id) AS records "
            f"FROM {self.s}.source_document d ORDER BY d.id DESC LIMIT %s",
            (limit,))

    def add_staging_records(self, document_id: int,
                            records: Iterable[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        for i, rec in enumerate(records):
            row = self._one(
                f"INSERT INTO {self.s}.staging_record"
                "(document_id, ord, external_id, section_path, raw_text, raw) "
                "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (document_id, rec.get("ord", i), rec.get("external_id", ""),
                 rec.get("section_path", ""), rec.get("raw_text", ""),
                 json.dumps(rec.get("raw", {}), ensure_ascii=False)))
            ids.append(int(row["id"]))
        return ids

    def staging_records(self, document_id: int | None = None,
                        status: str = "", limit: int = 500
                        ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {self.s}.staging_record WHERE 1=1"
        args: list[Any] = []
        if document_id is not None:
            sql += " AND document_id=%s"
            args.append(document_id)
        if status:
            sql += " AND status=%s"
            args.append(status)
        return self._all(sql + " ORDER BY document_id, ord LIMIT %s",
                         tuple(args + [limit]))

    def set_staging_status(self, staging_id: int, status: str,
                           note: str = "") -> None:
        self.conn.execute(
            f"UPDATE {self.s}.staging_record SET status=%s, note=%s WHERE id=%s",
            (status, note, staging_id))

    # ============ УЗЛЫ ИЗДЕЛИЯ ===========================================
    def upsert_node(self, code: str, name: str = "",
                    parent_code: str = "", tc_uid: str = "",
                    meta: dict[str, Any] | None = None) -> int:
        parent_id = None
        if parent_code:
            parent = self._one(
                f"SELECT id FROM {self.s}.product_node WHERE code=%s",
                (parent_code,))
            if parent is None:
                parent_id = self.upsert_node(parent_code)
            else:
                parent_id = int(parent["id"])
        row = self._one(
            f"INSERT INTO {self.s}.product_node(code, name, parent_id, tc_uid, meta) "
            "VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT (code) DO UPDATE SET "
            "  name=COALESCE(NULLIF(EXCLUDED.name,''), product_node.name), "
            "  parent_id=COALESCE(EXCLUDED.parent_id, product_node.parent_id), "
            "  tc_uid=COALESCE(NULLIF(EXCLUDED.tc_uid,''), product_node.tc_uid) "
            "RETURNING id",
            (code, name, parent_id, tc_uid,
             json.dumps(meta or {}, ensure_ascii=False)))
        return int(row["id"])

    def get_node(self, code: str) -> dict[str, Any] | None:
        return self._one(f"SELECT * FROM {self.s}.product_node WHERE code=%s",
                         (code,))

    def list_nodes(self) -> list[dict[str, Any]]:
        return self._all(f"SELECT * FROM {self.s}.product_node ORDER BY code")

    # ============ ТРЕБОВАНИЯ =============================================
    def create_requirement(self, external_id: str, text: str, *,
                           title: str = "", status: str = "draft",
                           node_code: str = "", owner: str = "",
                           document_id: int | None = None,
                           staging_id: int | None = None,
                           tc_uid: str = "",
                           attributes: dict[str, Any] | None = None,
                           embedding: Sequence[float] | None = None,
                           actor: str = "") -> int:
        if status not in REQUIREMENT_STATUSES:
            raise StoreError(
                f"Статус {status!r} неизвестен, допустимо: "
                f"{', '.join(REQUIREMENT_STATUSES)}")
        if not external_id.strip():
            raise StoreError("У требования должен быть непустой external_id")
        node_id = self.upsert_node(node_code) if node_code else None
        try:
            row = self._one(
                f"INSERT INTO {self.s}.requirement"
                "(external_id, title, text, status, node_id, owner, document_id,"
                " staging_id, tc_uid, attributes, embedding) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (external_id.strip(), title, text, status, node_id, owner,
                 document_id, staging_id, tc_uid,
                 json.dumps(attributes or {}, ensure_ascii=False),
                 _vec(embedding)))
        except Exception as exc:                                 # noqa: BLE001
            if "requirement_external_id_key" in str(exc):
                raise StoreError(
                    f"Требование {external_id!r} уже есть в базе. Импорт "
                    "того же документа повторно? Используйте upsert или "
                    "проверьте content_hash источника.") from exc
            raise
        req_id = int(row["id"])
        # Нулевая ревизия: даже создание должно быть видно в истории.
        self.conn.execute(
            f"INSERT INTO {self.s}.requirement_revision"
            "(requirement_id, version, text_before, text_after, status_before,"
            " status_after, reason, actor) VALUES(%s,1,'',%s,'',%s,%s,%s)",
            (req_id, text, status, "создано при импорте", actor))
        return req_id

    def get_requirement(self, req_id: int) -> dict[str, Any] | None:
        return self._one(
            f"SELECT r.*, n.code AS node_code, n.name AS node_name "
            f"FROM {self.s}.requirement r "
            f"LEFT JOIN {self.s}.product_node n ON n.id=r.node_id "
            "WHERE r.id=%s", (req_id,))

    def get_requirement_by_external(self, external_id: str
                                    ) -> dict[str, Any] | None:
        return self._one(
            f"SELECT * FROM {self.s}.requirement WHERE external_id=%s",
            (external_id,))

    def list_requirements(self, *, owner: str = "", node_code: str = "",
                          status: str = "", query: str = "",
                          limit: int = 200, offset: int = 0
                          ) -> list[dict[str, Any]]:
        sql = (f"SELECT r.*, n.code AS node_code FROM {self.s}.requirement r "
               f"LEFT JOIN {self.s}.product_node n ON n.id=r.node_id WHERE 1=1")
        args: list[Any] = []
        if owner:
            sql += " AND r.owner=%s"
            args.append(owner)
        if node_code:
            sql += " AND n.code=%s"
            args.append(node_code)
        if status:
            sql += " AND r.status=%s"
            args.append(status)
        if query:
            sql += " AND (r.text ILIKE %s OR r.external_id ILIKE %s "
            sql += "OR r.title ILIKE %s)"
            like = f"%{query}%"
            args += [like, like, like]
        sql += " ORDER BY r.external_id LIMIT %s OFFSET %s"
        return self._all(sql, tuple(args + [limit, offset]))

    def count_requirements(self, **kw: Any) -> int:
        kw.pop("limit", None)
        kw.pop("offset", None)
        return len(self.list_requirements(limit=100000, **kw))

    def update_requirement(self, req_id: int, *, text: str | None = None,
                           status: str | None = None, owner: str | None = None,
                           title: str | None = None,
                           attributes: dict[str, Any] | None = None,
                           node_code: str | None = None,
                           reason: str = "", actor: str = "") -> int:
        """Изменить требование, ОБЯЗАТЕЛЬНО оставив след в истории.

        Возвращает номер новой ревизии. Если ни текст, ни статус не
        меняются, ревизия всё равно создаётся при изменении смысловых
        полей — но не создаётся при пустом вызове, чтобы история не
        замусоривалась.
        """
        current = self.get_requirement(req_id)
        if current is None:
            raise StoreError(f"Требование #{req_id} не найдено")
        if status is not None and status not in REQUIREMENT_STATUSES:
            raise StoreError(f"Статус {status!r} неизвестен")

        sets, args = [], []
        text_changed = text is not None and text != current["text"]
        status_changed = status is not None and status != current["status"]
        if text_changed:
            sets.append("text=%s")
            args.append(text)
        if status_changed:
            sets.append("status=%s")
            args.append(status)
        if owner is not None and owner != current["owner"]:
            sets.append("owner=%s")
            args.append(owner)
        if title is not None and title != current["title"]:
            sets.append("title=%s")
            args.append(title)
        if attributes is not None:
            sets.append("attributes=%s")
            args.append(json.dumps(attributes, ensure_ascii=False))
        if node_code is not None:
            sets.append("node_id=%s")
            args.append(self.upsert_node(node_code) if node_code else None)
        if not sets:
            return self._current_version(req_id)

        sets.append("updated_at=now()")
        version = self._current_version(req_id) + 1
        # Транзакция: правка и запись в историю неразделимы. Иначе
        # обрыв между двумя запросами оставил бы изменение без следа.
        with self.conn.transaction():
            self.conn.execute(
                f"UPDATE {self.s}.requirement SET {', '.join(sets)} WHERE id=%s",
                tuple(args + [req_id]))
            self.conn.execute(
                f"INSERT INTO {self.s}.requirement_revision"
                "(requirement_id, version, text_before, text_after,"
                " status_before, status_after, reason, actor) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (req_id, version,
                 current["text"] if text_changed else "",
                 text if text_changed else "",
                 current["status"] if status_changed else "",
                 status if status_changed else "",
                 reason, actor))
        return version

    def _current_version(self, req_id: int) -> int:
        value = self._scalar(
            f"SELECT COALESCE(MAX(version),0) FROM {self.s}.requirement_revision "
            "WHERE requirement_id=%s", (req_id,))
        return int(value or 0)

    def revisions(self, req_id: int) -> list[dict[str, Any]]:
        return self._all(
            f"SELECT * FROM {self.s}.requirement_revision "
            "WHERE requirement_id=%s ORDER BY version", (req_id,))

    def set_requirement_embedding(self, req_id: int,
                                  embedding: Sequence[float]) -> None:
        self.conn.execute(
            f"UPDATE {self.s}.requirement SET embedding=%s WHERE id=%s",
            (_vec(embedding), req_id))

    def set_quality(self, req_id: int, score: float,
                    detail: dict[str, Any]) -> None:
        self.conn.execute(
            f"UPDATE {self.s}.requirement SET quality_score=%s, quality=%s, "
            "updated_at=now() WHERE id=%s",
            (float(score), json.dumps(detail, ensure_ascii=False), req_id))

    def requirements_without_embedding(self, limit: int = 500
                                       ) -> list[dict[str, Any]]:
        return self._all(
            f"SELECT id, external_id, title, text FROM {self.s}.requirement "
            "WHERE embedding IS NULL ORDER BY id LIMIT %s", (limit,))

    def similar_requirements(self, embedding: Sequence[float], *,
                             limit: int = 5, exclude_id: int | None = None,
                             min_score: float = 0.0) -> list[dict[str, Any]]:
        """Поиск похожих требований (дубли) по косинусному расстоянию."""
        sql = (f"SELECT id, external_id, title, text, "
               f"1 - (embedding <=> %s) AS score FROM {self.s}.requirement "
               "WHERE embedding IS NOT NULL")
        args: list[Any] = [_vec(embedding)]
        if exclude_id is not None:
            sql += " AND id <> %s"
            args.append(exclude_id)
        sql += " ORDER BY embedding <=> %s LIMIT %s"
        args += [_vec(embedding), limit]
        rows = self._all(sql, tuple(args))
        return [r for r in rows if float(r["score"]) >= min_score]

    # ============ АВИАЦИОННЫЕ ПРАВИЛА ====================================
    def upsert_clause(self, ruleset: str, clause: str, *, title: str = "",
                      text: str = "", keywords: str = "",
                      embedding: Sequence[float] | None = None,
                      meta: dict[str, Any] | None = None) -> int:
        row = self._one(
            f"INSERT INTO {self.s}.rule_clause"
            "(ruleset, clause, title, text, keywords, embedding, meta) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (ruleset, clause) DO UPDATE SET "
            "  title=EXCLUDED.title, text=EXCLUDED.text, "
            "  keywords=EXCLUDED.keywords, "
            "  embedding=COALESCE(EXCLUDED.embedding, rule_clause.embedding), "
            "  meta=EXCLUDED.meta RETURNING id",
            (ruleset, clause, title, text, keywords, _vec(embedding),
             json.dumps(meta or {}, ensure_ascii=False)))
        return int(row["id"])

    def get_clause(self, ruleset: str, clause: str) -> dict[str, Any] | None:
        return self._one(
            f"SELECT * FROM {self.s}.rule_clause WHERE ruleset=%s AND clause=%s",
            (ruleset, clause))

    def list_clauses(self, ruleset: str = "", limit: int = 1000
                     ) -> list[dict[str, Any]]:
        if ruleset:
            return self._all(
                f"SELECT id, ruleset, clause, title, text, keywords "
                f"FROM {self.s}.rule_clause WHERE ruleset=%s "
                "ORDER BY clause LIMIT %s", (ruleset, limit))
        return self._all(
            f"SELECT id, ruleset, clause, title, text, keywords "
            f"FROM {self.s}.rule_clause ORDER BY ruleset, clause LIMIT %s",
            (limit,))

    def clauses_without_embedding(self, limit: int = 1000
                                  ) -> list[dict[str, Any]]:
        return self._all(
            f"SELECT id, ruleset, clause, title, text, keywords "
            f"FROM {self.s}.rule_clause WHERE embedding IS NULL "
            "ORDER BY id LIMIT %s", (limit,))

    def set_clause_embedding(self, clause_id: int,
                             embedding: Sequence[float]) -> None:
        self.conn.execute(
            f"UPDATE {self.s}.rule_clause SET embedding=%s WHERE id=%s",
            (_vec(embedding), clause_id))

    def search_clauses(self, embedding: Sequence[float], *, limit: int = 5,
                       ruleset: str = "") -> list[dict[str, Any]]:
        sql = (f"SELECT id, ruleset, clause, title, text, "
               f"1 - (embedding <=> %s) AS score FROM {self.s}.rule_clause "
               "WHERE embedding IS NOT NULL")
        args: list[Any] = [_vec(embedding)]
        if ruleset:
            sql += " AND ruleset=%s"
            args.append(ruleset)
        sql += " ORDER BY embedding <=> %s LIMIT %s"
        args += [_vec(embedding), limit]
        return self._all(sql, tuple(args))

    def link_requirement_clause(self, req_id: int, clause_id: int, *,
                                score: float = 0.0, source: str = "agent",
                                confirmed: bool = False,
                                confirmed_by: str = "") -> int:
        row = self._one(
            f"INSERT INTO {self.s}.requirement_rule_link"
            "(requirement_id, clause_id, score, source, confirmed, confirmed_by) "
            "VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (requirement_id, clause_id) DO UPDATE SET "
            "  score=GREATEST(requirement_rule_link.score, EXCLUDED.score), "
            "  confirmed=requirement_rule_link.confirmed OR EXCLUDED.confirmed, "
            "  confirmed_by=CASE WHEN EXCLUDED.confirmed "
            "     THEN EXCLUDED.confirmed_by "
            "     ELSE requirement_rule_link.confirmed_by END "
            "RETURNING id",
            (req_id, clause_id, float(score), source, confirmed, confirmed_by))
        return int(row["id"])

    def confirm_link(self, link_id: int, actor: str) -> None:
        updated = self.conn.execute(
            f"UPDATE {self.s}.requirement_rule_link "
            "SET confirmed=TRUE, confirmed_by=%s WHERE id=%s",
            (actor, link_id))
        if updated.rowcount == 0:
            raise StoreError(f"Связь #{link_id} не найдена")

    def requirement_links(self, req_id: int) -> list[dict[str, Any]]:
        return self._all(
            f"SELECT l.*, c.ruleset, c.clause, c.title "
            f"FROM {self.s}.requirement_rule_link l "
            f"JOIN {self.s}.rule_clause c ON c.id=l.clause_id "
            "WHERE l.requirement_id=%s ORDER BY l.score DESC", (req_id,))

    # ============ ДОКАЗАТЕЛЬНАЯ ДОКУМЕНТАЦИЯ =============================
    def add_compliance_item(self, req_id: int, moc: str, *,
                            status: str = "planned", responsible: str = "",
                            planned_date: date | None = None,
                            note: str = "") -> int:
        if moc not in MOC_CODES:
            raise StoreError(
                f"Метод подтверждения {moc!r} неизвестен. Допустимые коды: "
                f"{', '.join(sorted(MOC_CODES))}")
        if status not in COMPLIANCE_STATUSES:
            raise StoreError(f"Статус {status!r} неизвестен")
        row = self._one(
            f"INSERT INTO {self.s}.compliance_item"
            "(requirement_id, moc, status, responsible, planned_date, note) "
            "VALUES(%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (requirement_id, moc) DO UPDATE SET "
            "  status=EXCLUDED.status, responsible=EXCLUDED.responsible, "
            "  planned_date=EXCLUDED.planned_date, note=EXCLUDED.note, "
            "  updated_at=now() RETURNING id",
            (req_id, moc, status, responsible, planned_date, note))
        return int(row["id"])

    def set_compliance_status(self, item_id: int, status: str) -> None:
        if status not in COMPLIANCE_STATUSES:
            raise StoreError(f"Статус {status!r} неизвестен")
        self.conn.execute(
            f"UPDATE {self.s}.compliance_item SET status=%s, updated_at=now() "
            "WHERE id=%s", (status, item_id))

    def compliance_items(self, req_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            f"SELECT * FROM {self.s}.compliance_item WHERE requirement_id=%s "
            "ORDER BY moc", (req_id,))
        for r in rows:
            r["evidence"] = self.evidence(int(r["id"]))
        return rows

    def add_evidence(self, compliance_id: int, *, kind: str = "report",
                     title: str = "", uri: str = "",
                     issued_at: date | None = None, note: str = "") -> int:
        exists = self._one(
            f"SELECT id FROM {self.s}.compliance_item WHERE id=%s",
            (compliance_id,))
        if exists is None:
            raise StoreError(f"Пункт доказательства #{compliance_id} не найден")
        row = self._one(
            f"INSERT INTO {self.s}.evidence"
            "(compliance_id, kind, title, uri, issued_at, note) "
            "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (compliance_id, kind, title, uri, issued_at, note))
        return int(row["id"])

    def evidence(self, compliance_id: int) -> list[dict[str, Any]]:
        return self._all(
            f"SELECT * FROM {self.s}.evidence WHERE compliance_id=%s "
            "ORDER BY id", (compliance_id,))

    # ============ ПРЕДЛОЖЕНИЯ АГЕНТОВ ====================================
    def add_suggestion(self, req_id: int, agent: str, *, kind: str = "text",
                       text_before: str = "", text_after: str = "",
                       payload: dict[str, Any] | None = None,
                       rationale: str = "", score: float | None = None) -> int:
        exists = self._one(f"SELECT id FROM {self.s}.requirement WHERE id=%s",
                           (req_id,))
        if exists is None:
            raise StoreError(f"Требование #{req_id} не найдено")
        row = self._one(
            f"INSERT INTO {self.s}.suggestion"
            "(requirement_id, agent, kind, text_before, text_after, payload,"
            " rationale, score) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (req_id, agent, kind, text_before, text_after,
             json.dumps(payload or {}, ensure_ascii=False), rationale, score))
        return int(row["id"])

    def get_suggestion(self, sug_id: int) -> dict[str, Any] | None:
        return self._one(f"SELECT * FROM {self.s}.suggestion WHERE id=%s",
                         (sug_id,))

    def list_suggestions(self, *, req_id: int | None = None,
                         status: str = "pending", agent: str = "",
                         limit: int = 200) -> list[dict[str, Any]]:
        sql = (f"SELECT s.*, r.external_id, r.owner FROM {self.s}.suggestion s "
               f"JOIN {self.s}.requirement r ON r.id=s.requirement_id WHERE 1=1")
        args: list[Any] = []
        if req_id is not None:
            sql += " AND s.requirement_id=%s"
            args.append(req_id)
        if status:
            sql += " AND s.status=%s"
            args.append(status)
        if agent:
            sql += " AND s.agent=%s"
            args.append(agent)
        return self._all(sql + " ORDER BY s.id DESC LIMIT %s",
                         tuple(args + [limit]))

    def decide_suggestion(self, sug_id: int, decision: str, actor: str,
                          ) -> dict[str, Any]:
        """Принять или отклонить предложение агента (ТЗ п.6.2).

        Принятие ПРИМЕНЯЕТ изменение и пишет ревизию — в одной
        транзакции с самим решением. Иначе возможен разрыв: предложение
        помечено принятым, а текст остался старым.
        """
        if decision not in ("accepted", "rejected"):
            raise StoreError("Решение может быть только accepted или rejected")
        sug = self.get_suggestion(sug_id)
        if sug is None:
            raise StoreError(f"Предложение #{sug_id} не найдено")
        if sug["status"] != "pending":
            raise StoreError(
                f"Предложение #{sug_id} уже обработано "
                f"(статус {sug['status']!r}) — повторное решение не имеет смысла")

        if decision == "rejected":
            self.conn.execute(
                f"UPDATE {self.s}.suggestion SET status='rejected', "
                "decided_by=%s, decided_at=now() WHERE id=%s", (actor, sug_id))
            return {"applied": False, "suggestion_id": sug_id}

        req_id = int(sug["requirement_id"])
        applied: dict[str, Any] = {"applied": True, "suggestion_id": sug_id,
                                   "kind": sug["kind"]}
        payload = sug["payload"] or {}
        if isinstance(payload, str):
            payload = json.loads(payload)

        if sug["kind"] == "text":
            version = self.update_requirement(
                req_id, text=sug["text_after"],
                reason=f"принято предложение #{sug_id} ({sug['agent']})",
                actor=actor)
            applied["version"] = version
        elif sug["kind"] == "rule_link":
            clause_id = int(payload.get("clause_id", 0))
            if not clause_id:
                raise StoreError(
                    f"Предложение #{sug_id}: в payload нет clause_id")
            self.link_requirement_clause(
                req_id, clause_id, score=float(sug["score"] or 0.0),
                source="agent", confirmed=True, confirmed_by=actor)
            applied["clause_id"] = clause_id
        elif sug["kind"] == "moc":
            moc = str(payload.get("moc", ""))
            self.add_compliance_item(req_id, moc,
                                     note=f"предложено агентом {sug['agent']}")
            applied["moc"] = moc
        elif sug["kind"] == "attribute":
            current = self.get_requirement(req_id)
            attrs = dict(current["attributes"] or {})
            attrs.update(payload.get("attributes", {}))
            self.update_requirement(
                req_id, attributes=attrs,
                reason=f"принято предложение #{sug_id}", actor=actor)
        else:
            raise StoreError(f"Неизвестный вид предложения: {sug['kind']!r}")

        self.conn.execute(
            f"UPDATE {self.s}.suggestion SET status='accepted', decided_by=%s, "
            "decided_at=now() WHERE id=%s", (actor, sug_id))
        return applied

    # ============ ЖУРНАЛ =================================================
    def log(self, actor: str, action: str, *, object_type: str = "",
            object_id: int | None = None, detail: str = "",
            data: dict[str, Any] | None = None) -> int:
        row = self._one(
            f"INSERT INTO {self.s}.audit_log"
            "(actor, action, object_type, object_id, detail, data) "
            "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
            (actor, action, object_type, object_id, detail,
             json.dumps(data or {}, ensure_ascii=False)))
        return int(row["id"])

    def audit(self, *, object_type: str = "", object_id: int | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {self.s}.audit_log WHERE 1=1"
        args: list[Any] = []
        if object_type:
            sql += " AND object_type=%s"
            args.append(object_type)
        if object_id is not None:
            sql += " AND object_id=%s"
            args.append(object_id)
        return self._all(sql + " ORDER BY id DESC LIMIT %s",
                         tuple(args + [limit]))

    # ============ АНАЛИТИКА / ЗДОРОВЬЕ СЕРТИФИКАЦИИ ======================
    def mark_tc_synced(self, req_id: int, tc_uid: str = "") -> None:
        if tc_uid:
            self.conn.execute(
                f"UPDATE {self.s}.requirement SET tc_synced_at=now(), tc_uid=%s "
                "WHERE id=%s", (tc_uid, req_id))
        else:
            self.conn.execute(
                f"UPDATE {self.s}.requirement SET tc_synced_at=now() WHERE id=%s",
                (req_id,))

    def clear_embeddings(self) -> dict[str, int]:
        """Сбросить все векторы (смена модели эмбеддингов).

        Векторы разных моделей лежат в РАЗНЫХ пространствах: косинус
        между ними бессмысленен, и поиск начинает возвращать случайные
        пункты — молча, без единой ошибки. Поэтому при смене модели
        старые векторы обязаны быть сброшены и пересчитаны, а не
        дополнены новыми.
        """
        c = self.conn.execute(
            f"UPDATE {self.s}.rule_clause SET embedding=NULL "
            "WHERE embedding IS NOT NULL")
        r = self.conn.execute(
            f"UPDATE {self.s}.requirement SET embedding=NULL "
            "WHERE embedding IS NOT NULL")
        return {"clauses": c.rowcount, "requirements": r.rowcount}

    def embedding_coverage(self) -> dict[str, int]:
        """Сколько объектов уже проиндексировано — для диагностики."""
        def n(sql: str) -> int:
            return int(self._scalar(sql) or 0)
        return {
            "clauses_total": n(f"SELECT COUNT(*) FROM {self.s}.rule_clause"),
            "clauses_indexed": n(f"SELECT COUNT(*) FROM {self.s}.rule_clause "
                                 "WHERE embedding IS NOT NULL"),
            "requirements_total": n(f"SELECT COUNT(*) FROM {self.s}.requirement"),
            "requirements_indexed": n(
                f"SELECT COUNT(*) FROM {self.s}.requirement "
                "WHERE embedding IS NOT NULL"),
        }

    def stats(self) -> dict[str, Any]:
        def n(sql: str, args: tuple = ()) -> int:
            return int(self._scalar(sql, args) or 0)

        return {
            "documents": n(f"SELECT COUNT(*) FROM {self.s}.source_document"),
            "staging": n(f"SELECT COUNT(*) FROM {self.s}.staging_record"),
            "staging_new": n(f"SELECT COUNT(*) FROM {self.s}.staging_record "
                             "WHERE status='new'"),
            "requirements": n(f"SELECT COUNT(*) FROM {self.s}.requirement"),
            "requirements_approved": n(
                f"SELECT COUNT(*) FROM {self.s}.requirement WHERE status='approved'"),
            "clauses": n(f"SELECT COUNT(*) FROM {self.s}.rule_clause"),
            "links_confirmed": n(
                f"SELECT COUNT(*) FROM {self.s}.requirement_rule_link "
                "WHERE confirmed"),
            "compliance_items": n(f"SELECT COUNT(*) FROM {self.s}.compliance_item"),
            "evidence": n(f"SELECT COUNT(*) FROM {self.s}.evidence"),
            "suggestions_pending": n(
                f"SELECT COUNT(*) FROM {self.s}.suggestion WHERE status='pending'"),
            "low_quality": n(
                f"SELECT COUNT(*) FROM {self.s}.requirement "
                "WHERE quality_score IS NOT NULL AND quality_score < 0.7"),
        }

    def coverage(self, *, node_code: str = "", owner: str = ""
                 ) -> list[dict[str, Any]]:
        """Покрытие требований: связь с АП, назначенный MoC, доказательства.

        Один запрос вместо трёх обходов в Python: покрытие обязано
        считаться по тем же данным, что показывает дашборд, иначе цифры
        в отчёте и на экране разойдутся.
        """
        sql = f"""
        SELECT r.id, r.external_id, r.title, r.status, r.owner,
               r.quality_score, n.code AS node_code,
               (SELECT COUNT(*) FROM {self.s}.requirement_rule_link l
                 WHERE l.requirement_id=r.id AND l.confirmed) AS links,
               (SELECT COUNT(*) FROM {self.s}.compliance_item c
                 WHERE c.requirement_id=r.id) AS moc_count,
               (SELECT COUNT(*) FROM {self.s}.compliance_item c
                 JOIN {self.s}.evidence e ON e.compliance_id=c.id
                 WHERE c.requirement_id=r.id) AS evidence_count,
               (SELECT COUNT(*) FROM {self.s}.compliance_item c
                 WHERE c.requirement_id=r.id AND c.status='compliant')
                 AS compliant_count
        FROM {self.s}.requirement r
        LEFT JOIN {self.s}.product_node n ON n.id=r.node_id
        WHERE 1=1
        """
        args: list[Any] = []
        if node_code:
            sql += " AND n.code=%s"
            args.append(node_code)
        if owner:
            sql += " AND r.owner=%s"
            args.append(owner)
        return self._all(sql + " ORDER BY r.external_id", tuple(args))
