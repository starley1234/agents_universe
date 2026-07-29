"""Работа с изображениями: загрузка, проверка, нормализация, data-URI.

Три вещи, ради которых существует этот модуль:

1. **Деньги.** Провайдеры берут плату за пиксели. Фото с телефона на 12 Мп
   стоит в разы дороже того же кадра, ужатого до 1024 px по длинной
   стороне, а модель на нём не видит ничего нового. Уменьшаем до отправки.
2. **Безопасность.** Файл из интернета нельзя слепо декодировать: проверяем
   размер и формат до того, как отдать его в декодер.
3. **Воспроизводимость.** У каждого изображения есть sha256 — по нему
   кешируются ответы и сверяются прогоны.

Pillow необязателен: без него сервис работает, но не может уменьшать
картинки и читает размеры из заголовков сам.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - зависит от окружения
    from PIL import Image, ImageOps

    HAVE_PILLOW = True
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    HAVE_PILLOW = False

MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
DATA_URI = re.compile(r"^data:(image/[a-z+]+);base64,(.+)$", re.I | re.S)


class ImageError(ValueError):
    """Некорректное изображение: битое, слишком большое или чужого формата."""


@dataclass
class ImageRef:
    """Одно изображение внутри запроса.

    `scene` — служебное поле для оффлайн-режима: описание того, что
    «видно» на картинке. Реальный провайдер его игнорирует, а заглушка
    использует, чтобы тесты проверяли логику сервиса, а не фантазию модели.
    """

    data: bytes
    fmt: str = "png"
    width: int = 0
    height: int = 0
    name: str = ""
    scene: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size_kb(self) -> float:
        return round(len(self.data) / 1024, 1)

    @property
    def mime(self) -> str:
        return MIME.get(self.fmt, "image/png")

    def to_data_uri(self) -> str:
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode()

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "format": self.fmt, "width": self.width,
                "height": self.height, "size_kb": self.size_kb, "sha256": self.sha256[:16]}


# --- разбор заголовков без Pillow ------------------------------------------
def probe(data: bytes) -> tuple[str, int, int]:
    """Формат и размеры по сигнатуре файла. Работает без Pillow."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return "jpeg", w, h
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg
        return "jpeg", 0, 0
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X" and len(data) >= 30:
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return "webp", w, h
        return "webp", 0, 0
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return "gif", w, h
    raise ImageError("неподдерживаемый формат: ожидается PNG, JPEG, WEBP или GIF")


# --- загрузка --------------------------------------------------------------
def load(src: Any, name: str = "", scene: dict | None = None,
         max_mb: float = 20.0) -> ImageRef:
    """Принять изображение из пути, байтов, base64 или data-URI."""
    if isinstance(src, ImageRef):
        return src
    if isinstance(src, dict):  # форма из JSON-запроса
        scene = src.get("scene", scene)
        name = src.get("name", name)
        src = src.get("data") or src.get("path") or src.get("url", "")

    if isinstance(src, (str, Path)):
        text = str(src)
        m = DATA_URI.match(text.strip())
        if m:
            data = _b64(m.group(2))
        elif text.startswith(("http://", "https://")):
            raise ImageError(
                "загрузка по URL отключена: сервис не ходит в сеть за картинками "
                "(SSRF). Скачайте файл и передайте байты или data-URI")
        else:
            p = Path(text)
            if not p.is_file():
                raise ImageError(f"файл не найден: {p}")
            data = p.read_bytes()
            name = name or p.name
    elif isinstance(src, (bytes, bytearray)):
        data = bytes(src)
    else:
        raise ImageError(f"не могу прочитать изображение из {type(src).__name__}")

    if not data:
        raise ImageError("пустое изображение")
    limit = int(max_mb * 1024 * 1024)
    if len(data) > limit:
        raise ImageError(f"файл больше {max_mb} МБ ({len(data) / 1048576:.1f} МБ)")

    fmt, w, h = probe(data)
    return ImageRef(data=data, fmt=fmt, width=w, height=h, name=name, scene=dict(scene or {}))


def _b64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError(f"некорректный base64: {exc}") from None


def load_many(items: Any, max_images: int = 8, max_mb: float = 20.0) -> list[ImageRef]:
    if items is None:
        return []
    if isinstance(items, (str, bytes, bytearray, Path, ImageRef, dict)):
        items = [items]
    if len(items) > max_images:
        raise ImageError(f"слишком много изображений: {len(items)} > {max_images}")
    return [load(it, max_mb=max_mb) for it in items]


# --- нормализация ----------------------------------------------------------
def normalize(img: ImageRef, max_side: int = 1024, quality: int = 85) -> ImageRef:
    """Развернуть по EXIF и ужать до `max_side` по длинной стороне.

    Без Pillow возвращаем как есть: лучше отправить крупный кадр, чем
    уронить запрос.
    """
    if not HAVE_PILLOW or max_side <= 0:
        return img
    try:
        with Image.open(io.BytesIO(img.data)) as im:
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            longest = max(w, h)
            if longest <= max_side and img.fmt in ("png", "jpeg", "webp"):
                return ImageRef(img.data, img.fmt, w, h, img.name, img.scene)
            if longest > max_side:
                scale = max_side / longest
                im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return ImageRef(buf.getvalue(), "jpeg", im.width, im.height, img.name, img.scene)
    except Exception:  # noqa: BLE001 — битый кадр не должен ронять сервис
        return img


def average_color(img: ImageRef, box: tuple[float, float, float, float] | None = None
                  ) -> tuple[int, int, int] | None:
    """Средний цвет области в долях кадра (x0, y0, x1, y1). Нужен для UX-аудита."""
    if not HAVE_PILLOW:
        return None
    try:
        with Image.open(io.BytesIO(img.data)) as im:
            im = im.convert("RGB")
            if box:
                w, h = im.size
                x0, y0, x1, y1 = box
                crop = (max(0, int(x0 * w)), max(0, int(y0 * h)),
                        min(w, max(1, int(x1 * w))), min(h, max(1, int(y1 * h))))
                if crop[2] <= crop[0] or crop[3] <= crop[1]:
                    return None
                im = im.crop(crop)
            im = im.resize((1, 1), Image.LANCZOS)
            return im.getpixel((0, 0))[:3]
    except Exception:  # noqa: BLE001
        return None
