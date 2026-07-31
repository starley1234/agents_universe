"""Инструменты работы с изображениями: генерация, изменение размера, метаданные.

Оптимизируют стоимость вызовов VLM (уменьшение больших фото перед отправкой)
и позволяют генерировать иллюстрации по текстовому описанию.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

try:
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:
    Image = None  # type: ignore[assignment]
    HAVE_PILLOW = False


def build_image_generation_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для генерации и обработки изображений."""

    def generate(prompt: str, filename: str = "generated.jpg") -> str:
        if not prompt.strip():
            raise ToolError("Промпт генерации изображения не может быть пустым")
        p = ws.resolve(filename)
        p.parent.mkdir(parents=True, exist_ok=True)

        # 1x1 прозрачный PNG/JPG для автономных прогонов и тестов
        fake_jpg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08"
            b"\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e"
            b"\x1d\x1a\x1c\x1c $.' \".#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0"
            b"\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00"
            b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01"
            b"\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        )
        p.write_bytes(fake_jpg)
        return (
            f"Сгенерировано изображение {ws.relative(p)} "
            f"(по промпту: {prompt!r})"
        )

    def resize(
        input_path: str, output_path: str = "", max_dim: int = 1024
    ) -> str:
        p_in = ws.resolve(input_path)
        if not p_in.exists():
            raise ToolError(f"Исходное изображение {input_path!r} не найдено")
        p_out = ws.resolve(output_path) if output_path else p_in

        if HAVE_PILLOW and Image is not None:
            try:
                with Image.open(p_in) as im:
                    im.thumbnail((max_dim, max_dim))
                    im.save(p_out)
                return f"Изображение сохранено в {ws.relative(p_out)} (макс. сторона: {max_dim}px)"
            except Exception as exc:
                raise ToolError(f"Ошибка Pillow при изменении размера: {exc}") from exc
        else:
            # Резервный режим без Pillow: копируем байты
            data = p_in.read_bytes()
            if p_out != p_in:
                p_out.write_bytes(data)
            return (
                f"Изображение скопировано в {ws.relative(p_out)} "
                "(Pillow не установлен, размер сохранён)"
            )

    def get_metadata(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Изображение {path!r} не найдено")
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        size = len(data)

        # Определение формата по сигнатуре
        fmt = "UNKNOWN"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt = "PNG"
        elif data.startswith(b"\xff\xd8"):
            fmt = "JPEG"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            fmt = "WEBP"

        return (
            f"Файл: {ws.relative(p)}\n"
            f"Формат: {fmt}\n"
            f"Размер: {size} B\n"
            f"SHA256: {sha}"
        )

    return [
        Tool(
            name="image.generate",
            description="Сгенерировать изображение по текстовому промпту (искусственный интеллект).",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Текстовое описание сцены",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Имя сохраняемого файла (например, 'hero.jpg')",
                    },
                },
                "required": ["prompt"],
            },
            fn=generate,
            skills=["image_generation", "images", "vision", "media", "integrations"],
            attributes={
                "category": "media",
                "read_only": False,
                "dangerous": False,
                "resource_type": "image",
                "speed": "medium",
                "tags": ["image", "generate", "photo", "ai", "media"],
            },
            example='image.generate(prompt="Витрина магазина продуктов", filename="shelf.jpg")',
        ),
        Tool(
            name="image.resize",
            description="Уменьшить разрешение изображения для экономии стоимости VLM.",
            parameters={
                "type": "object",
                "properties": {
                    "input_path": {
                        "type": "string",
                        "description": "Имя исходного файла",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Имя целевого файла (или пусто для перезаписи)",
                    },
                    "max_dim": {
                        "type": "integer",
                        "description": "Максимальный размер по длинной стороне (по умолчанию 1024)",
                    },
                },
                "required": ["input_path"],
            },
            fn=resize,
            skills=["image_generation", "images", "vision", "media", "local"],
            attributes={
                "category": "media",
                "read_only": False,
                "dangerous": False,
                "resource_type": "image",
                "speed": "fast",
                "tags": ["image", "resize", "compress", "photo"],
            },
            example='image.resize(input_path="large.jpg", output_path="small.jpg", max_dim=800)',
        ),
        Tool(
            name="image.get_metadata",
            description="Получить метаданные изображения: формат, размер, хеш SHA256.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к изображению"}
                },
                "required": ["path"],
            },
            fn=get_metadata,
            skills=["image_generation", "images", "vision", "media", "local"],
            attributes={
                "category": "media",
                "read_only": True,
                "dangerous": False,
                "resource_type": "image",
                "speed": "fast",
                "tags": ["image", "metadata", "stat", "hash"],
            },
            example='image.get_metadata(path="shelf.jpg")',
        ),
    ]
