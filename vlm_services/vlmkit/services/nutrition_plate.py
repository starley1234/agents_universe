"""№7 «AI-нутрициолог "Фото-тарелка"».

Фото еды → блюда, порции, БЖУ и калории, совет по балансу.

Два решения, определяющих качество продукта:

1. **Калории считает код.** Модель называет продукт и оценивает массу, а
   килокалории выводятся из БЖУ по коэффициентам Этуотера (4/4/9). Иначе
   в дневнике накапливается арифметический шум, и вес не сходится.
2. **Оценка порции по фото неточна, и это сказано вслух.** Возвращается
   диапазон ±30%, а не одно «точное» число: без веса и с одного ракурса
   отличить 150 г риса от 220 г нельзя.

Медицинских советов сервис не даёт — только состав и общие рекомендации
по балансу.
"""

from __future__ import annotations

from typing import Any

from ..core import Service, as_float, bullets, register, table
from ..images import ImageRef

KCAL_PROTEIN = 4.0
KCAL_CARB = 4.0
KCAL_FAT = 9.0
PORTION_UNCERTAINTY = 0.30  # ±30% — честная погрешность оценки по фото

# Ориентиры дневной нормы (взрослый, 2000 ккал). Для контекста, не для диагноза.
DAILY = {"kcal": 2000, "protein_g": 75, "fat_g": 67, "carb_g": 250, "fiber_g": 25,
         "sodium_mg": 2300, "sugar_g": 50}


@register
class NutritionPlateService(Service):
    slug = "nutrition-plate"
    title = "AI-нутрициолог «Фото-тарелка»"
    summary = "Фото еды → блюда, порции, БЖУ и калории с честной погрешностью."
    tags = ("health", "b2c")
    max_images = 3

    system = (
        "Ты нутрициолог. На фото — приём пищи. Перечисли блюда и продукты, "
        "оцени массу каждого в граммах (ориентируйся на посуду и столовые "
        "приборы как на масштаб) и укажи белки, жиры, углеводы и клетчатку "
        "на 100 г продукта. Отметь способ приготовления и заметное количество "
        "масла или соуса. Не давай медицинских рекомендаций."
    )
    schema = {
        "meal_type": "",
        "items": [{"name": "", "grams": 0, "cooking": "",
                   "per100g": {"protein_g": 0.0, "fat_g": 0.0, "carb_g": 0.0,
                               "fiber_g": 0.0, "sodium_mg": 0.0, "sugar_g": 0.0},
                   "confidence": 0.0}],
        "visible_oil": "",
        "plate_scale_known": False,
        "notes": [""],
    }

    def analyze(self, images: list[ImageRef], goal: str = "",
                daily_kcal: float = 0.0, **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Оцени приём пищи по {len(images)} фото."
            + (f" Цель пользователя: {goal}." if goal else ""),
            images,
        )

        items, warnings = _clean_items(data.get("items"))
        totals = _totals(items)
        target_kcal = as_float(daily_kcal) or DAILY["kcal"]

        if not items:
            warnings.append("на фото не распознано ни одного продукта")
        if not data.get("plate_scale_known", False):
            warnings.append("масштаб не определён (нет прибора или посуды известного "
                            "размера в кадре) — масса порций оценена грубо")

        oil = str(data.get("visible_oil") or "").strip()
        advice = _advice(totals, items, oil, target_kcal)

        low_conf = [i["name"] for i in items if i["confidence"] < 0.5]
        if low_conf:
            warnings.append("низкая уверенность в распознавании: " + ", ".join(low_conf))

        return {
            "meal_type": str(data.get("meal_type") or ""),
            "items": items,
            "totals": totals,
            "kcal_range": [int(totals["kcal"] * (1 - PORTION_UNCERTAINTY)),
                           int(totals["kcal"] * (1 + PORTION_UNCERTAINTY))],
            "uncertainty_pct": int(PORTION_UNCERTAINTY * 100),
            "daily_share_pct": round(totals["kcal"] / target_kcal * 100, 1)
                               if target_kcal else 0.0,
            "target_kcal": target_kcal,
            "balance": _balance(totals),
            "advice": advice,
            "disclaimer": "Оценка по фотографии, не медицинская рекомендация. "
                          "Точность массы порций ±30%.",
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        t = data["totals"]
        lo, hi = data["kcal_range"]
        lines = [f"# Тарелка: {data['meal_type'] or 'приём пищи'}", ""]
        lines.append(f"**{t['kcal']} ккал** (вероятный диапазон {lo}–{hi}) — "
                     f"{data['daily_share_pct']}% дневной нормы "
                     f"{int(data['target_kcal'])} ккал.")
        lines.append("")
        lines.append(f"Б {t['protein_g']} г · Ж {t['fat_g']} г · У {t['carb_g']} г · "
                     f"клетчатка {t['fiber_g']} г")
        lines.append("")
        if data["items"]:
            lines.append("## Состав")
            lines += table(["Продукт", "Масса", "Ккал", "Б", "Ж", "У"],
                           [[i["name"], f"{i['grams']} г", i["kcal"], i["protein_g"],
                             i["fat_g"], i["carb_g"]] for i in data["items"]])
            lines.append("")
        b = data["balance"]
        lines.append("## Баланс")
        lines.append(f"Белки {b['protein_pct']}% · жиры {b['fat_pct']}% · "
                     f"углеводы {b['carb_pct']}% от калорийности.")
        lines.append("")
        if data["advice"]:
            lines.append("## Советы")
            lines += bullets(data["advice"])
            lines.append("")
        lines.append(f"_{data['disclaimer']}_")
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "meal_type": "обед",
            "items": [
                {"name": "Куриная грудка жареная", "grams": 180, "cooking": "жарка на масле",
                 "per100g": {"protein_g": 29.0, "fat_g": 9.0, "carb_g": 0.0,
                             "fiber_g": 0.0, "sodium_mg": 380.0, "sugar_g": 0.0},
                 "confidence": 0.88},
                {"name": "Рис отварной", "grams": 220, "cooking": "варка",
                 "per100g": {"protein_g": 2.7, "fat_g": 0.3, "carb_g": 28.0,
                             "fiber_g": 0.4, "sodium_mg": 1.0, "sugar_g": 0.1},
                 "confidence": 0.82},
                {"name": "Салат из огурцов с маслом", "grams": 120, "cooking": "свежий",
                 "per100g": {"protein_g": 0.8, "fat_g": 7.0, "carb_g": 2.5,
                             "fiber_g": 0.8, "sodium_mg": 210.0, "sugar_g": 1.8},
                 "confidence": 0.7},
            ],
            "visible_oil": "заметное количество масла на курице и в салате",
            "plate_scale_known": True,
            "notes": ["порция крупная"],
        }
        return {"images": [demo_image("lunch.jpg", scene)],
                "params": {"goal": "снижение веса", "daily_kcal": 1800}}


