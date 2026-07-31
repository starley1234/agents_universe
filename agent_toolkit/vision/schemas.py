"""Схемы данных для мультимодальных (Vision) и ритейл-аудиторских сервисов."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ImageRef:
    """Ссылка на изображение в рабочей области (или с data-URI/сценой для тестов)."""

    name: str = ""
    path: str = ""
    data_uri: str = ""
    sha256: str = ""
    width: int = 0
    height: int = 0
    scene: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, filepath: str | Path, scene: dict[str, Any] | None = None) -> "ImageRef":
        p = Path(filepath)
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        b64 = base64.b64encode(data).decode("ascii")
        ext = p.suffix.lower().lstrip(".") or "jpeg"
        mime = f"image/{ext}" if ext in ("png", "jpeg", "webp", "gif") else "image/jpeg"
        return cls(
            name=p.name,
            path=str(p),
            data_uri=f"data:{mime};base64,{b64}",
            sha256=sha,
            scene=scene or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FacingItem:
    """Элемент выкладки (фейсинг товара на полке)."""

    brand: str
    product: str
    count: int = 1
    shelf_level: int = 1
    price_tag: bool = True
    price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InventoryAuditResult:
    """Результат автоматического аудита полки (Retail Audit)."""

    shelf_levels: int = 0
    facings: list[FacingItem] = field(default_factory=list)
    empty_slots: int = 0
    sos_percentage: float = 0.0  # Share of Shelf (доля полки целевого бренда)
    compliance_score: float = 100.0  # Оценка соответствия планограмме
    issues: list[dict[str, Any]] = field(default_factory=list)
    photo_quality: str = "good"

    def to_dict(self) -> dict[str, Any]:
        return {
            "shelf_levels": self.shelf_levels,
            "facings": [f.to_dict() for f in self.facings],
            "empty_slots": self.empty_slots,
            "sos_percentage": self.sos_percentage,
            "compliance_score": self.compliance_score,
            "issues": self.issues,
            "photo_quality": self.photo_quality,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
