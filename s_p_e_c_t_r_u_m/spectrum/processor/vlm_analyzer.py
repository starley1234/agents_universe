"""VLM-анализатор: классификация и анализ сложных документов через Vision-LLM."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VLMResult:
    """Результат VLM-анализа."""
    text: str
    confidence: float = 0.0
    document_type: str = "unknown"
    structured_data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class VLMAnalyzer:
    """Анализирует изображения и сложные страницы через Vision-LLM.

    Каскад:
    1. Определяет тип документа (скан, чертёж, схема, таблица, рукопись)
    2. Извлекает текст с учётом типа
    3. Структурирует данные (таблицы → JSON, чертежи → описания)
    """

    def __init__(self, api_url: str = "", api_key: str = "", model: str = ""):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model

    @classmethod
    def from_settings(cls) -> VLMAnalyzer:
        """Создаёт анализатор из текущих настроек."""
        from ..config import settings
        cfg = settings()
        p = cfg.llm_profile
        return cls(api_url=p.api_url, api_key=p.api_key, model=p.model)

    def analyze_image(self, image_path: str | Path, prompt: str = "") -> VLMResult:
        """Анализирует изображение через VLM."""
        path = Path(image_path)
        data = path.read_bytes()
        ext = path.suffix.lower().replace(".", "")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp",
                "bmp": "bmp"}.get(ext, "png")

        if not prompt:
            prompt = self._build_extraction_prompt()

        return self._call_vlm(data, mime, prompt)

    def classify_page(self, image_data: bytes, mime: str = "png") -> str:
        """Классифицирует тип страницы: scan, table, diagram, handwritten, text."""
        prompt = (
            "Классифицируй эту страницу. Ответь одним словом:\n"
            "- scan: если это сканированный документ\n"
            "- table: если это таблица или электронная таблица\n"
            "- diagram: если это схема, чертёж или диаграмма\n"
            "- handwritten: если это рукописный текст\n"
            "- text: если это обычный текстовый документ\n"
            "- unknown: если не можешь определить\n"
            "Ответ: только одно слово из списка выше."
        )
        result = self._call_vlm(image_data, mime, prompt)
        doc_type = result.text.strip().lower()
        valid_types = {"scan", "table", "diagram", "handwritten", "text"}
        return doc_type if doc_type in valid_types else "unknown"

    def extract_table(self, image_data: bytes, mime: str = "png") -> VLMResult:
        """Извлекает таблицу из изображения в формате JSON."""
        prompt = (
            "На этом изображении таблица. Извлеки ВСЕ данные из неё.\n"
            "Верни результат строго в JSON формате:\n"
            '{"headers": ["колонка1", "колонка2", ...], '
            '"rows": [["значение1", "значение2", ...], ...]}\n'
            "Важно: сохрани ВСЕ числа и текст точно. Не пропускай строки."
        )
        result = self._call_vlm(image_data, mime, prompt)

        # Пытаемся распарсить JSON из ответа
        try:
            data = json.loads(result.text)
            result.structured_data = data
            result.document_type = "table"
        except json.JSONDecodeError:
            # Пытаемся найти JSON в тексте
            import re
            match = re.search(r"\{.*\}", result.text, re.S)
            if match:
                try:
                    data = json.loads(match.group())
                    result.structured_data = data
                    result.document_type = "table"
                except json.JSONDecodeError:
                    result.warnings.append("Failed to parse table JSON")

        return result

    def describe_diagram(self, image_data: bytes, mime: str = "png") -> VLMResult:
        """Описывает схему/чертёж с извлечением всех подписей и размеров."""
        prompt = (
            "Это инженерная схема или чертёж. Опиши подробно:\n"
            "1. Все текстовые подписи и обозначения\n"
            "2. Все размеры и допуски\n"
            "3. Все элементы и их взаимное расположение\n"
            "4. Общую структуру и назначение\n"
            "Будь максимально точным и полным."
        )
        result = self._call_vlm(image_data, mime, prompt)
        result.document_type = "diagram"
        return result

    def _build_extraction_prompt(self) -> str:
        return (
            "Проанализируй это изображение и извлеки ВСЕ текстовое содержимое.\n"
            "Правила:\n"
            "- Если это таблица — воспроизведи в формате markdown\n"
            "- Если это схема — опиши все подписи и размеры\n"
            "- Если это рукопись — распознай текст точно\n"
            "- Если это печатный текст — воспроизведи дословно\n"
            "Отвечай на языке оригинала документа."
        )

    def _call_vlm(self, image_data: bytes, mime: str, prompt: str) -> VLMResult:
        """Вызов VLM через OpenAI-совместимый API."""
        if not self.api_url:
            return VLMResult(text="", warnings=["No VLM API configured"])

        b64 = base64.b64encode(image_data).decode()

        try:
            import requests

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/{mime};base64,{b64}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 4096,
            }

            resp = requests.post(
                f"{self.api_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
            text = result["choices"][0]["message"]["content"]

            return VLMResult(text=text, confidence=0.8)

        except Exception as e:
            return VLMResult(text="", warnings=[str(e)])
