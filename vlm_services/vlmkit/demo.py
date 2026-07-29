"""Демо-изображения для оффлайн-режима.

Настоящих фотографий в репозитории нет — это лишние мегабайты и чужие
права. Вместо них генерируем крошечный валидный PNG и прикладываем к нему
`scene`: описание того, что «видно» на кадре.

Оффлайн-модель читает `scene` и отвечает по нему, поэтому демо и тесты
проходят весь путь сервиса — нормализацию, промпт, разбор, арифметику и
отчёт, — не обращаясь в сеть.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

from .images import ImageRef


def png_bytes(width: int = 64, height: int = 64, rgb: tuple[int, int, int] = (200, 200, 200)
              ) -> bytes:
    """Собрать валидный PNG заданного размера без Pillow."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def demo_image(name: str, scene: dict[str, Any] | None = None, size: int = 64,
               rgb: tuple[int, int, int] = (200, 200, 200)) -> ImageRef:
    """Изображение-заглушка с описанием сцены для оффлайн-модели."""
    data = png_bytes(size, size, rgb)
    return ImageRef(data=data, fmt="png", width=size, height=size,
                    name=name, scene=dict(scene or {}))
