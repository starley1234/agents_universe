"""Eval API routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.auth.jwt import get_current_user
from astra.db.models import User

router = APIRouter(prefix="/api", tags=["eval"])

# Resolve tasks.json — both from repo root and from src location
POSSIBLE_PATHS = [
    Path(__file__).resolve().parents[4] / "tests" / "eval" / "tasks.json",  # src/astra/api/routes/eval.py -> repo/tests/eval/tasks.json
    Path(__file__).resolve().parents[3] / "tests" / "eval" / "tasks.json",
    Path.cwd() / "tests" / "eval" / "tasks.json",
    Path("/app/tests/eval/tasks.json"),
    Path("/app/src/../tests/eval/tasks.json"),
]


def _find_tasks_file() -> Path | None:
    for p in POSSIBLE_PATHS:
        if p.exists():
            return p
    # Search recursively
    for root in [Path.cwd(), Path("/app"), Path(__file__).resolve().parent.parent.parent.parent]:
        candidate = root / "tests" / "eval" / "tasks.json"
        if candidate.exists():
            return candidate
    return None


@router.get("/data/eval/tasks")
async def list_eval_tasks() -> list[dict[str, Any]]:
    f = _find_tasks_file()
    if f and f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data
        except Exception:
            pass
    return [
        {"id": "arch_overview", "goal": "Опиши архитектуру ASTRA", "expected_contains": ["память"], "max_steps": 6},
    ]


@router.post("/eval/run")
async def run_eval(
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        from tests.eval.harness import EvalHarness

        harness = EvalHarness()
        metrics = await harness.run_all()
        return metrics.to_dict()
    except Exception as exc:
        # Fallback: try import from different path
        try:
            import sys
            sys.path.insert(0, str(Path.cwd()))
            from tests.eval.harness import EvalHarness

            harness = EvalHarness()
            metrics = await harness.run_all()
            return metrics.to_dict()
        except Exception as exc2:
            return {"error": f"{exc} / {exc2}", "total_tasks": 0, "passed_tasks": 0, "success_rate": 0}
