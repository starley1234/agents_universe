"""Изображения: разбор форматов, лимиты, нормализация, безопасность."""

from __future__ import annotations

import base64
import io

import pytest

from vlmkit.demo import png_bytes
from vlmkit.images import (HAVE_PILLOW, ImageError, ImageRef, average_color, load,
                           load_many, normalize, probe)

pillow = pytest.mark.skipif(not HAVE_PILLOW, reason="нет Pillow")


def jpeg_bytes(w: int = 100, h: int = 60, rgb=(10, 20, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, "JPEG")
    return buf.getvalue()


def test_probe_png():
    assert probe(png_bytes(120, 80)) == ("png", 120, 80)


@pillow
def test_probe_jpeg():
    fmt, w, h = probe(jpeg_bytes(160, 90))
    assert (fmt, w, h) == ("jpeg", 160, 90)


def test_probe_rejects_unknown_format():
    with pytest.raises(ImageError, match="неподдерживаемый формат"):
        probe(b"not an image at all, just text")


def test_load_from_bytes_and_data_uri():
    raw = png_bytes(32, 32)
    a = load(raw, name="a.png")
    b = load(a.to_data_uri())
    assert a.sha256 == b.sha256
    assert a.width == 32 and a.mime == "image/png"


def test_load_from_path(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(png_bytes(20, 10))
    img = load(str(p))
    assert img.name == "pic.png" and img.width == 20


def test_load_missing_file():
    with pytest.raises(ImageError, match="не найден"):
        load("/nope/nope.png")


def test_load_rejects_remote_url():
    """Скачивание по URL — это SSRF: сервис ходил бы во внутреннюю сеть."""
    with pytest.raises(ImageError, match="SSRF"):
        load("https://example.com/photo.jpg")


def test_load_rejects_oversized_file():
    big = png_bytes(600, 600)
    with pytest.raises(ImageError, match="больше"):
        load(big, max_mb=0.0001)


def test_load_rejects_bad_base64():
    with pytest.raises(ImageError, match="base64"):
        load("data:image/png;base64,!!!не-base64!!!")


def test_load_rejects_empty():
    with pytest.raises(ImageError, match="пустое"):
        load(b"")


def test_load_many_respects_limit():
    imgs = [png_bytes(8, 8) for _ in range(3)]
    assert len(load_many(imgs, max_images=3)) == 3
    with pytest.raises(ImageError, match="слишком много"):
        load_many(imgs, max_images=2)


def test_load_many_accepts_single():
    assert len(load_many(png_bytes(8, 8))) == 1
    assert load_many(None) == []


def test_load_dict_form_keeps_scene():
    img = load({"data": load(png_bytes(8, 8)).to_data_uri(), "name": "x.png",
                "scene": {"k": 1}})
    assert img.scene == {"k": 1} and img.name == "x.png"


@pillow
def test_normalize_shrinks_large_image():
    """Главная экономия сервиса: телефонное фото ужимается до лимита."""
    big = load(jpeg_bytes(4000, 3000))
    small = normalize(big, max_side=1024)
    assert max(small.width, small.height) == 1024
    assert len(small.data) < len(big.data) / 2


@pillow
def test_normalize_keeps_small_image_untouched():
    img = load(jpeg_bytes(200, 100))
    same = normalize(img, max_side=1024)
    assert (same.width, same.height) == (200, 100)


def test_normalize_survives_broken_data():
    """Битый кадр не должен ронять запрос — возвращаем как есть."""
    broken = ImageRef(data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, fmt="png",
                      width=10, height=10)
    assert normalize(broken, max_side=64) is not None


def test_data_uri_roundtrip():
    img = load(png_bytes(16, 16))
    uri = img.to_data_uri()
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == img.data


def test_info_fields():
    info = load(png_bytes(24, 12), name="q.png").info()
    assert info["width"] == 24 and info["format"] == "png"
    assert len(info["sha256"]) == 16


@pillow
def test_average_color_reads_region():
    img = load(jpeg_bytes(50, 50, (200, 30, 40)))
    r, g, b = average_color(img)
    assert r > 150 and g < 90