def _clean_items(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        grams = as_float(it.get("grams"))
        if not name:
            continue
        if grams <= 0:
            warnings.append(f"«{name}»: масса не оценена, продукт не учтён в итогах")
            continue
        if grams > 2000:
            warnings.append(f"«{name}»: {grams:.0f} г выглядит неправдоподобно "
                            "для одной порции")
        p100 = it.get("per100g") or {}
        prot = as_float(p100.get("protein_g")) * grams / 100
        fat = as_float(p100.get("fat_g")) * grams / 100
        carb = as_float(p100.get("carb_g")) * grams / 100
        fiber = as_float(p100.get("fiber_g")) * grams / 100
        sodium = as_float(p100.get("sodium_mg")) * grams / 100
        sugar = as_float(p100.get("sugar_g")) * grams / 100
        # Килокалории считаем сами по Этуотеру, а не берём у модели.
        kcal = prot * KCAL_PROTEIN + carb * KCAL_CARB + fat * KCAL_FAT
        items.append({
            "name": name, "grams": round(grams), "cooking": str(it.get("cooking") or ""),
            "protein_g": round(prot, 1), "fat_g": round(fat, 1), "carb_g": round(carb, 1),
            "fiber_g": round(fiber, 1), "sodium_mg": round(sodium), "sugar_g": round(sugar, 1),
            "kcal": round(kcal),
            "confidence": round(min(1.0, max(0.0, as_float(it.get("confidence"), 0.6))), 2),
        })
    return items, warnings


def _totals(items: list[dict]) -> dict[str, float]:
    keys = ("protein_g", "fat_g", "carb_g", "fiber_g", "sugar_g")
    out = {k: round(sum(i[k] for i in items), 1) for k in keys}
    out["sodium_mg"] = round(sum(i["sodium_mg"] for i in items))
    out["kcal"] = round(sum(i["kcal"] for i in items))
    out["grams"] = round(sum(i["grams"] for i in items))
    return out


def _balance(t: dict[str, float]) -> dict[str, float]:
    kcal = t["kcal"] or 1
    return {
        "protein_pct": round(t["protein_g"] * KCAL_PROTEIN / kcal * 100, 1),
        "fat_pct": round(t["fat_g"] * KCAL_FAT / kcal * 100, 1),
        "carb_pct": round(t["carb_g"] * KCAL_CARB / kcal * 100, 1),
    }


def _advice(t: dict[str, float], items: list[dict], oil: str,
            target_kcal: float) -> list[str]:
    """Советы по измеренному составу, без медицинских утверждений."""
    out: list[str] = []
    b = _balance(t)
    if b["fat_pct"] > 40:
        out.append(f"жиры дают {b['fat_pct']}% калорийности — многовато; "
                   "часть масла при готовке можно убрать")
    if oil and any(w in oil.lower() for w in ("заметн", "много", "обильн")):
        out.append("на фото видно много масла — при жарке достаточно чайной ложки "
                   "или используйте посуду с покрытием")
    if t["fiber_g"] < 5:
        out.append(f"клетчатки всего {t['fiber_g']} г — добавьте овощи или зелень "
                   "к этому приёму пищи")
    if b["protein_pct"] < 15:
        out.append("мало белка: добавьте творог, яйцо, рыбу или бобовые")
    if t["sodium_mg"] > DAILY["sodium_mg"] * 0.5:
        out.append(f"натрия {t['sodium_mg']} мг — больше половины дневного "
                   "ориентира за один приём")
    if target_kcal and t["kcal"] > target_kcal * 0.5:
        out.append(f"этот приём — {round(t['kcal'] / target_kcal * 100)}% дневной "
                   "нормы; учтите при планировании остальных")
    if not out:
        out.append("состав сбалансирован, заметных перекосов не видно")
    return out
