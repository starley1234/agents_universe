"""Синхронный безопасный исполнитель детерминированных workflow-этапов.
Долгие LLM-этапы добавляются оркестратором; QA выполняется кодом, не моделью.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from ..config import Config
from ..memory.store import Store
from ..site_qa import check_site
from ..llm.embeddings import build_embedder
from ..orchestrator.service import Orchestrator
from ..tools.base import Workspace
from ..tools import office_docs
from .room_inventory import inventory_markdown

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
        if step["kind"] == "docx_generation":
            inventory = (workflow.get("state") or {}).get("inventory")
            if not isinstance(inventory, dict):
                raise ValueError("state.inventory с валидной описью обязателен")
            ws = Workspace(Path(self.cfg.artifact_root) / "workflow" / str(workflow["id"]))
            path = "inventory.docx"
            tools = {t.name: t for t in office_docs.build(ws)}
            tools["docx_create"].fn(path=path, markdown=inventory_markdown(inventory))
            file_path = ws.resolve(path)
            artifact_id = self.store.add_artifact(workflow["id"], "inventory_docx", file_path.name,
                                                  str(Path("workflow") / str(workflow["id"]) / path),
                                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                                  file_path.stat().st_size, step_id=step["id"])
            return {"artifact_id": artifact_id, "path": path, "size_bytes": file_path.stat().st_size}
        if step["kind"] == "qa":
            site=str((workflow.get("state") or {}).get("site_root", ""))
            if not site: return {"skipped": True, "reason": "state.site_root не задан"}
            report=check_site(site)
            if not report["ok"]: raise ValueError("site QA не пройден: " + "; ".join(report["errors"][:5]))
            return report
        # Агентские шаги исполняются только при явном назначении: state.agent_map
        # хранит {"research": "researcher", "implementation": "frontend"}.
        # Это предотвращает произвольный выбор личности для внешних действий.
        state = workflow.get("state") or {}
        agent_slug = (state.get("agent_map") or {}).get(step["kind"])
        if not agent_slug:
            return {"requires_agent": True, "kind": step["kind"],
                    "hint": "укажите state.agent_map.<kind> = slug агента"}
        embedder = build_embedder(*self.cfg.resolve_embedding()[:2], dim=self.cfg.embedding_dim,
                                  base_url=self.cfg.resolve_embedding()[2],
                                  api_key=self.cfg.resolve_embedding()[3],
                                  timeout=self.cfg.resolve_embedding()[4])
        task = (step.get("input") or {}).get("task") or (
            f"Выполни этап workflow '{step['kind']}'. Контекст задачи: "
            f"{workflow.get('input') or {}}. Сохрани проверяемый результат в своей рабочей папке "
            "и в ответе кратко перечисли созданные файлы и следующий результат.")
        result = Orchestrator(self.cfg, self.store, embedder).chat(str(task), agent_slug=str(agent_slug))
        return {"agent_slug": agent_slug, "conversation_id": result.conversation_id,
                "provider_model": result.turn.provider_model, "answer": result.turn.text,
                "tool_calls": result.turn.tool_calls, "stopped_by": result.turn.stopped_by}
