"""№1 «Автоматический менеджер карточек товаров» (PIM AI).

1–3 фото товара → категория, атрибуты, SEO-заголовок и описание под
конкретный маркетплейс.

Разделение труда: VLM смотрит и называет (что за товар, цвет, материал),
а код следит за тем, что нарушает выдачу маркетплейса, — длина заголовка,
дубли ключевых слов, запрещённые обещания, заполненность обязательных
атрибутов категории. Карточку с заголовком в 200 символов Wildberries
просто обрежет, и селлер потеряет показы.
"""

from __future__ import annotations

import re
from typing import Any

from ..core import Service, as_float, bullets, register, table
from ..images import ImageRef

# Ограничения площадок. Держим в коде: они меняются, и это должно быть
# видно в одном месте, а не размазано по промптам.
MARKETPLACES = {
    "wildberries": {"title_max": 60, "desc_max": 2000, "required":
                    ("brand", "color", "material", "size"), "bullets": 5},
    "ozon": {"title_max": 200, "desc_max": 6000, "required":
             ("brand", "color", "material"), "bullets": 8},
    "amazon": {"title_max": 200, "desc_max": 2000, "required":
               ("brand", "color", "material"), "bullets": 5},
}

# Обещания, за которые площадки снимают карточку с публикации.
BANNED = ("лучший", "№1", "номер один", "самый дешёвый", "гарантия 100",
          "вылечивает", "лечит", "чудо", "best in the world", "cheapest")

STOP_WORDS = {"и", "в", "на", "для", "с", "из", "по", "the", "a", "of", "for", "with"}


@register
class PimCardService(Service):
    slug = "pim-cards"
    title = "Менеджер карточек товаров (PIM AI)"
    summary = "1–3 фото товара → атрибуты, SEO-заголовок и описание под маркетплейс."
    tags = ("ecommerce", "seo")
    min_images = 1
    max_images = 3

    system = (
        "Ты товаровед-контентщик маркетплейса. По фотографиям определи, что это "
        "за товар, и заполни карточку. Пиши только то, что видно на фото или "
        "однозначно следует из вида товара. Не выдумывай бренд, если логотипа "
        "не видно, и не обещай свойств, которых не можешь проверить глазами."
    )
    schema = {
        "category": "", "subcategory": "", "product_type": "",
        "attributes": {"brand": "", "color": "", "material": "", "size": "",
                       "style": "", "pattern": ""},
        "features": [""],
        "keywords": [""],
        "title": "",
        "description": "",
        "defects": [""],
        "confidence": 0.0,
    }

    def analyze(self, images: list[ImageRef], marketplace: str = "wildberries",
                brand: str = "", **params: Any) -> dict[str, Any]:
        mp = marketplace.lower()
        rules = MARKETPLACES.get(mp)
        if rules is None:
            raise_unknown(mp)
        data = self.ask(
            f"Товар на {len(images)} фото. Площадка: {marketplace}. "
            f"Заполни карточку. Заголовок не длиннее {rules['title_max']} символов.",
            images,
        )

        attrs = {k: str(v or "").strip() for k, v in (data.get("attributes") or {}).items()}
        if brand:  # бренд знает селлер, а не модель — его слово важнее
            attrs["brand"] = brand
        features = _clean_list(data.get("features"), limit=rules["bullets"])
        keywords = _dedup_keywords(data.get("keywords"))

        title = _clean_text(data.get("title"))
        title, title_note = _fit_title(title, rules["title_max"], attrs, data)
        description = _clean_text(data.get("description"))[: rules["desc_max"]]

        warnings: list[str] = []
        if title_note:
            warnings.append(title_note)

        missing = [f for f in rules["required"] if not attrs.get(f)]
        if missing:
            warnings.append("не определены обязательные атрибуты: " + ", ".join(missing))

        banned_hits = _find_banned(f"{title} {description}")
        if banned_hits:
            warnings.append("запрещённые формулировки (площадка снимет карточку): "
                            + ", ".join(banned_hits))

        if len(images) < 2:
            warnings.append("одно фото: атрибуты вроде материала и размера "
                            "определяются ненадёжно, добавьте ракурсы")

        defects = _clean_list(data.get("defects"))
        if defects:
            warnings.append("на фото видны дефекты — снимите товар заново: "
                            + "; ".join(defects))

        confidence = round(_confidence(data, attrs, rules, len(images)), 2)
        return {
            "marketplace": mp,
            "category": _clean_text(data.get("category")),
            "subcategory": _clean_text(data.get("subcategory")),
            "product_type": _clean_text(data.get("product_type")),
            "attributes": attrs,
            "features": features,
            "keywords": keywords,
            "title": title,
            "title_len": len(title),
            "description": description,
            "defects": defects,
            "missing_required": missing,
            "confidence": confidence,
            "ready_to_publish": not missing and not banned_hits and confidence >= 0.6,
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        rules = MARKETPLACES[data["marketplace"]]
        lines = [f"# Карточка товара · {data['marketplace']}", ""]
        lines.append(f"**{data['title']}**  \n`{data['title_len']}/{rules['title_max']} символов`")
        lines.append("")
        lines.append(f"Категория: {data['category']} → {data['subcategory']} "
                     f"({data['product_type']})")
        lines.append(f"Готовность к публикации: "
                     f"{'да' if data['ready_to_publish'] else 'нет'} "
                     f"(уверенность {data['confidence']:.0%})")
        lines.append("")
        rows = [[k, v or "—"] for k, v in data["attributes"].items() if k]
        if rows:
            lines.append("## Атрибуты")
            lines += table(["Атрибут", "Значение"], rows)
            lines.append("")
        if data["features"]:
            lines.append("## Преимущества")
            lines += bullets(data["features"])
            lines.append("")
        if data["description"]:
            lines.append("## Описание")
            lines.append(data["description"])
            lines.append("")
        if data["keywords"]:
            lines.append("## Ключевые слова")
            lines.append(", ".join(data["keywords"]))
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "category": "Обувь", "subcategory": "Кроссовки",
            "product_type": "Кроссовки беговые",
            "attributes": {"brand": "", "color": "тёмно-синий с белой подошвой",
                           "material": "текстильный верх, резиновая подошва",
                           "size": "", "style": "спортивный", "pattern": "однотонный"},
            "features": ["дышащий сетчатый верх", "амортизирующая подошва",
                         "светоотражающие вставки"],
            "keywords": ["кроссовки", "беговые кроссовки", "кроссовки мужские",
                         "кроссовки", "спортивная обувь"],
            "title": "Кроссовки беговые мужские текстильные тёмно-синие с амортизацией",
            "description": "Лёгкие беговые кроссовки с дышащим верхом.",
            "defects": [], "confidence": 0.82,
        }
        return {
            "images": [demo_image("shoe-front.jpg", scene),
                       demo_image("shoe-side.jpg", {})],
            "params": {"marketplace": "wildberries"},
        }


