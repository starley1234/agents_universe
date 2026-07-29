"""Синхронный безопасный исполнитель детерминированных workflow-этапов.
Долгие LLM-этапы добавляются оркестратором; QA выполняется кодом, не моделью.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..config import Config
from ..memory.store import Store
from ..site_qa import check_site

class WorkflowRunner:
    def __init__(self, cfg: Config, store: Store) -> None: self.cfg, self.store = cfg, store
    def run(self, workflow_id: int) -> dict[str, Any]:
        workflow=self.store.get_workflow(workflow_id)
        if not workflow: raise ValueError(f"workflow {workflow_id} не найден")
        if workflow["status"] in {"completed","cancelled"}: return workflow
        self.store.set_workflow(workflow_id, status="running", error="")
        for step in self.store.workflow_steps(workflow_id):
            if step["status"] in {"done","skipped"}: continue
            self.store.set_workflow_step(step["id"], "running")
            try:
                out=self._run_step(workflow, step)
                self.store.set_workflow_step(step["id"], "done", out)
            except Exception as exc:
                self.store.set_workflow_step(step["id"], "failed", error=str(exc))
                self.store.set_workflow(workflow_id, status="failed", error=str(exc))
                return self.store.get_workflow(workflow_id) or workflow
        self.store.set_workflow(workflow_id, status="review_required")
        return self.store.get_workflow(workflow_id) or workflow
    def _run_step(self, workflow: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        if step["kind"] == "qa":
            site=str((workflow.get("state") or {}).get("site_root", ""))
            if not site: return {"skipped": True, "reason": "state.site_root не задан"}
            report=check_site(site)
            if not report["ok"]: raise ValueError("site QA не пройден: " + "; ".join(report["errors"][:5]))
            return report
        # Агентские steps намеренно остаются pending-to-done orchestration layer:
        # runner не выдумывает результат без назначенного агента.
        return {"pending_agent_execution": True, "kind": step["kind"]}
