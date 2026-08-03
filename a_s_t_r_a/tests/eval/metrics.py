"""Evaluation metrics for A.S.T.R.A. agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskResult:
    task_id: str
    goal: str
    status: str
    result: str
    steps_taken: int
    duration_seconds: float
    expected_contains: list[str] = field(default_factory=list)
    expected_status: str | None = None
    passed: bool = False
    score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalMetrics:
    total_tasks: int = 0
    passed_tasks: int = 0
    failed_tasks: int = 0
    avg_steps: float = 0.0
    avg_duration: float = 0.0
    success_rate: float = 0.0
    results: list[TaskResult] = field(default_factory=list)

    def compute(self) -> None:
        if not self.results:
            return
        self.total_tasks = len(self.results)
        self.passed_tasks = sum(1 for r in self.results if r.passed)
        self.failed_tasks = self.total_tasks - self.passed_tasks
        self.avg_steps = sum(r.steps_taken for r in self.results) / self.total_tasks if self.total_tasks else 0
        self.avg_duration = sum(r.duration_seconds for r in self.results) / self.total_tasks if self.total_tasks else 0
        self.success_rate = self.passed_tasks / self.total_tasks if self.total_tasks else 0

    def to_dict(self) -> dict[str, Any]:
        self.compute()
        return {
            "total_tasks": self.total_tasks,
            "passed_tasks": self.passed_tasks,
            "failed_tasks": self.failed_tasks,
            "success_rate": round(self.success_rate, 3),
            "avg_steps": round(self.avg_steps, 2),
            "avg_duration": round(self.avg_duration, 2),
            "results": [
                {
                    "task_id": r.task_id,
                    "passed": r.passed,
                    "score": r.score,
                    "status": r.status,
                    "steps_taken": r.steps_taken,
                    "duration": round(r.duration_seconds, 2),
                    "details": r.details,
                }
                for r in self.results
            ],
        }

    def print_report(self) -> None:
        self.compute()
        print(f"\n{'='*60}")
        print(f"EVAL REPORT: {self.passed_tasks}/{self.total_tasks} passed ({self.success_rate*100:.1f}%)")
        print(f"Avg steps: {self.avg_steps:.2f}, Avg duration: {self.avg_duration:.2f}s")
        print(f"{'='*60}")
        for r in self.results:
            status_icon = "✅" if r.passed else "❌"
            print(f"{status_icon} {r.task_id}: {r.status} ({r.steps_taken} steps, {r.duration_seconds:.1f}s) score={r.score:.2f}")
            if not r.passed:
                print(f"   Goal: {r.goal[:80]}")
                print(f"   Result snippet: {r.result[:200]}")
                print(f"   Details: {r.details}")
        print(f"{'='*60}\n")