def raise_unknown(mp: str) -> None:
    from ..core import ServiceError

    raise ServiceError(f"неизвестная площадка {mp!r}. Есть: {', '.join(MARKETPLACES)}")


def _clean_text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _clean_list(v: Any, limit: int = 20) -> list[str]:
    out = []
    for it in v or []:
        s = _clean_text(it)
        if s and s not in out:
            out.append(s)
    return out[:limit]


def _dedup_keywords(v: Any, limit: int = 25) -> list[str]:
    """Дубли ключей режут релевантность и считаются спамом."""
    seen: set[str] = set()
    out: list[str] = []
    for it in v or []:
        s = _clean_text(it).lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out[:limit]


def _fit_title(title: str, limit: int, attrs: dict, data: dict) -> tuple[str, str]:
    """Уложить заголовок в лимит площадки, не обрубая слово посередине."""
    if not title:
        parts = [data.get("product_type") or "", attrs.get("color", ""),
                 attrs.get("material", "")]
        title = " ".join(p for p in parts if p).strip()
        if not title:
            return "", "модель не смогла составить заголовок"
    if len(title) <= limit:
        return title, ""
    cut = title[: limit + 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;-"), (f"заголовок обрезан до {limit} символов "
                                f"(было {len(title)}) — иначе площадка обрежет сама")


def _find_banned(text: str) -> list[str]:
    low = text.lower()
    return [w for w in BANNED if w in low]


def _confidence(data: dict, attrs: dict, rules: dict, n_images: int) -> float:
    """Своя оценка уверенности: модель склонна её завышать."""
    own = as_float(data.get("confidence"), 0.5)
    filled = sum(1 for f in rules["required"] if attrs.get(f)) / max(1, len(rules["required"]))
    photos = min(1.0, n_images / 2)
    return max(0.0, min(1.0, 0.4 * own + 0.4 * filled + 0.2 * photos))
