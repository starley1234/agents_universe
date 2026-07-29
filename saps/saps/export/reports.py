"""Сборка «Протокола соответствия» и выгрузок (ТЗ п.3.4, п.6.3).

ПРОТОКОЛ СООТВЕТСТВИЯ — главный выходной документ САПС: по каждому
требованию указано, какому пункту авиационных правил оно соответствует,
каким методом подтверждается и какими документами это доказано.

ЧЕСТНОСТЬ ПРОТОКОЛА — ключевое свойство. Документ, который печатает
«соответствует» там, где доказательства нет, опаснее отсутствия
документа: он создаёт ложную уверенность и всплывает на проверке
регулятора. Поэтому:

  * в протокол идут ТОЛЬКО подтверждённые человеком связи с пунктами АП
    (confirmed=true); предложения агентов туда не попадают;
  * строка с назначенным MoC, но без приложенного доказательства,
    печатается как «не подтверждено» с явной пометкой;
  * итоговая сводка отдельно показывает число дыр, а не только процент
    готовности;
  * если дыры есть, в шапке документа печатается предупреждение, что
    протокол не является доказательством соответствия.

Форматы: Word (для подписи и совещаний) и Excel (для работы со срезом
данных). Оба собираются одним и тем же кодом сбора данных — цифры в
разных форматах разойтись не могут.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..agents.gap import GapAgent
from ..db.schema import MOC_CODES
from ..db.store import Store
from .writers import timestamp, write_docx, write_xlsx

#: Как печатать статус пункта доказательства в протоколе.
STATUS_LABELS = {
    "planned": "запланировано",
    "in_progress": "выполняется",
    "submitted": "представлено",
    "compliant": "соответствует",
    "non_compliant": "НЕ соответствует",
    "not_applicable": "неприменимо",
}


def collect_compliance(store: Store, *, node_code: str = "", owner: str = ""
                       ) -> dict[str, Any]:
    """Собрать данные протокола. Единый источник для всех форматов."""
    requirements = store.list_requirements(node_code=node_code, owner=owner,
                                           limit=10000)
    rows: list[dict[str, Any]] = []
    gaps_total = 0

    for req in requirements:
        req_id = int(req["id"])
        links = [l for l in store.requirement_links(req_id) if l["confirmed"]]
        unconfirmed = [l for l in store.requirement_links(req_id)
                       if not l["confirmed"]]
        items = store.compliance_items(req_id)

        clause_text = ", ".join(f"{l['ruleset']} {l['clause']}" for l in links)
        moc_parts: list[str] = []
        evidence_parts: list[str] = []
        verdict = "не подтверждено"
        row_gaps: list[str] = []

        if not links:
            row_gaps.append("нет подтверждённой связи с пунктом АП")
        for item in items:
            label = STATUS_LABELS.get(item["status"], item["status"])
            moc_parts.append(f"{item['moc']} ({label})")
            for ev in item["evidence"]:
                evidence_parts.append(
                    f"{ev['title'] or ev['kind']}"
                    + (f" — {ev['uri']}" if ev["uri"] else ""))
        if not items:
            row_gaps.append("не назначен метод подтверждения (MoC)")
        elif not evidence_parts:
            row_gaps.append("нет доказательных документов")

        # Вердикт ставится ТОЛЬКО при совпадении трёх условий.
        if links and items and evidence_parts and all(
                i["status"] in ("compliant", "not_applicable") for i in items):
            verdict = "соответствует"
        elif any(i["status"] == "non_compliant" for i in items):
            verdict = "НЕ соответствует"

        gaps_total += len(row_gaps)
        rows.append({
            "external_id": req["external_id"],
            "title": req["title"] or "",
            "text": req["text"],
            "status": req["status"],
            "owner": req["owner"] or "",
            "node": req.get("node_code") or "",
            "clauses": clause_text or "—",
            "unconfirmed_clauses": ", ".join(
                f"{l['ruleset']} {l['clause']}" for l in unconfirmed),
            "moc": "; ".join(moc_parts) or "—",
            "evidence": "; ".join(evidence_parts) or "—",
            "quality": (round(float(req["quality_score"]), 2)
                        if req["quality_score"] is not None else None),
            "verdict": verdict,
            "gaps": row_gaps,
        })

    compliant = sum(1 for r in rows if r["verdict"] == "соответствует")
    non_compliant = sum(1 for r in rows if r["verdict"] == "НЕ соответствует")
    return {
        "rows": rows,
        "total": len(rows),
        "compliant": compliant,
        "non_compliant": non_compliant,
        "unproven": len(rows) - compliant - non_compliant,
        "gaps_total": gaps_total,
        "node_code": node_code,
        "owner": owner,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def compliance_docx(store: Store, cfg, path: str | Path, *,
                    node_code: str = "", owner: str = "") -> Path:
    """Протокол соответствия в Word."""
    data = collect_compliance(store, node_code=node_code, owner=owner)
    health = GapAgent(cfg, store).health(node_code=node_code, owner=owner)

    scope = []
    if node_code:
        scope.append(f"узел: {node_code}")
    if owner:
        scope.append(f"ответственный: {owner}")
    scope_text = ", ".join(scope) or "все требования базы"

    blocks: list[dict[str, Any]] = [
        {"type": "paragraph",
         "text": f"Сформирован САПС {data['generated_at']}. Область: {scope_text}."},
        {"type": "heading", "text": "1. Сводка", "level": 1},
        {"type": "table",
         "header": ["Показатель", "Значение"],
         "rows": [
             ["Всего требований", data["total"]],
             ["Соответствует (подтверждено документами)", data["compliant"]],
             ["Не соответствует", data["non_compliant"]],
             ["Не подтверждено", data["unproven"]],
             ["Готовность (индикатор здоровья)",
              f"{health['health'] * 100:.0f}% — {health['status']}"],
             ["Выявлено пробелов", data["gaps_total"]],
         ]},
    ]

    if data["gaps_total"] or data["unproven"]:
        # Предупреждение в начале документа, а не в примечании мелким
        # шрифтом: читатель протокола должен увидеть его первым.
        blocks.append({
            "type": "paragraph", "bold": True,
            "text": ("ВНИМАНИЕ. Протокол содержит незакрытые позиции "
                     f"({data['unproven']} требований без подтверждения, "
                     f"{data['gaps_total']} пробелов) и НЕ ЯВЛЯЕТСЯ "
                     "доказательством соответствия. Документ отражает "
                     "текущее состояние подготовки доказательной "
                     "документации.")})

    blocks.append({"type": "heading", "text": "2. Матрица соответствия",
                   "level": 1})
    blocks.append({
        "type": "table",
        "header": ["Требование", "Формулировка", "Пункт АП", "Метод (MoC)",
                   "Доказательства", "Вывод"],
        "rows": [[r["external_id"], _clip(r["text"], 400), r["clauses"],
                  r["moc"], _clip(r["evidence"], 200), r["verdict"]]
                 for r in data["rows"]],
    })

    gap_rows = [[r["external_id"], "; ".join(r["gaps"])]
                for r in data["rows"] if r["gaps"]]
    if gap_rows:
        blocks.append({"type": "heading",
                       "text": "3. Пробелы, требующие закрытия", "level": 1})
        blocks.append({"type": "table",
                       "header": ["Требование", "Что отсутствует"],
                       "rows": gap_rows})

    blocks.append({"type": "heading", "text": "Условные обозначения",
                   "level": 1})
    blocks.append({"type": "table", "header": ["Код", "Метод подтверждения"],
                   "rows": [[k, v] for k, v in sorted(MOC_CODES.items())]})

    store.log("system", "export", detail=f"протокол соответствия: {path}",
              data={"node_code": node_code, "owner": owner,
                    "rows": data["total"]})
    return write_docx(path, blocks, title="Протокол соответствия требованиям")


def compliance_xlsx(store: Store, path: str | Path, *, node_code: str = "",
                    owner: str = "") -> Path:
    """Тот же протокол в Excel — для работы со срезом данных."""
    data = collect_compliance(store, node_code=node_code, owner=owner)
    sheets = {
        "Матрица": {
            "header": ["Требование", "Заголовок", "Формулировка", "Статус",
                       "Ответственный", "Узел", "Пункт АП", "Метод (MoC)",
                       "Доказательства", "Качество", "Вывод"],
            "rows": [[r["external_id"], r["title"], r["text"], r["status"],
                      r["owner"], r["node"], r["clauses"], r["moc"],
                      r["evidence"],
                      r["quality"] if r["quality"] is not None else "",
                      r["verdict"]] for r in data["rows"]],
        },
        "Пробелы": {
            "header": ["Требование", "Что отсутствует"],
            "rows": [[r["external_id"], g]
                     for r in data["rows"] for g in r["gaps"]],
        },
        "Сводка": {
            "header": ["Показатель", "Значение"],
            "rows": [["Сформирован", data["generated_at"]],
                     ["Всего требований", data["total"]],
                     ["Соответствует", data["compliant"]],
                     ["Не соответствует", data["non_compliant"]],
                     ["Не подтверждено", data["unproven"]],
                     ["Пробелов", data["gaps_total"]]],
        },
    }
    store.log("system", "export", detail=f"выгрузка протокола (xlsx): {path}")
    return write_xlsx(path, sheets)


def requirements_xlsx(store: Store, path: str | Path, *, node_code: str = "",
                      owner: str = "", status: str = "") -> Path:
    """Срез требований в Excel (ТЗ п.6.3: «выгрузить для совещаний»)."""
    rows = store.list_requirements(node_code=node_code, owner=owner,
                                   status=status, limit=10000)
    sheets = {
        "Требования": {
            "header": ["Идентификатор", "Заголовок", "Требование", "Статус",
                       "Ответственный", "Узел", "Качество", "Обновлено"],
            "rows": [[r["external_id"], r["title"] or "", r["text"],
                      r["status"], r["owner"] or "", r.get("node_code") or "",
                      (round(float(r["quality_score"]), 2)
                       if r["quality_score"] is not None else ""),
                      str(r["updated_at"])[:16]] for r in rows],
        }
    }
    store.log("system", "export", detail=f"выгрузка требований: {path}",
              data={"rows": len(rows)})
    return write_xlsx(path, sheets)


def export_path(workdir: str | Path, prefix: str, suffix: str) -> Path:
    """Имя файла выгрузки с отметкой времени."""
    return Path(workdir) / f"{prefix}_{timestamp()}.{suffix}"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"
