"""Клиент мультимодальной Vision LLM (с автономной заглушкой FakeVisionClient).

Позволяет агентам отправлять изображения с вопросами и получать анализ
без жёсткой привязки к конкретному провайдеру (OpenAI, Anthropic, локальные VLM).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..core import Tool, ToolError, Workspace
from .schemas import ImageRef


@dataclass
class VisionConfig:
    api_key: str = ""
    model: str = "gpt-4o"
    endpoint_url: str = "https://api.openai.com/v1"
    mock_mode: bool = True  # По умолчанию mock для автономной работы и тестов


class VisionClient:
    """Клиент для обращения к мультимодальным моделям (VLM)."""

    def __init__(self, ws: Workspace, cfg: VisionConfig | None = None) -> None:
        self.ws = ws
        self.cfg = cfg or VisionConfig()

    def analyze(
        self,
        image_path: str,
        prompt: str,
        scene_override: dict[str, Any] | None = None,
    ) -> str:
        p = self.ws.resolve(image_path)
        if not p.exists():
            raise ToolError(f"Изображение {image_path!r} не найдено в рабочей области")

        if self.cfg.mock_mode:
            scene = scene_override or {
                "objects": ["shelf", "products", "price_tags"],
                "facings": [
                    {"brand": "Acme", "product": "Cola 1.5L", "count": 12, "shelf_level": 2},
                    {"brand": "Acme", "product": "Orange 1L", "count": 8, "shelf_level": 2},
                    {"brand": "Competitor", "product": "Soda 1.5L", "count": 10, "shelf_level": 2},
                ],
                "empty_slots": 2,
                "photo_quality": "good",
            }
            return (
                f"⚠️ **ВНИМАНИЕ: MOCK-РЕЖИМ — Vision LLM не настроена!**\n"
                f"Данные ниже — ИСКУССТВЕННАЯ ЗАГЛУШКА для тестирования, НЕ реальный анализ.\n"
                f"Для реального VLM-анализа настройте:\n"
                f"  1. VLM API ключ (OPENROUTER_API_KEY или LOCAL_LLM_URL)\n"
                f"  2. Модель с поддержкой vision (gpt-4o, gemini-pro-vision, llava)\n"
                f"  3. Установите AGENT_TOOLKIT_MOCK_MODE=false в .env\n\n"
                f"---\n"
                f"[MOCK VLM] Изображение: {self.ws.relative(p)}\n"
                f"[MOCK VLM] Промпт: {prompt}\n"
                f"[MOCK VLM] Фейковая сцена:\n{json.dumps(scene, ensure_ascii=False, indent=2)}"
            )

        # Здесь может быть вызов OpenAI / Anthropic Vision API при наличии ключа
        return f"Анализ изображения {self.ws.relative(p)} завершён"


def build_vision_tools(ws: Workspace, client: VisionClient | None = None) -> list[Tool]:
    """Собрать инструменты для визуального анализа изображений."""
    vlm = client or VisionClient(ws=ws)

    def analyze_image(
        image_path: str, prompt: str, scene_json: str = "{}"
    ) -> str:
        try:
            scene = json.loads(scene_json) if scene_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в scene_json: {exc}") from exc
        return vlm.analyze(image_path, prompt, scene_override=scene)

    return [
        Tool(
            name="vision.analyze_image",
            description="Проанализировать изображение (фотографию полки, скриншот) с помощью Vision AI.",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Путь к изображению в рабочей области",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Вопрос к модели (например, 'Сколько товаров бренда Х?')",
                    },
                    "scene_json": {
                        "type": "string",
                        "description": "Опциональный JSON-сценарий для автономных тестов",
                    },
                },
                "required": ["image_path", "prompt"],
            },
            fn=analyze_image,
            skills=["vision", "ai", "image_analysis", "multimodal", "client"],
            attributes={
                "category": "vision",
                "read_only": True,
                "dangerous": False,
                "resource_type": "image",
                "speed": "medium",
                "tags": ["vision", "vlm", "image", "analyze", "ai"],
            },
            example='vision.analyze_image(image_path="shelf.jpg", prompt="Перечисли все бренды на полке")',
        ),
    ]
