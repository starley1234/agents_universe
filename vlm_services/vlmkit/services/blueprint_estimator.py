"""№4 «Автоматический сметчик по чертежам».

Фото или скан плана → площади помещений → ведомость материалов с
запасом на подрезку и стоимостью.

Вся арифметика — в коде. Модель читает с чертежа только размеры и типы
помещений; расход плитки, краски и кабеля считается по нормам, а не
«прикидывается» словами. Ошибка модели в умножении здесь стоила бы
клиенту закупки лишних поддонов плитки.
"""

from __future__ import annotations

import math
from typing import Any

from ..core import Service, ServiceError, as_float, register, table
from ..images import ImageRef

# Нормы расхода. Вынесены в код: их правит сметчик, а не промпт.
WASTE = {"tile": 0.10, "laminate": 0.07, "paint": 0.05, "wallpaper": 0.12,
         "screed": 0.05, "plinth": 0.05}
PAINT_L_PER_M2 = 0.12          # один слой, гладкая стена
SCREED_KG_PER_M2_CM = 18.0     # сухая смесь на 1 см толщины
DEFAULT_HEIGHT_M = 2.7
WET_ROOMS = ("санузел", "ванная", "туалет", "кухня", "bathroom", "kitchen", "wc")


@register
class BlueprintEstimatorService(Service):
    slug = "blueprint-estimator"
    title = "Автоматический сметчик по чертежам"
    summary = "План помещения → площади → ведомость материалов и смета."
    tags = ("construction", "estimation")
    max_images = 4

    system = (
        "Ты инженер-сметчик. На изображении — план помещения с размерами. "
        "Для каждой комнаты сними название, длину и ширину в метрах, "
        "количество дверей и окон. Если размер подписан на чертеже — бери "
        "подписанный, не измеряй по картинке. Если размеры не читаются, "
        "поставь 0 и отметь это, а не угадывай."
    )
    schema = {
        "unit": "m",
        "scale_note": "",
        "rooms": [{"name": "", "length_m": 0.0, "width_m": 0.0, "height_m": 0.0,
                   "doors": 0, "windows": 0, "wet": False}],
        "unreadable": [""],
        "total_area_m2": 0.0,
    }

    def analyze(self, images: list[ImageRef], works: list[str] | None = None,
                prices: dict[str, float] | None = None,
                ceiling_height_m: float = DEFAULT_HEIGHT_M,
                paint_coats: int = 2, screed_cm: float = 5.0,
                **params: Any) -> dict[str, Any]:
        works = [w.lower() for w in (works or ["tile", "paint", "laminate"])]
        prices = {k.lower(): as_float(v) for k, v in (prices or {}).items()}
        if ceiling_height_m <= 0:
            raise ServiceError("высота потолка должна быть больше нуля")

        data = self.ask(
            f"Сними размеры помещений с плана ({len(images)} изображение(й)).", images)

        rooms, warnings = _clean_rooms(data.get("rooms"), ceiling_height_m)
        if not rooms:
            warnings.append("на плане не распознано ни одного помещения с размерами")

        for u in data.get("unreadable") or []:
            if str(u).strip():
                warnings.append(f"не читается на чертеже: {u}")

        total_floor = round(sum(r["floor_m2"] for r in rooms), 2)
        total_wall = round(sum(r["wall_m2"] for r in rooms), 2)

        materials = _materials(rooms, works, paint_coats, screed_cm)
        for m in materials:
            price = prices.get(m["key"], 0.0)
            m["unit_price"] = price
            m["cost"] = round(m["qty_with_waste"] * price, 2)

        total_cost = round(sum(m["cost"] for m in materials), 2)
        if not prices:
            warnings.append("цены не заданы — посчитаны только объёмы, стоимость нулевая")

        return {
            "rooms": rooms,
            "total_floor_m2": total_floor,
            "total_wall_m2": total_wall,
            "ceiling_height_m": ceiling_height_m,
            "materials": materials,
            "total_cost": total_cost,
            "works": works,
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = ["# Смета по чертежу", ""]
        lines.append(f"Помещений: {len(data['rooms'])}, пол: "
                     f"**{data['total_floor_m2']} м²**, стены: {data['total_wall_m2']} м² "
                     f"(высота {data['ceiling_height_m']} м).")
        if data["total_cost"]:
            lines.append(f"Итого материалов на **{data['total_cost']:,.0f}**.".replace(",", " "))
        lines.append("")
        if data["rooms"]:
            lines.append("## Помещения")
            lines += table(["Помещение", "Д×Ш, м", "Пол, м²", "Стены, м²", "Влажное"],
                           [[r["name"], f"{r['length_m']}×{r['width_m']}", r["floor_m2"],
                             r["wall_m2"], "да" if r["wet"] else "—"]
                            for r in data["rooms"]])
            lines.append("")
        if data["materials"]:
            lines.append("## Ведомость материалов")
            lines += table(["Материал", "Объём", "С запасом", "Ед.", "Цена", "Стоимость"],
                           [[m["title"], m["qty"], m["qty_with_waste"], m["unit"],
                             m["unit_price"] or "—", m["cost"] or "—"]
                            for m in data["materials"]])
            lines.append("")
            lines.append("_Запас на подрезку и брак уже включён в колонку «С запасом»._")
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "unit": "m", "scale_note": "размеры подписаны на плане",
            "rooms": [
                {"name": "Гостиная", "length_m": 5.4, "width_m": 3.8, "height_m": 2.7,
                 "doors": 1, "windows": 2, "wet": False},
                {"name": "Спальня", "length_m": 4.2, "width_m": 3.2, "height_m": 2.7,
                 "doors": 1, "windows": 1, "wet": False},
                {"name": "Кухня", "length_m": 3.6, "width_m": 3.0, "height_m": 2.7,
                 "doors": 1, "windows": 1, "wet": True},
                {"name": "Санузел", "length_m": 2.2, "width_m": 1.8, "height_m": 2.7,
                 "doors": 1, "windows": 0, "wet": True},
            ],
            "unreadable": [], "total_area_m2": 48.5,
        }
        return {
            "images": [demo_image("plan.png", scene)],
            "params": {"works": ["tile", "paint", "laminate", "plinth"],
                       "prices": {"tile": 1200, "paint": 480, "laminate": 950,
                                  "plinth": 210}},
        }


