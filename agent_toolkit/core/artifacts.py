"""Управление артефактами: отчёты, изображения, файлы выгрузок (ArtifactStore)."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .workspace import Workspace, WorkspaceError

MIME_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".json": "application/json",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


@dataclass
class Artifact:
    """Артефакт (результат работы агента или инструмента)."""

    name: str
    path: str
    mime_type: str
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        return cls(
            name=str(data["name"]),
            path=str(data["path"]),
            mime_type=str(data.get("mime_type", "application/octet-stream")),
            created_at=float(data.get("created_at", time.time())),
            metadata=data.get("metadata", {}),
            size=int(data.get("size", 0)),
        )


class ArtifactStore:
    """Хранилище и реестр артефактов внутри рабочей области (Workspace)."""

    def __init__(self, workspace: Workspace, subfolder: str = "artifacts") -> None:
        self.workspace = workspace
        self.subfolder = subfolder
        self.dir = self.workspace.root / self.subfolder
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "artifacts_index.json"
        self._lock = threading.RLock()
        with self._lock:
            self._index: dict[str, Artifact] = self._load_index()

    def _load_index(self) -> dict[str, Artifact]:
        if not self.index_path.exists():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return {
                name: Artifact.from_dict(item) for name, item in raw.items()
            }
        except (ValueError, OSError):
            return {}

    def _save_index(self) -> None:
        data = {name: art.to_dict() for name, art in self._index.items()}
        self.index_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_text(
        self,
        name: str,
        content: str,
        *,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Сохранить текстовый артефакт в хранилище."""
        with self._lock:
            rel_path = f"{self.subfolder}/{name}"
            p = self.workspace.resolve(rel_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            if not mime_type:
                mime_type = MIME_TYPES.get(p.suffix.lower(), "text/plain")
            art = Artifact(
                name=name,
                path=self.workspace.relative(p),
                mime_type=mime_type,
                created_at=time.time(),
                metadata=metadata or {},
                size=len(content.encode("utf-8")),
            )
            self._index[name] = art
            self._save_index()
            return art

    def save_bytes(
        self,
        name: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Сохранить бинарный артефакт (изображение, документ docx/xlsx)."""
        with self._lock:
            rel_path = f"{self.subfolder}/{name}"
            p = self.workspace.resolve(rel_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
            if not mime_type:
                mime_type = MIME_TYPES.get(p.suffix.lower(), "application/octet-stream")
            art = Artifact(
                name=name,
                path=self.workspace.relative(p),
                mime_type=mime_type,
                created_at=time.time(),
                metadata=metadata or {},
                size=len(content),
            )
            self._index[name] = art
            self._save_index()
            return art

    def get(self, name: str) -> Artifact | None:
        """Получить информацию об артефакте по имени."""
        with self._lock:
            return self._index.get(name)

    def list(
        self,
        *,
        tag: str | None = None,
        mime_type: str | None = None,
    ) -> list[Artifact]:
        """Отфильтровать список сохранённых артефактов."""
        with self._lock:
            results = list(self._index.values())
            if mime_type:
                results = [a for a in results if a.mime_type == mime_type]
            if tag:
                results = [
                    a
                    for a in results
                    if tag in a.metadata.get("tags", [])
                    or tag == a.metadata.get("category")
                ]
            return sorted(results, key=lambda x: x.created_at, reverse=True)

    def remove(self, name: str) -> bool:
        """Удалить артефакт из индекса и диска."""
        with self._lock:
            art = self._index.pop(name, None)
            if not art:
                return False
            self._save_index()
            try:
                p = self.workspace.resolve(art.path)
                if p.exists():
                    p.unlink()
            except (WorkspaceError, OSError):
                pass
            return True
