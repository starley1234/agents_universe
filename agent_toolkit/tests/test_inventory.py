"""Тесты мультимодальных инструментов (Vision), ритейл-аудита и генерации отчётов."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.vision import (
    FacingItem,
    ImageRef,
    InventoryAuditResult,
    build_inventory_tools,
    build_vision_tools,
)
from agent_toolkit.workflows import InventoryReportWorkflow, build_inventory_workflow_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        # Создадим тестовое фото
        p_img = ws.resolve("shelf_test.jpg")
        p_img.write_bytes(b"fake-image-bytes-for-test")

        section("1. Визуальный анализ (Vision AI)")
        vis_tools = {t.name: t for t in build_vision_tools(ws)}
        check("зарегистрирован инструмент vision.analyze_image", len(vis_tools) == 1)

        res_vis = vis_tools["vision.analyze_image"].execute(
            image_path="shelf_test.jpg",
            prompt="Перечисли все бренды",
        )
        check("vision.analyze_image возвращает результат анализа", "MOCK VLM ANALYSIS" in res_vis)

        section("2. Ритейл-аудит полки (Inventory AI)")
        inv_tools = {t.name: t for t in build_inventory_tools(ws)}
        check("зарегистрировано 3 инструмента инвентаризации", len(inv_tools) == 3)

        # 1) audit_shelf
        scene_data = {
            "facings": [
                {"brand": "Acme", "product": "Juice 1L", "count": 15, "shelf_level": 1, "price_tag": True},
                {"brand": "Acme", "product": "Juice 2L", "count": 5, "shelf_level": 1, "price_tag": False},
                {"brand": "Other", "product": "Water 1L", "count": 20, "shelf_level": 2, "price_tag": True},
            ],
            "empty_slots": 4,
            "shelf_levels": 3,
        }
        res_audit = inv_tools["inventory.audit_shelf"].execute(
            image_path="shelf_test.jpg",
            target_brand="Acme",
            scene_json=json.dumps(scene_data),
        )
        check("audit_shelf считает SOS % (20/40 = 50.0%)", "SOS бренда Acme): 50.0%" in res_audit)
        check("audit_shelf показывает пустые слоты (OOS: 4)", "Пустых мест (out-of-stock): 4" in res_audit)

        # 2) check_price_tags
        res_tags = inv_tools["inventory.check_price_tags"].execute(
            image_path="shelf_test.jpg",
            scene_json=json.dumps(scene_data),
        )
        check("check_price_tags находит товар без ценника", "без ценника" in res_tags and "Juice 2L" in res_tags)

        # 3) calculate_metrics
        res_metrics_json = inv_tools["inventory.calculate_metrics"].execute(
            facings_json=json.dumps(scene_data["facings"]),
            empty_slots=4,
            target_brand="Acme",
        )
        metrics_data = json.loads(res_metrics_json)
        check("calculate_metrics считает точные показатели", metrics_data["sos_percentage"] == 50.0)
        check("calculate_metrics считает заполненность полки", metrics_data["occupancy_rate_pct"] == 90.91)

        section("3. Рабочий процесс отчёта инвентаризации (InventoryReportWorkflow)")
        wf = InventoryReportWorkflow(ws=ws)
        res_wf = wf.run_report(
            image_path="shelf_test.jpg",
            target_brand="Acme",
            fmt="both",
            save_path="audit_acme",
            scene_json=json.dumps(scene_data),
        )
        check("Workflow создаёт оба файла (.docx и .xlsx)", len(res_wf["outputs"]) == 2)
        check("Файл .docx существует", ws.exists("audit_acme.docx"))
        check("Файл .xlsx существует", ws.exists("audit_acme.xlsx"))

        wf_tools = {t.name: t for t in build_inventory_workflow_tools(ws)}
        res_tool_wf = wf_tools["workflow.create_inventory_report"].execute(
            image_path="shelf_test.jpg",
            target_brand="Acme",
            fmt="md",
            save_path="report.md",
            scene_json=json.dumps(scene_data),
        )
        check("Инструмент workflow.create_inventory_report генерирует отчёт", "SOS 50.0%" in res_tool_wf)
        check("MD-отчёт создан", ws.exists("report.md"))

    return summary("Тесты мультимодальных и ритейл-инструментов")


def test_inventory_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
