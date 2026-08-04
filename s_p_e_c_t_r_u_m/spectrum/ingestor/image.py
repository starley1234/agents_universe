"""Ингестор Image: OCR и VLM-анализ изображений, сканов, чертежей."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from .base import IngestResult, Ingestor, PageChunk, SourceType


class ImageIngestor(Ingestor):
    """Извлекает текст и структурированные данные из изображений.

    Каскад:
    - Уровень 2: Tesseract/PaddleOCR для простых сканов
    - Уровень 3: VLM для сложных схем, чертежей, рукописного текста
    """

    SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}

    def can_handle(self, source: str | Path) -> bool:
        p = Path(source) if isinstance(source, str) else source
        return p.suffix.lower() in self.SUPPORTED_EXTENSIONS

    def ingest(self, source: str | Path, use_vlm: bool = False, **kwargs) -> IngestResult:
        path = Path(source)
        data = self._read_bytes(path)
        file_hash = IngestResult.compute_hash(data)

        metadata: dict[str, Any] = {}
        chunks: list[PageChunk] = []

        if use_vlm:
            # Уровень 3: Vision-LLM для сложных изображений
            text, vlm_meta = self._vlm_extract(path, data, **kwargs)
            metadata.update(vlm_meta)
            if text.strip():
                chunks.append(PageChunk(
                    text=text,
                    page_number=1,
                    metadata={"method": "vlm", **vlm_meta},
                ))
        else:
            # Уровень 2: OCR
            text, ocr_meta = self._ocr_extract(path, data)
            metadata.update(ocr_meta)
            if text.strip():
                chunks.append(PageChunk(
                    text=text,
                    page_number=1,
                    metadata={"method": "ocr", **ocr_meta},
                ))

        # Информация об изображении
        img_meta = self._get_image_info(path, data)
        metadata.update(img_meta)

        return IngestResult(
            source_path=str(path),
            source_type=SourceType.IMAGE,
            chunks=chunks,
            file_hash=file_hash,
            total_pages=1,
            metadata=metadata,
        )

    def _ocr_extract(self, path: Path, data: bytes) -> tuple[str, dict[str, Any]]:
        """OCR через Tesseract."""
        try:
            import pytesseract
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            text = pytesseract.image_to_string(img, lang="rus+eng")
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

            return text, {"ocr_engine": "tesseract", "confidence": "auto"}

        except ImportError:
            pass

        # Фолбэк: пробуем PaddleOCR
        try:
            return self._paddle_ocr(path, data)
        except ImportError:
            pass

        return "", {"ocr_engine": "none", "warning": "No OCR engine available"}

    def _paddle_ocr(self, path: Path, data: bytes) -> tuple[str, dict[str, Any]]:
        """OCR через PaddleOCR."""
        from paddleocr import PaddleOCR

        ocr = PaddleOCR(use_angle_cls=True, lang="ru", show_log=False)
        import io
        img_path = str(path)
        result = ocr.ocr(img_path, cls=True)

        lines = []
        if result and result[0]:
            for line in result[0]:
                if line and len(line) >= 2:
                    text_part = line[1]
                    if isinstance(text_part, (list, tuple)):
                        lines.append(text_part[0])
                    else:
                        lines.append(str(text_part))

        return "\n".join(lines), {"ocr_engine": "paddleocr", "line_count": len(lines)}

    def _vlm_extract(self, path: Path, data: bytes, **kwargs) -> tuple[str, dict[str, Any]]:
        """VLM-анализ через LLM API (OpenAI-совместимый)."""
        import base64
        from ..config import settings

        cfg = settings()
        profile = cfg.llm_profile

        if not profile.api_url:
            return "", {"vlm": False, "warning": "No LLM configured"}

        # Кодируем изображение
        ext = path.suffix.lower().replace(".", "")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp",
                "bmp": "bmp", "tiff": "tiff", "tif": "tiff"}.get(ext, "png")
        b64 = base64.b64encode(data).decode()

        # Промпт
        prompt = kwargs.get("prompt", self._default_vlm_prompt())

        try:
            import requests

            headers = {
                "Authorization": f"Bearer {profile.api_key}",
                "Content-Type": "application/json",
            }

            payload = {
                "model": profile.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/{mime};base64,{b64}",
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 4096,
            }

            resp = requests.post(
                f"{profile.api_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
            text = result["choices"][0]["message"]["content"]

            return text, {"vlm": True, "model": profile.model}

        except Exception as e:
            return "", {"vlm": False, "error": str(e)}

    def _default_vlm_prompt(self) -> str:
        return (
            "Проанализируй это изображение и извлеки ВСЕ текстовое содержимое. "
            "Если это таблица — воспроизведи её в формате markdown. "
            "Если это схема или чертеж — опиши все подписи, размеры и элементы. "
            "Если это рукописный текст — распознай его максимально точно. "
            "Отвечай на русском языке, если текст на русском."
        )

    def _get_image_info(self, path: Path, data: bytes) -> dict[str, Any]:
        """Извлекает базовую информацию об изображении."""
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(data))
            return {
                "width": img.width,
                "height": img.height,
                "format": img.format,
                "mode": img.mode,
                "size_bytes": len(data),
            }
        except ImportError:
            return {"size_bytes": len(data)}
