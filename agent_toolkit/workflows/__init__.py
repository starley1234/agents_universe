"""Комплексные рабочие процессы агентов: аудит веб-сайтов и отчёты по инвентаризации."""
from __future__ import annotations

from .inventory_report import InventoryReportWorkflow, build_inventory_workflow_tools
from .website import WebsiteAuditWorkflow, build_website_workflow_tools

__all__ = [
    "WebsiteAuditWorkflow",
    "build_website_workflow_tools",
    "InventoryReportWorkflow",
    "build_inventory_workflow_tools",
]
