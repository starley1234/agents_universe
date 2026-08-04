"""Файловое хранилище: управление загруженными документами и knowledge_base."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class StoredFile:
    """Информация о сохранённом файле."""
    file_id: str
    original_name: str
    stored_path: str
    file_hash: str
    size_bytes: int
    content_type: str
    ingested_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


class FileStore:
    """Управление файлами в knowledge_base.

    Структура:
    workspace/
    ├── files/          # Оригинальные файлы (по хешу)
    ├── index.json      # Реестр файлов
    └── graph.json      # Семантический граф
    """

    def __init__(self, workspace_dir: str | Path):
        self._workspace = Path(workspace_dir)
        self._files_dir = self._workspace / "files"
        self._index_path = self._workspace / "index.json"
        self._index: dict[str, StoredFile] = {}

        self._workspace.mkdir(parents=True, exist_ok=True)
        self._files_dir.mkdir(parents=True, exist_ok=True)

        if self._index_path.is_file():
            self._load_index()

    def store(self, source_path: str | Path, copy: bool = True) -> StoredFile:
        """Сохраняет файл в хранилище."""
        src = Path(source_path)
        if not src.is_file():
            raise FileNotFoundError(f"File not found: {src}")

        data = src.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        file_id = file_hash[:16]

        # Если уже есть — возвращаем
        if file_id in self._index:
            return self._index[file_id]

        # Определяем тип
        content_type = self._guess_content_type(src.suffix)

        # Сохраняем
        if copy:
            ext = src.suffix.lower()
            stored_name = f"{file_id}{ext}"
            stored_path = self._files_dir / stored_name
            shutil.copy2(src, stored_path)
        else:
            stored_path = src

        entry = StoredFile(
            file_id=file_id,
            original_name=src.name,
            stored_path=str(stored_path),
            file_hash=file_hash,
            size_bytes=len(data),
            content_type=content_type,
            ingested_at=datetime.now(timezone.utc).isoformat(),
        )

        self._index[file_id] = entry
        self._save_index()

        return entry

    def get(self, file_id: str) -> StoredFile | None:
        return self._index.get(file_id)

    def list_all(self) -> list[StoredFile]:
        return list(self._index.values())

    def delete(self, file_id: str) -> bool:
        entry = self._index.pop(file_id, None)
        if not entry:
            return False

        stored = Path(entry.stored_path)
        if stored.exists() and self._files_dir in stored.parents:
            stored.unlink()

        self._save_index()
        return True

    def count(self) -> int:
        return len(self._index)

    def total_size(self) -> int:
        return sum(e.size_bytes for e in self._index.values())

    def find_by_hash(self, file_hash: str) -> StoredFile | None:
        for entry in self._index.values():
            if entry.file_hash == file_hash:
                return entry
        return None

    def _save_index(self) -> None:
        data = {fid: {
            "file_id": e.file_id,
            "original_name": e.original_name,
            "stored_path": e.stored_path,
            "file_hash": e.file_hash,
            "size_bytes": e.size_bytes,
            "content_type": e.content_type,
            "ingested_at": e.ingested_at,
            "metadata": e.metadata,
        } for fid, e in self._index.items()}
        self._index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_index(self) -> None:
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            for fid, d in data.items():
                self._index[fid] = StoredFile(**d)
        except Exception:
            self._index = {}

    @staticmethod
    def _guess_content_type(ext: str) -> str:
        types = {
            ".pdf": "application/pdf",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".csv": "text/csv",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        return types.get(ext.lower(), "application/octet-stream")
