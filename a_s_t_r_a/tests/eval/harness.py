"""Evaluation harness — runs golden tasks through agent and scores."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from astra.config import settings
from astra.core.agent import agent_graph
from astra.core.state import make_initial_state
from astra.db.engine import engine, get_session
from astra.db.models import Base
from astra.db.repositories import ProjectRepo, SessionRepo

from .metrics import EvalMetrics, TaskResult


TASKS_FILE = Path(__file__).parent / "tasks.json"


class EvalHarness:
    """Runs evaluation tasks and collects metrics."""

    def __init__(self, tasks_file: Path | None = None, mock_llm: bool = True) -> None:
        self.tasks_file = tasks_file or TASKS_FILE
        self.mock_llm = mock_llm
        self.tasks = self._load_tasks()

    def _load_tasks(self) -> list[dict[str, Any]]:
        if not self.tasks_file.exists():
            logger.warning("Tasks file not found: {}", self.tasks_file)
            return []
        try:
            data = json.loads(self.tasks_file.read_text(encoding="utf-8"))
            logger.info("Loaded {} eval tasks", len(data))
            return data
        except Exception as exc:
            logger.error("Failed to load tasks: {}", exc)
            return []

    async def _ensure_db(self) -> None:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except Exception as exc:
            logger.warning("DB ensure failed: {}", exc)

    async def run_single_task(self, task: dict[str, Any]) -> TaskResult:
        task_id = task.get("id", str(uuid4()))
        goal = task.get("goal", "")
        expected_contains = task.get("expected_contains", [])
        expected_status = task.get("expected_status")
        max_steps = task.get("max_steps", 10)
        project_name = task.get("project_name", f"eval_{task_id}")

        logger.info("🧪 Running eval task: {} — {}", task_id, goal[:80])

        start = time.time()

        # Ensure DB tables exist
        await self._ensure_db()

        # Create isolated project for eval
        try:
            async with get_session() as db:
                project_repo = ProjectRepo(db)
                project = await project_repo.create(project_name, f"Eval task {task_id}")
                pid = project.id
        except Exception as exc:
            logger.error("Failed to create eval project: {}", exc)
            return TaskResult(
                task_id=task_id,
                goal=goal,
                status="error",
                result=f"Project creation failed: {exc}",
                steps_taken=0,
                duration_seconds=time.time() - start,
                expected_contains=expected_contains,
                expected_status=expected_status,
                passed=False,
                score=0.0,
                details={"error": str(exc)},
            )

        # Run agent
        try:
            sid = uuid4()
            initial_state = make_initial_state(session_id=sid, project_id=pid, goal=goal)
            final_state = await agent_graph.ainvoke(initial_state)

            ai_messages = [m for m in final_state.get("messages", []) if getattr(m, "type", None) == "ai"]
            result = ai_messages[-1].content if ai_messages else "No result"
            status = "halted" if final_state.get("is_halted") else "completed"
            steps_taken = len(final_state.get("completed_steps", []))

            # Persist result for debugging
            try:
                async with get_session() as db:
                    session_repo = SessionRepo(db)
                    sess = await session_repo.create(pid, goal)
                    await session_repo.finish(sess.id, result, status)
            except Exception:
                pass

        except Exception as exc:
            logger.exception("Eval task {} failed", task_id)
            result = f"Error: {exc}"
            status = "error"
            steps_taken = 0

        duration = time.time() - start

        passed, score, details = self._score_task(
            task_id=task_id,
            goal=goal,
            result=result,
            status=status,
            steps_taken=steps_taken,
            expected_contains=expected_contains,
            expected_status=expected_status,
            max_steps=max_steps,
        )

        return TaskResult(
            task_id=task_id,
            goal=goal,
            status=status,
            result=result,
            steps_taken=steps_taken,
            duration_seconds=duration,
            expected_contains=expected_contains,
            expected_status=expected_status,
            passed=passed,
            score=score,
            details=details,
        )

    def _score_task(
        self,
        task_id: str,
        goal: str,
        result: str,
        status: str,
        steps_taken: int,
        expected_contains: list[str],
        expected_status: str | None,
        max_steps: int,
    ) -> tuple[bool, float, dict]:
        score = 0.0
        details: dict[str, Any] = {}
        passed = True

        if expected_status:
            if status != expected_status:
                passed = False
                details["status_mismatch"] = f"expected {expected_status}, got {status}"
            else:
                score += 0.3
        else:
            if status == "completed":
                score += 0.3
            elif status == "halted" and "circuit" in goal.lower():
                score += 0.3
            else:
                if status != "completed":
                    passed = False
                    details["unexpected_status"] = status

        if expected_contains:
            found = 0
            missing = []
            result_lower = result.lower()
            for expected in expected_contains:
                if expected.lower() in result_lower:
                    found += 1
                else:
                    missing.append(expected)
            content_score = found / len(expected_contains) if expected_contains else 1.0
            score += content_score * 0.5
            details["content_match"] = f"{found}/{len(expected_contains)} found"
            if missing:
                details["missing_keywords"] = missing
                if content_score < 0.5:
                    passed = False
        else:
            score += 0.3

        if steps_taken <= max_steps:
            score += 0.2
        else:
            details["too_many_steps"] = f"{steps_taken} > {max_steps}"
            score += max(0, 0.2 - (steps_taken - max_steps) * 0.02)
            if steps_taken > max_steps * 1.5:
                passed = False

        score = max(0.0, min(1.0, score))
        if score < 0.5:
            passed = False

        return passed, score, details

    async def run_all(self) -> EvalMetrics:
        await self._ensure_db()
        metrics = EvalMetrics()
        for task in self.tasks:
            result = await self.run_single_task(task)
            metrics.results.append(result)
            await asyncio.sleep(0.5)

        metrics.compute()
        return metrics

    def run_all_sync(self) -> EvalMetrics:
        return asyncio.run(self.run_all())


async def main() -> None:
    harness = EvalHarness()
    metrics = await harness.run_all()
    metrics.print_report()

    report_path = Path("eval_report.json")
    report_path.write_text(json.dumps(metrics.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
