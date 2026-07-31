"""Рабочий процесс аудита выкладки и генерации отчёта (InventoryReportWorkflow).

Связывает визуальный анализ полок (Inventory AI), расчёт доли полки (SOS)
и генерацию официального документа (.docx, .xlsx, .md).
"""
from __future__ import annotations

import json
from typing import Any

from ..core import Tool, ToolError, Workspace
from ..local.document_templates import build_template_tools
from ..local.office import build_office_tools
from ..vision.inventory import InventoryService


class InventoryReportWorkflow:
    """Оркестратор ритейл-аудита: фото полки -> анализ SOS -> документ."""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.inv = InventoryService(ws=ws)
        self.office_tools = {t.name: t for t in build_office_tools(ws)}
        self.tpl_tools = {t.name: t for t in build_template_tools(ws)}

    def run_report(
        self,
        image_path: str,
        target_brand: str = "Acme",
        fmt: str = "docx",
        save_path: str = "",
        scene_json: str = "{}",
    ) -> dict[str, Any]:
        fmt = (fmt or "docx").lower()
        if fmt not in ("docx", "xlsx", "md", "both"):
            raise ToolError(
                f"Формат {fmt!r} не поддерживается (используйте: docx | xlsx | md | both)"
            )

        audit_res = self.inv.audit_shelf(
            image_path=image_path,
            target_brand=target_brand,
            scene_json=scene_json,
        )

        metrics = self.inv.calculate_metrics(
            facings_json=json.dumps(
                [f.to_dict() for f in audit_res.facings], ensure_ascii=False
            ),
            empty_slots=audit_res.empty_slots,
            target_brand=target_brand,
        )

        outputs: list[str] = []
        if fmt in ("docx", "both"):
            p_doc = save_path if save_path.endswith(".docx") else (f"{save_path}.docx" if save_path else "inventory_report.docx")
            content_lines = [
                f"## Итоги аудита полки: {image_path}",
                f"Целевой бренд: {target_brand}",
                f"Доля полки (SOS): {metrics['sos_percentage']}%",
                f"Уровень соответствия планограмме: {audit_res.compliance_score}%",
                f"Всего фейсингов: {metrics['total_facings']}",
                f"Пустых слотов: {audit_res.empty_slots}",
                "",
                "## Список замечаний",
            ]
            if audit_res.issues:
                for iss in audit_res.issues:
                    content_lines.append(f"- {iss.get('detail')} ({iss.get('severity')})")
            else:
                content_lines.append("- Замечаний не обнаружено")

            res_docx = self.office_tools["office.create_docx"].execute(
                path=p_doc,
                title=f"Отчёт по инвентаризации {target_brand}",
                content="\n".join(content_lines),
            )
            outputs.append(str(res_docx))

        if fmt in ("xlsx", "both"):
            p_xls = save_path if save_path.endswith(".xlsx") else (f"{save_path}.xlsx" if save_path else "inventory_report.xlsx")
            headers = ["Бренд", "Товар", "Полка", "Кол-во", "Ценник"]
            rows = [
                [
                    f.brand,
                    f.product,
                    f.shelf_level,
                    f.count,
                    "Да" if f.price_tag else "НЕТ",
                ]
                for f in audit_res.facings
            ]
            res_xlsx = self.office_tools["office.create_xlsx"].execute(
                path=p_xls,
                sheet_name="АудитПолки",
                headers_json=json.dumps(headers, ensure_ascii=False),
                rows_json=json.dumps(rows, ensure_ascii=False),
            )
            outputs.append(str(res_xlsx))

        if fmt == "md":
            p_md = save_path if save_path.endswith(".md") else (f"{save_path}.md" if save_path else "inventory_report.md")
            sections = [
                {
                    "title": "Сводка показателей",
                    "body": f"SOS ({target_brand}): **{metrics['sos_percentage']}%**\n"
                    f"Соответствие: **{audit_res.compliance_score}%**",
                }
            ]
            res_md = self.tpl_tools["templates.render_report"].execute(
                title=f"Аудит выкладки: {target_brand}",
                summary=f"Фото {image_path}, доля полки {metrics['sos_percentage']}%",
                sections_json=json.dumps(sections, ensure_ascii=False),
                metrics_json=json.dumps(
                    {
                        "SOS %": f"{metrics['sos_percentage']}%",
                        "Соответствие": f"{audit_res.compliance_score}%",
                        "Пустые места": str(audit_res.empty_slots),
                    },
                    ensure_ascii=False,
                ),
                path=p_md,
            )
            outputs.append(str(res_md))

        return {
            "image_path": image_path,
            "sos_percentage": metrics["sos_percentage"],
            "compliance_score": audit_res.compliance_score,
            "outputs": outputs,
        }


def build_inventory_workflow_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты рабочего процесса аудита полок и генерации отчётов."""
    wf = InventoryReportWorkflow(ws=ws)

    def create_inventory_report(
        image_path: str,
        target_brand: str = "Acme",
        fmt: str = "docx",
        save_path: str = "",
        scene_json: str = "{}",
    ) -> str:
        res = wf.run_report(
            image_path=image_path,
            target_brand=target_brand,
            fmt=fmt,
            save_path=save_path,
            scene_json=scene_json,
        )
        outputs = res.get("outputs", [])
        return (
            f"Отчёт по инвентаризации создан (SOS {res['sos_percentage']}%):\n"
            + "\n".join(f"- {o}" for o in outputs)
        )

    return [
        Tool(
            name="workflow.create_inventory_report",
            description="Провести ритейл-аудит полки по фото и создать официальный отчёт в Word (.docx), Excel (.xlsx) или Markdown (.md).",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Путь к фото полки в рабочей области",
                    },
                    "target_brand": {
                        "type": "string",
                        "description": "Целевой бренд (по умолчанию 'Acme')",
                    },
                    "fmt": {
                        "type": "string",
                        "description": "Формат отчёта: docx | xlsx | md | both",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Имя файла отчёта (опционально)",
                    },
                    "scene_json": {
                        "type": "string",
                        "description": "Опциональный JSON сцены для автономного тестирования",
                    },
                },
                "required": ["image_path"],
            },
            fn=create_inventory_report,
            skills=["workflow", "inventory", "reporting", "office", "retail", "automation"],
            attributes={
                "category": "workflow",
                "read_only": False,
                "dangerous": False,
                "resource_type": "report_document",
                "speed": "medium",
                "tags": ["workflow", "inventory", "report", "docx", "xlsx", "retail"],
            },
            example='workflow.create_inventory_report(image_path="shelf1.jpg", target_brand="Acme", fmt="docx")',
        ),
    ]
