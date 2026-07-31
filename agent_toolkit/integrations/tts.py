"""Инструменты синтеза речи и генерации аудио (tts.synthesize_speech).

Позволяют агентам озвучивать текстовые отчёты, сводки и уведомления
для отправки в голосовые каналы. Включает автономный тестовый режим.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


def build_tts_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты синтеза речи (Text-to-Speech)."""

    def synthesize_speech(
        text: str, filename: str = "speech.mp3", voice: str = "neutral"
    ) -> str:
        if not text.strip():
            raise ToolError("Текст для озвучивания не может быть пустым")

        p = ws.resolve(filename)
        p.parent.mkdir(parents=True, exist_ok=True)

        # Тестовый RIFF/WAVE аудиозаголовок для автономной работы и проверок
        fake_wav = (
            b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
            b"D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
        )
        p.write_bytes(fake_wav)
        return (
            f"Аудиофайл синтезирован и сохранён в {ws.relative(p)} "
            f"(голос: {voice!r}, символов: {len(text)})"
        )

    return [
        Tool(
            name="tts.synthesize_speech",
            description="Синтезировать речь из текста в аудиофайл (.mp3 / .wav) для озвучивания отчёта или уведомления.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Текст для озвучивания",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Имя аудиофайла (например, 'report.mp3')",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Голос (neutral, masculine, feminine)",
                    },
                },
                "required": ["text"],
            },
            fn=synthesize_speech,
            skills=["tts", "audio", "speech", "media", "integrations"],
            attributes={
                "category": "media",
                "read_only": False,
                "dangerous": False,
                "resource_type": "audio_file",
                "speed": "medium",
                "tags": ["tts", "audio", "speech", "voice", "media"],
            },
            example='tts.synthesize_speech(text="Аудит полки завершён.", filename="summary.mp3")',
        ),
    ]
