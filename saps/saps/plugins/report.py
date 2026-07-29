"""Плагин «Report Generator» (ТЗ п.3.4): сборка протокола соответствия.

Обёртка над saps.export.reports в контракте плагина — чтобы генерация
отчёта запускалась тем же способом, что и остальные расширения
(`saps plugin run report ...`), и попадала в тот же журнал.

Сама логика сборки живёт в export/reports.py, а не здесь: протокол нужен
и веб-интерфейсу, и CLI напрямую, а плагин — лишь один из способов его
вызвать.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..agents.base import AgentReport
from ..export.reports import (compliance_docx, compliance_xlsx, export_path,
                              requirements_xlsx)
from .base import Plugin


class ReportPlugin(Plugin):
    """Сборка «Протокола соответствия» в Word/Excel по данным базы."""

    name = "report"
    title = "Генератор протокола соответствия (Word/Excel)"
    needs_llm = False

    def run(self, *, node_code: str = "", owner: str = "",
            fmt: str = "docx", path: str = "", **kwargs: Any) -> AgentReport:
        report = self._report()
        report.agent = self.name

        fmt = (fmt or "docx").lower()
        if fmt not in ("docx", "xlsx", "both", "requirements"):
            report.errors.append(
                f"Формат {fmt!r} не поддерживается: docx | xlsx | both | "
                "requirements")
            return report

        workdir = Path(self.cfg.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        created: list[str] = []

        try:
            if fmt in ("docx", "both"):
                target = Path(path) if path and fmt == "docx" else export_path(
                    workdir, "Протокол_соответствия", "docx")
                created.append(str(compliance_docx(
                    self.store, self.cfg, target, node_code=node_code,
                    owner=owner)))
            if fmt in ("xlsx", "both"):
                target = Path(path) if path and fmt == "xlsx" else export_path(
                    workdir, "Протокол_соответствия", "xlsx")
                created.append(str(compliance_xlsx(
                    self.store, target, node_code=node_code, owner=owner)))
            if fmt == "requirements":
                target = Path(path) if path else export_path(
                    workdir, "Требования", "xlsx")
                created.append(str(requirements_xlsx(
                    self.store, target, node_code=node_code, owner=owner)))
        except OSError as exc:
            report.errors.append(f"Не удалось записать файл: {exc}")
            return report

        report.processed = len(created)
        report.findings.append({"kind": "export", "files": created})
        self.store.log(f"plugin:{self.name}", "plugin_run",
                       detail=f"создано файлов: {len(created)}",
                       data={"files": created})
        return report
