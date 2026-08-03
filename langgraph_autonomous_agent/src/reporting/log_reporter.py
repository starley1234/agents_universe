"""Structured log reporter — JSONL + markdown reports."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from src.config import get_settings

log = logging.getLogger(__name__)
_cfg = get_settings()


class LogReporter:
    def __init__(self, task_id: str, title: str):
        self.task_id = task_id
        self.title = title
        self.dir = os.path.join(_cfg.WORKSPACE_PATH, ".logs")
        os.makedirs(self.dir, exist_ok=True)
        base = os.path.join(self.dir, f"task_{task_id[:8]}")
        self.log_path = base + ".log"
        self.jsonl_path = base + ".jsonl"
        self.report_path = base + "_report.md"

    def _write(self, level: str, msg: str, data: dict | None = None):
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.log_path, "a") as f:
                f.write(f"[{ts}] [{level}] {msg}\n")
        except Exception:
            pass
        try:
            with open(self.jsonl_path, "a") as f:
                entry = {"ts": ts, "level": level, "task_id": self.task_id, "msg": msg, **(data or {})}
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def log_info(self, m: str, **kw):
        self._write("INFO", m, kw)
        log.info("[%s] %s", self.task_id[:8], m)

    def log_warning(self, m: str, **kw):
        self._write("WARN", m, kw)
        log.warning("[%s] %s", self.task_id[:8], m)

    def log_error(self, m: str, **kw):
        self._write("ERROR", m, kw)
        log.error("[%s] %s", self.task_id[:8], m)

    def generate_report(self, state: dict[str, Any]) -> str:
        plan = state.get("plan", [])
        results = state.get("results", [])
        errors = state.get("errors", [])
        quality = state.get("quality", 0)
        iteration = state.get("iteration", 0)
        progress = state.get("progress", 0)
        status = state.get("status", "?")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        md = f"""# Agent Report — {self.title}

**Task ID:** {self.task_id} · **Status:** {status} · **Generated:** {now}

## Progress: {progress:.0f}% · Iterations: {iteration} · Quality: {quality:.2f}

| # | Step | Status |
|---|------|--------|
"""
        for s in plan:
            icon = {"completed": "✅", "failed": "❌", "pending": "⏳"}.get(s.get("status"), "❓")
            md += f"| {s.get('id','?')} | {s.get('description','')[:80]} | {icon} |\n"

        if results:
            md += "\n## Results\n\n"
            for r in results[-5:]:
                md += f"### Step {r.get('step','?')}\n**{r.get('description','')}** ({r.get('duration_s',0):.1f}s)\n```\n{str(r.get('result',''))[:1000]}\n```\n\n"

        if errors:
            md += "\n## Errors\n\n" + "\n".join(f"- {e}" for e in errors) + "\n"

        final = state.get("final_result", "")
        if final:
            md += f"\n## Final Result\n\n{final}\n"

        try:
            with open(self.report_path, "w") as f:
                f.write(md)
        except Exception:
            pass
        return md
