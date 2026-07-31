"""Мультимодальные инструменты компьютерного зрения: VLM-клиент, инспекция полок, ритейл-аудит, умный парсинг PDF."""
from __future__ import annotations

from .client import VisionClient, build_vision_tools
from .inventory import build_inventory_tools
from .pdf_vlm import PdfVlmService, build_pdf_vlm_tools
from .schemas import FacingItem, ImageRef, InventoryAuditResult

__all__ = [
    "VisionClient",
    "PdfVlmService",
    "build_vision_tools",
    "build_inventory_tools",
    "build_pdf_vlm_tools",
    "ImageRef",
    "FacingItem",
    "InventoryAuditResult",
]
