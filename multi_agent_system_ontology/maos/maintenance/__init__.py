"""Фоновое обслуживание: дистилляция, дедупликация, синтез графа, экстракция."""
from __future__ import annotations

from .extract import extract_graph_from_messages
from .service import MaintenanceReport, MaintenanceService

__all__ = ["MaintenanceReport", "MaintenanceService", "extract_graph_from_messages"]
