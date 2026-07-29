"""Тесты app.agents.procurement.ProcurementAgent: сквозной AI-сценарий,
демонстрирующий ВСЮ обязательную агентскую архитектуру из ТЗ (§3.7):
explainability, confirmation gates, guardrails, audit trail, rollback,
настраиваемые режимы автономности.

Реальный embedded PostgreSQL (pgserver) — без моков.
"""
from __future__ import annotations

import re
import sys
import tempfile
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
        _tmp = tempfile.mkdtemp(prefix="erp_agent_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


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


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_procurement_agent: тесты пропущены — {SKIP_REASON}")
        return 0

    from app.agents.procurement import ProcurementAgent, ProcurementAgentError
    from app.config import Config
    from app.db.store import Store

    def _fresh_setup(max_auto=10_000.0):
        cfg = Config(db_dsn=_fresh_dsn(), procurement_max_auto_amount=max_auto)
        st = Store(cfg.db_dsn)
        agent = ProcurementAgent(cfg, st)
        return cfg, st, agent

    section("propose_for_nomenclature: без дефицита -> ошибка")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-001", "Сталь", unit="лист", min_stock=10)
    st.set_stock(nid, 20)  # выше страхового запаса
    try:
        agent.propose_for_nomenclature(nid)
        check("предложение без дефицита отклонено", False)
    except ProcurementAgentError as exc:
        check("предложение без дефицита отклонено", True)
        check("сообщение объясняет причину", "дефицит" in str(exc).lower())
    st.close()

    section("propose_for_nomenclature: дефицит без поставщика -> ошибка")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-002", "Медь", unit="кг", min_stock=50)
    st.set_stock(nid, 10)
    try:
        agent.propose_for_nomenclature(nid)
        check("предложение без известного поставщика отклонено", False)
    except ProcurementAgentError as exc:
        check("предложение без известного поставщика отклонено", True)
        check("сообщение упоминает отсутствие поставщика", "поставщик" in str(exc).lower())
    st.close()

    section("Explainability: выбор поставщика ПО НАДЁЖНОСТИ, а не только по цене")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-003", "Алюминий", unit="лист", min_stock=100)
    cid_reliable = st.upsert_counterparty("Надёжный", reliability_score=0.9)
    cid_cheap = st.upsert_counterparty("Дешёвый но ненадёжный", reliability_score=0.2)
    st.set_supplier_price(nid, cid_reliable, 100.0)
    st.set_supplier_price(nid, cid_cheap, 50.0)
    st.set_stock(nid, 10)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="suggest")
    check("выбран надёжный, а не дешёвый поставщик", decision.chosen_supplier == "Надёжный")
    check("explanation содержит остаток/дефицит", "дефицит" in decision.explanation.lower())
    check("explanation называет выбранного поставщика", "Надёжный" in decision.explanation)
    check("sources в БД содержат stock_balance и supplier_price",
         {s["type"] for s in st.get_proposal(decision.proposal_id)["sources"]}
         == {"stock_balance", "supplier_price"})
    st.close()

    section("Explainability: если надёжных нет вообще — берём хоть кого-то")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-004", "Титан", unit="лист", min_stock=10)
    cid_only = st.upsert_counterparty("Единственный", reliability_score=0.1)
    st.set_supplier_price(nid, cid_only, 500.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="suggest")
    check("выбран единственный доступный, несмотря на низкую надёжность",
         decision.chosen_supplier == "Единственный")
    st.close()

    section("Autonomy mode: SUGGEST — только предложение, без документа")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-005", "Прокат", unit="лист", min_stock=50)
    cid = st.upsert_counterparty("Поставщик", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="suggest")
    check("статус pending", decision.status == "pending")
    check("документ НЕ создан", decision.purchase_order_id is None)
    check("реально нет ни одного заказа в базе", st.list_purchase_orders() == [])
    st.close()

    section("Autonomy mode: FULL_AUTO ниже guardrail-лимита — реально исполняется")
    cfg, st, agent = _fresh_setup(max_auto=10_000.0)
    nid = st.upsert_nomenclature("MTL-006", "Лист", unit="лист", min_stock=10)
    cid = st.upsert_counterparty("Поставщик", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 50.0)  # дефицит 5 * 50 = 250 руб, ниже лимита
    st.set_stock(nid, 5)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="full_auto")
    check("режим НЕ понижен (сумма ниже лимита)", not decision.guardrail_downgraded)
    check("статус auto_executed", decision.status == "auto_executed")
    check("документ реально создан", decision.purchase_order_id is not None)
    po = st.get_purchase_order(decision.purchase_order_id)
    check("документ реально в базе со статусом approved", po["status"] == "approved")
    st.close()

    section("Guardrail: FULL_AUTO ВЫШЕ лимита ПРИНУДИТЕЛЬНО понижается до DRAFT")
    cfg, st, agent = _fresh_setup(max_auto=100.0)  # низкий лимит
    nid = st.upsert_nomenclature("MTL-007", "Дорогой материал", unit="лист", min_stock=100)
    cid = st.upsert_counterparty("Поставщик", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 1000.0)  # дефицит большой -> сумма выше лимита
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="full_auto")
    check("режим ПОНИЖЕН guardrail'ом", decision.guardrail_downgraded)
    check("эффективный режим — draft", decision.autonomy_mode == "draft")
    check("статус pending, документ НЕ создан автоматически",
         decision.status == "pending" and decision.purchase_order_id is None)
    check("explanation объясняет понижение", "лимит" in decision.explanation.lower())
    check("guardrail нельзя обойти сменой autonomy_mode на auto_with_review тоже",
         agent.propose_for_nomenclature(nid, autonomy_mode="auto_with_review")
         .guardrail_downgraded is True)
    st.close()

    section("Неизвестный режим автономности -> явная ошибка")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-008", "Х", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    try:
        agent.propose_for_nomenclature(nid, autonomy_mode="ultra_mega_auto")
        check("неизвестный режим отклонён", False)
    except ProcurementAgentError as exc:
        check("неизвестный режим отклонён", True)
        check("сообщение перечисляет допустимые режимы", "suggest" in str(exc))
    st.close()

    section("Confirmation gate: approve переводит DRAFT в реальный документ")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-009", "Y", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П2", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="draft")
    check("до approve документа нет", st.list_purchase_orders() == [])
    po_id = agent.approve_proposal(decision.proposal_id, actor="human:ivanov")
    check("после approve документ создан", st.get_purchase_order(po_id) is not None)
    check("создатель документа — человек, принявший решение",
         st.get_purchase_order(po_id)["created_by"] == "human:ivanov")
    check("статус предложения — approved",
         st.get_proposal(decision.proposal_id)["status"] == "approved")
    try:
        agent.approve_proposal(decision.proposal_id, actor="human:ivanov")
        check("повторный approve отклонён", False)
    except ProcurementAgentError:
        check("повторный approve отклонён", True)
    st.close()

    section("Confirmation gate: reject НЕ создаёт документ")
    cfg, st, agent = _fresh_setup()
    nid = st.upsert_nomenclature("MTL-010", "Z", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П3", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="draft")
    agent.reject_proposal(decision.proposal_id, actor="human:petrov", reason="не нужно")
    check("статус rejected", st.get_proposal(decision.proposal_id)["status"] == "rejected")
    check("документ не создан", st.list_purchase_orders() == [])
    try:
        agent.reject_proposal(decision.proposal_id, actor="human:petrov")
        check("повторный reject отклонён", False)
    except ProcurementAgentError:
        check("повторный reject отклонён", True)
    st.close()

    section("Rollback: отмена auto_executed решения удаляет документ")
    cfg, st, agent = _fresh_setup(max_auto=1_000_000.0)
    nid = st.upsert_nomenclature("MTL-011", "W", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П4", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="full_auto")
    check("документ реально создан перед откатом",
         st.get_purchase_order(decision.purchase_order_id) is not None)
    agent.rollback_proposal(decision.proposal_id, actor="human:ivanov", reason="ошибка агента")
    check("после отката документ удалён",
         st.get_purchase_order(decision.purchase_order_id) is None)
    check("статус предложения — rolled_back",
         st.get_proposal(decision.proposal_id)["status"] == "rolled_back")
    try:
        agent.rollback_proposal(decision.proposal_id, actor="human:ivanov")
        check("повторный rollback отклонён", False)
    except ProcurementAgentError:
        check("повторный rollback отклонён", True)
    st.close()

    section("Rollback: НЕВОЗМОЖЕН после отправки заказа в 1С")
    cfg, st, agent = _fresh_setup(max_auto=1_000_000.0)
    nid = st.upsert_nomenclature("MTL-012", "V", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П5", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="full_auto")
    cur = st.conn.cursor()
    cur.execute("UPDATE purchase_order SET onec_uuid=%s WHERE id=%s",
               ("1c-fake-guid", decision.purchase_order_id))
    try:
        agent.rollback_proposal(decision.proposal_id, actor="human:ivanov")
        check("rollback после синхронизации с 1С отклонён", False)
    except ProcurementAgentError as exc:
        check("rollback после синхронизации с 1С отклонён", True)
        check("сообщение объясняет причину (уже в 1С)", "1С" in str(exc))
    check("документ НЕ удалён (защита от рассинхронизации с 1С)",
         st.get_purchase_order(decision.purchase_order_id) is not None)
    st.close()

    section("Audit trail: КАЖДОЕ решение агента и человека зафиксировано")
    cfg, st, agent = _fresh_setup(max_auto=1_000_000.0)
    nid = st.upsert_nomenclature("MTL-013", "U", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П6", reliability_score=0.9)
    st.set_supplier_price(nid, cid, 10.0)
    st.set_stock(nid, 0)
    decision = agent.propose_for_nomenclature(nid, autonomy_mode="draft")
    trail_after_propose = st.audit_trail_for("agent_proposal", decision.proposal_id)
    check("после propose в аудите есть запись propose",
         any(a["action"] == "propose" for a in trail_after_propose))
    agent.approve_proposal(decision.proposal_id, actor="human:sidorov")
    trail_after_approve = st.audit_trail_for("agent_proposal", decision.proposal_id)
    check("после approve в аудите добавлена запись approve",
         any(a["action"] == "approve" for a in trail_after_approve))
    check("actor записи approve — человек, а не агент",
         any(a["action"] == "approve" and a["actor"] == "human:sidorov"
            for a in trail_after_approve))
    st.close()

    section("run_deficit_scan: пропускает позиции без поставщика, не падает")
    cfg, st, agent = _fresh_setup()
    nid_ok = st.upsert_nomenclature("MTL-014", "С поставщиком", unit="шт", min_stock=10)
    nid_no_supplier = st.upsert_nomenclature("MTL-015", "Без поставщика", unit="шт", min_stock=10)
    cid = st.upsert_counterparty("П7", reliability_score=0.9)
    st.set_supplier_price(nid_ok, cid, 10.0)
    st.set_stock(nid_ok, 0)
    st.set_stock(nid_no_supplier, 0)
    decisions = agent.run_deficit_scan(autonomy_mode="suggest")
    check("скан не упал на позиции без поставщика", True)
    check("предложение создано только для позиции с поставщиком",
         len(decisions) == 1 and decisions[0].nomenclature_sku == "MTL-014")
    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