def _clean_rooms(raw: Any, default_h: float) -> tuple[list[dict[str, Any]], list[str]]:
    rooms: list[dict[str, Any]] = []
    warnings: list[str] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "помещение").strip()
        length, width = as_float(r.get("length_m")), as_float(r.get("width_m"))
        if length <= 0 or width <= 0:
            warnings.append(f"«{name}»: размеры не распознаны, помещение пропущено")
            continue
        if length * width > 400:
            warnings.append(f"«{name}»: площадь {length * width:.0f} м² выглядит "
                            "неправдоподобно — проверьте единицы измерения на чертеже")
        height = as_float(r.get("height_m")) or default_h
        doors, windows = max(0, int(as_float(r.get("doors")))), max(0, int(as_float(r.get("windows"))))
        floor = round(length * width, 2)
        # проёмы вычитаем по типовым размерам: дверь 1.6 м², окно 1.8 м²
        gross_wall = 2 * (length + width) * height
        wall = round(max(0.0, gross_wall - doors * 1.6 - windows * 1.8), 2)
        wet = bool(r.get("wet")) or any(w in name.lower() for w in WET_ROOMS)
        rooms.append({"name": name, "length_m": round(length, 2), "width_m": round(width, 2),
                      "height_m": round(height, 2), "floor_m2": floor, "wall_m2": wall,
                      "perimeter_m": round(2 * (length + width), 2),
                      "doors": doors, "windows": windows, "wet": wet})
    return rooms, warnings


def _materials(rooms: list[dict], works: list[str], coats: int,
               screed_cm: float) -> list[dict[str, Any]]:
    """Объёмы по нормам. Мокрые зоны — плитка, сухие — ламинат."""
    out: list[dict[str, Any]] = []

    def add(key: str, title: str, qty: float, unit: str) -> None:
        if qty <= 0:
            return
        waste = WASTE.get(key, 0.05)
        out.append({"key": key, "title": title, "qty": round(qty, 2),
                    "qty_with_waste": _round_up(qty * (1 + waste), unit),
                    "unit": unit, "waste_pct": round(waste * 100)})

    wet_floor = sum(r["floor_m2"] for r in rooms if r["wet"])
    dry_floor = sum(r["floor_m2"] for r in rooms if not r["wet"])
    wet_wall = sum(r["wall_m2"] for r in rooms if r["wet"])
    dry_wall = sum(r["wall_m2"] for r in rooms if not r["wet"])
    perimeter = sum(r["perimeter_m"] - r["doors"] * 0.9 for r in rooms)

    if "tile" in works:
        add("tile", "Плитка (пол влажных зон)", wet_floor, "м²")
        add("tile", "Плитка (стены влажных зон)", wet_wall, "м²")
    if "laminate" in works:
        add("laminate", "Ламинат", dry_floor, "м²")
    if "paint" in works:
        add("paint", f"Краска ({coats} слоя)", dry_wall * PAINT_L_PER_M2 * max(1, coats), "л")
    if "wallpaper" in works:
        add("wallpaper", "Обои", dry_wall, "м²")
    if "screed" in works:
        add("screed", f"Стяжка {screed_cm} см",
            (wet_floor + dry_floor) * SCREED_KG_PER_M2_CM * screed_cm, "кг")
    if "plinth" in works:
        add("plinth", "Плинтус", perimeter, "м")
    return out


def _round_up(value: float, unit: str) -> float:
    """Материалы продают упаковками — дробные метры округляем вверх."""
    if unit in ("м²", "м"):
        return float(math.ceil(value * 10) / 10)
    return round(value, 2)
