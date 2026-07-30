"""Инструмент визуального анализа изображений (Vision) для агентов MAOS."""
from __future__ import annotations

import os
from typing import Any

from ..config import Config
from ..vision.openai_like import analyze_images
from .base import Tool, ToolError, Workspace


def build(ws: Workspace, cfg: Config) -> list[Tool]:
    def analyze_image(image_path: str, prompt: str, model: str = "") -> str:
        image_path = (image_path or "").strip()
        prompt = (prompt or "").strip()
        if not image_path:
            raise ToolError("Параметр 'image_path' обязателен для analyze_image")
        if not prompt:
            raise ToolError("Параметр 'prompt' обязателен для analyze_image")

        try:
            target_file = ws.resolve(image_path)
        except Exception as exc:
            raise ToolError(f"Недопустимый путь к изображению: {exc}") from exc

        if not target_file.exists() or not target_file.is_file():
            raise ToolError(f"Файл изображения {image_path!r} не найден в рабочей папке агента")

        base_url = (
            getattr(cfg, "vision_base_url", "")
            or os.getenv("MAOS_VISION_BASE_URL", "")
            or os.getenv("OPENROUTER_BASE_URL", "")
            or os.getenv("OPENAI_BASE_URL", "")
            or "https://openrouter.ai/api/v1"
        )
        api_key = (
            getattr(cfg, "vision_api_key", "")
            or os.getenv("MAOS_VISION_API_KEY", "")
            or os.getenv("OPENROUTER_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")
        )
        use_model = (
            (model or "").strip()
            or getattr(cfg, "vision_model", "")
            or os.getenv("MAOS_VISION_MODEL", "")
            or "openai/gpt-4o-mini"
        )

        if not api_key:
            raise ToolError(
                "API-ключ для Vision не задан. Укажите MAOS_VISION_API_KEY, "
                "OPENROUTER_API_KEY или OPENAI_API_KEY в окружении (.env)"
            )

        try:
            return analyze_images(
                [target_file],
                prompt,
                base_url=base_url,
                api_key=api_key,
                model=use_model,
            )
        except Exception as exc:
            raise ToolError(f"Ошибка вызова Vision-модели ({use_model}): {exc}") from exc

    return [
        Tool(
            "analyze_image",
            "Анализирует изображение (png, jpg, webp, gif) из рабочей папки агента с помощью Vision LLM-модели по заданному текстовому запросу.",
            {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Относительный путь к файлу изображения в рабочей папке агента (например, 'chart.png')."
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Вопрос или инструкция для анализа изображения (например, 'Что изображено на графике?')."
                    },
                    "model": {
                        "type": "string",
                        "description": "Имя Vision-модели (по умолчанию берётся из настроек MAOS)."
                    }
                },
                "required": ["image_path", "prompt"]
            },
            analyze_image
        )
    ]
