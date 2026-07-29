"""Агент-снабженец (procurement agent) — сквозной AI-сценарий ТЗ §3.4/§3.7.

Демонстрирует ВСЮ обязательную агентскую архитектуру из ТЗ на одном
конкретном контуре, вместо разбавления по многим недоделанным контурам:

  - Explainability     — каждое предложение несёт `explanation` (текст
                          обоснования) и `sources` (на основании каких
                          именно данных принято решение — остаток, цены
                          поставщиков, надёжность), можно проверить.
  - Confirmation gates  — режимы автономности per-операция (см.
                          AutonomyMode); критичные предложения ждут
                          решения человека, прежде чем стать документом.
  - Guardrails          — сумма выше cfg.procurement_max_auto_amount
                          НИКОГДА не проводится автоматически, даже если
                          настроен full_auto — принудительно понижается
                          до draft. Это проверяется в коде, а не только
                          в UI (не обойти сменой конфигурации).
  - Audit trail         — КАЖДОЕ решение (агента и человека) пишется в
                          audit_log через Store.log_audit — неизменяемый
                          журнал, независимый от статуса самого документа.
  - Rollback            — для full_auto решений, которые оказались
                          неверными, отмена возможна, пока заказ не
                          отправлен в 1С (см. rollback_proposal).

Алгоритм анализа (детерминированный, без LLM — тот же принцип "критичные
операции только через типизированные инструменты", что в ТЗ §6.2):
  1. Найти номенклатуру с доступным остатком ниже страхового запаса
     (Store.low_stock_nomenclature).
  2. Для каждой — определить дефицит (min_stock - available).
  3. Выбрать поставщика: минимальная цена среди поставщиков с
     reliability_score >= min_reliability (по умолчанию 0.5) — не самый
     дешёвый ЛЮБОЙ ценой, а самый дешёвый среди надёжных.
  4. Сформировать предложение: создать заявку на закупку + (если
     разрешено автономностью) сразу заказ поставщику.

Естественно-языковое объяснение опционально дополняется LLM
(app/agents/narrator.py) — но базовый текст объяснения ВСЕГДА
детерминированный и не зависит от доступности LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..config import Config
from ..db.store import Store


class AutonomyMode(str, Enum):
    SUGGEST = "suggest"                 # агент только предлагает
    DRAFT = "draft"                     # готовит черновик, человек утверждает
    AUTO_WITH_REVIEW = "auto_with_review"  # выполняет, помечается для контроля
    FULL_AUTO = "full_auto"             # полностью автоматически


VALID_AUTONOMY_MODES = {m.value for m in AutonomyMode}


class ProcurementAgentError(RuntimeError):
    """Ожидаемая ошибка агента: не найдены данные, нарушение guardrail и т.п."""


@dataclass
class ProcurementDecision:
    proposal_id: int
    nomenclature_sku: str
    deficit: float
    chosen_supplier: str
    unit_price: float
    total_amount: float
    autonomy_mode: str
    guardrail_downgraded: bool
    explanation: str
    status: str                # pending | auto_executed
    purchase_order_id: int | None = None


class ProcurementAgent:
    """Агент-снабженец: анализ дефицита -> предложение закупки."""

    slug = "procurement"

    def __init__(self, cfg: Config, store: Store,
                min_reliability: float = 0.5) -> None:
        self.cfg = cfg
        self.store = store
        self.min_reliability = min_reliability

    # --------------------------------------------------------- анализ
    def find_deficits(self) -> list[dict[str, Any]]:
        """Номенклатура с дефицитом (доступный остаток < страхового
        запаса) — детерминированный SQL-запрос, без участия LLM."""
        return self.store.low_stock_nomenclature()

    def choose_supplier(self, nomenclature_id: int) -> dict[str, Any] | None:
        """Самый дешёвый поставщик СРЕДИ ДОСТАТОЧНО НАДЁЖНЫХ — намеренно
        не просто минимальная цена: агент не должен предлагать самого
        дешёвого, но ненадёжного поставщика без объяснения компромисса."""
        candidates = self.store.supplier_prices_for(nomenclature_id)
        reliable = [c for c in candidates if c["reliability_score"] >= self.min_reliability]
        pool = reliable or candidates  # если совсем никого надёжного нет — берём хоть кого-то
        if not pool:
            return None
        return pool[0]

    # --------------------------------------------------- guardrail-логика
    def _effective_autonomy(self, total_amount: float,
                            requested_mode: str) -> tuple[str, bool]:
        """Применяет guardrail по сумме. Возвращает (реальный_режим,
        был_ли_понижен). Guardrail НЕЛЬЗЯ обойти настройкой конфигурации —
        проверка происходит здесь, а не полагается на то, что вызывающий
        код передаст правильный режим."""
        if requested_mode not in VALID_AUTONOMY_MODES:
            raise ProcurementAgentError(
                f"Неизвестный режим автономности {requested_mode!r}. "
                f"Допустимы: {', '.join(sorted(VALID_AUTONOMY_MODES))}")
        if total_amount > self.cfg.procurement_max_auto_amount and requested_mode in (
                AutonomyMode.AUTO_WITH_REVIEW.value, AutonomyMode.FULL_AUTO.value):
            return AutonomyMode.DRAFT.value, True
        return requested_mode, False

    # ------------------------------------------------------- предложение
    def propose_for_nomenclature(self, nomenclature_id: int,
                                 autonomy_mode: str | None = None
                                 ) -> ProcurementDecision:
        nom = self.store.get_nomenclature(nomenclature_id)
        if not nom:
            raise ProcurementAgentError(f"Номенклатура #{nomenclature_id} не найдена")
        stock = self.store.stock_for(nomenclature_id)
        deficit = max(0.0, nom["min_stock"] - stock["available"])
        if deficit <= 0:
            raise ProcurementAgentError(
                f"У {nom['sku']} нет дефицита (доступно {stock['available']}, "
                f"страховой запас {nom['min_stock']}) — предложение не требуется")

        supplier = self.choose_supplier(nomenclature_id)
        if not supplier:
            raise ProcurementAgentError(
                f"Для {nom['sku']} нет ни одного известного поставщика "
                "с ценой — заведите цену через set_supplier_price перед тем, "
                "как просить агента предложить закупку")

        requested_mode = autonomy_mode or self.cfg.procurement_default_autonomy
        total_amount = deficit * supplier["price"]
        effective_mode, downgraded = self._effective_autonomy(total_amount, requested_mode)

        explanation = self._explain(nom, stock, deficit, supplier, effective_mode,
                                    downgraded, requested_mode)
        sources = [
            {"type": "stock_balance", "nomenclature_id": nomenclature_id,
             "available": stock["available"], "min_stock": nom["min_stock"]},
            {"type": "supplier_price", "counterparty_id": supplier["counterparty_id"],
             "counterparty_name": supplier["counterparty_name"],
             "price": supplier["price"],
             "reliability_score": supplier["reliability_score"]},
        ]

        payload = {
            "nomenclature_id": nomenclature_id, "sku": nom["sku"],
            "quantity": deficit, "counterparty_id": supplier["counterparty_id"],
            "unit_price": supplier["price"], "total_amount": total_amount,
            "requested_autonomy_mode": requested_mode,
        }
        proposal_id = self.store.create_proposal(
            self.slug, "create_purchase_order", payload, explanation,
            effective_mode, sources=sources)
        self.store.log_audit(f"agent:{self.slug}", "propose", "agent_proposal",
                             proposal_id, {"kind": "create_purchase_order",
                                          "autonomy_mode": effective_mode,
                                          "guardrail_downgraded": downgraded})

        status = "pending"
        po_id = None
        if effective_mode in (AutonomyMode.AUTO_WITH_REVIEW.value,
                              AutonomyMode.FULL_AUTO.value):
            po_id = self._execute_proposal(proposal_id, payload,
                                           actor=f"agent:{self.slug}")
            status = "auto_executed"

        return ProcurementDecision(
            proposal_id=proposal_id, nomenclature_sku=nom["sku"], deficit=deficit,
            chosen_supplier=supplier["counterparty_name"],
            unit_price=supplier["price"], total_amount=total_amount,
            autonomy_mode=effective_mode, guardrail_downgraded=downgraded,
            explanation=explanation, status=status, purchase_order_id=po_id)

    def run_deficit_scan(self, autonomy_mode: str | None = None) -> list[ProcurementDecision]:
        """Проходит по ВСЕЙ номенклатуре с дефицитом и формирует
        предложения — типичный вызов агента по расписанию/по кнопке."""
        decisions = []
        for item in self.find_deficits():
            try:
                decisions.append(self.propose_for_nomenclature(item["id"], autonomy_mode))
            except ProcurementAgentError:
                continue  # нет поставщика и т.п. — пропускаем, не роняем скан
        return decisions

    # ------------------------------------------------------------ explain
    @staticmethod
    def _explain(nom: dict, stock: dict, deficit: float, supplier: dict,
                effective_mode: str, downgraded: bool, requested_mode: str) -> str:
        lines = [
            f"Остаток {nom['sku']} ({nom['name']}): доступно "
            f"{stock['available']:.2f} {nom['unit']}, страховой запас "
            f"{nom['min_stock']:.2f} {nom['unit']} — дефицит "
            f"{deficit:.2f} {nom['unit']}.",
            f"Предлагаемый поставщик: {supplier['counterparty_name']} "
            f"(цена {supplier['price']:.2f} ₽, надёжность "
            f"{supplier['reliability_score']:.2f}) — минимальная цена среди "
            "поставщиков с достаточной надёжностью.",
        ]
        if downgraded:
            lines.append(
                f"Режим автономности понижен с {requested_mode!r} до "
                f"{effective_mode!r}: сумма заказа превышает guardrail-лимит "
                "на автоматическое исполнение — требуется подтверждение человека.")
        return " ".join(lines)

    # --------------------------------------------------------- решения
    def _execute_proposal(self, proposal_id: int, payload: dict[str, Any],
                          actor: str) -> int:
        po_id = self.store.create_purchase_order(
            payload["counterparty_id"],
            [{"nomenclature_id": payload["nomenclature_id"],
              "quantity": payload["quantity"], "price": payload["unit_price"]}],
            created_by=actor, status="approved")
        self.store.set_proposal_decision(
            proposal_id, "auto_executed", actor,
            result_document_type="purchase_order", result_document_id=po_id)
        # Две записи аудита с одним и тем же действием, но разными
        # entity_type/entity_id — чтобы полная история была видна ОБОИМ
        # путём просмотра: со стороны предложения (agent_proposal) и со
        # стороны созданного документа (purchase_order).
        self.store.log_audit(actor, "auto_execute", "purchase_order", po_id,
                             {"proposal_id": proposal_id})
        self.store.log_audit(actor, "auto_execute", "agent_proposal", proposal_id,
                             {"purchase_order_id": po_id})
        return po_id

    def approve_proposal(self, proposal_id: int, actor: str) -> int:
        """Человек утверждает предложение (Draft/Suggest -> документ)."""
        proposal = self.store.get_proposal(proposal_id)
        if not proposal:
            raise ProcurementAgentError(f"Предложение #{proposal_id} не найдено")
        if proposal["status"] != "pending":
            raise ProcurementAgentError(
                f"Предложение #{proposal_id} уже в статусе "
                f"{proposal['status']!r}, повторное утверждение невозможно")
        payload = proposal["payload"]
        po_id = self.store.create_purchase_order(
            payload["counterparty_id"],
            [{"nomenclature_id": payload["nomenclature_id"],
              "quantity": payload["quantity"], "price": payload["unit_price"]}],
            created_by=actor, status="approved")
        self.store.set_proposal_decision(
            proposal_id, "approved", actor,
            result_document_type="purchase_order", result_document_id=po_id)
        self.store.log_audit(actor, "approve", "agent_proposal", proposal_id,
                             {"result_purchase_order_id": po_id})
        self.store.log_audit(actor, "approve", "purchase_order", po_id,
                             {"proposal_id": proposal_id})
        return po_id

    def reject_proposal(self, proposal_id: int, actor: str, reason: str = "") -> None:
        proposal = self.store.get_proposal(proposal_id)
        if not proposal:
            raise ProcurementAgentError(f"Предложение #{proposal_id} не найдено")
        if proposal["status"] != "pending":
            raise ProcurementAgentError(
                f"Предложение #{proposal_id} уже в статусе "
                f"{proposal['status']!r}, повторное решение невозможно")
        self.store.set_proposal_decision(proposal_id, "rejected", actor)
        self.store.log_audit(actor, "reject", "agent_proposal", proposal_id,
                             {"reason": reason})

    def rollback_proposal(self, proposal_id: int, actor: str, reason: str = "") -> None:
        """Отменяет РЕЗУЛЬТАТ полностью автоматического решения — работает
        только пока созданный заказ ещё не отправлен в 1С (onec_uuid
        пуст): после отправки во внешнюю систему откат теряет смысл
        (документ уже существует в 1С), это ограничение сознательное."""
        proposal = self.store.get_proposal(proposal_id)
        if not proposal:
            raise ProcurementAgentError(f"Предложение #{proposal_id} не найдено")
        if proposal["status"] != "auto_executed":
            raise ProcurementAgentError(
                f"Откатить можно только auto_executed предложение, "
                f"текущий статус: {proposal['status']!r}")
        if proposal["result_document_type"] != "purchase_order":
            raise ProcurementAgentError("Неизвестный тип результата для отката")
        po_id = proposal["result_document_id"]
        po = self.store.get_purchase_order(po_id)
        if po and po["onec_uuid"]:
            raise ProcurementAgentError(
                f"Заказ #{po_id} уже отправлен в 1С ({po['onec_uuid']}) — "
                "откат невозможен, требуется ручная корректировка в 1С")
        if po:
            self.store.delete_purchase_order(po_id)
        self.store.set_proposal_decision(proposal_id, "rolled_back", actor)
        self.store.log_audit(actor, "rollback", "agent_proposal", proposal_id,
                             {"reason": reason, "removed_purchase_order_id": po_id})
