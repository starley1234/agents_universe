"""WebSocket connection manager — real-time task updates for the web UI."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class WSManager:
    def __init__(self):
        self._global: list[WebSocket] = []
        self._per_task: dict[str, list[WebSocket]] = {}

    async def connect_global(self, ws: WebSocket):
        await ws.accept()
        self._global.append(ws)

    def disconnect_global(self, ws: WebSocket):
        if ws in self._global:
            self._global.remove(ws)

    async def connect_task(self, task_id: str, ws: WebSocket):
        await ws.accept()
        self._per_task.setdefault(task_id, []).append(ws)

    def disconnect_task(self, task_id: str, ws: WebSocket):
        conns = self._per_task.get(task_id, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            self._per_task.pop(task_id, None)

    async def broadcast(self, task_id: str, data: dict[str, Any]):
        msg = json.dumps({"type": "task_update", "task_id": task_id,
                          "ts": datetime.now(timezone.utc).isoformat(), "data": data},
                         ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in self._per_task.get(task_id, []):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_task(task_id, ws)
        dead = []
        for ws in self._global:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_global(ws)

    @property
    def connections(self) -> int:
        return len(self._global) + sum(len(v) for v in self._per_task.values())


ws = WSManager()
