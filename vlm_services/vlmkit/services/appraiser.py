"""№11 «AI-оценщик антиквариата и недвижимости».

Фото предмета или интерьера → состояние, стиль, диапазон стоимости.

Ключевое ограничение продукта, вынесенное в код: **по фотографии нельзя
отличить подлинник от хорошей копии**. Клеймо, патина и следы
инструмента подделываются, а на снимке не видно ни веса, ни звука, ни
люминесценции. Поэтому сервис даёт диапазон, а не число, и явно
разделяет два сценария: «если подлинник» и «если реплика». Ломбард,
выдавший деньги под уверенную оценку копии, — это судебный иск.

Диапазон считается кодом от базы сравнимых продаж с поправками на
состояние и полноту комплекта.
"""

from __future__ import annotations

from typing import Any

from ..core import Service, as_float, bullets, register, table
from ..images import ImageRef

# Множители к базовой цене по состоянию.
CONDITION = {
    "mint": (1.15, "как новый"),
    "excellent": (1.0, "отличное"),
    "good": (0.8, "хорошее"),
    "fair": (0.55, "удовлетворительное"),
    "poor": (0.3, "плохое"),
    "restoration": (0.2, "требует реставрации"),
}
# Влияние дефектов на цену: скол на фарфоре бьёт сильнее царапины на мебели.
DEFECT_IMPACT = {"скол": 0.25, "трещина": 0.35, "реставрация": 0.2, "потёртость": 0.08,
                 "царапина": 0.06, "утрата": 0.4, "ржавчина": 0.2, "плесень": 0.3,
                 "перекрас": 0.35, "замена деталей": 0.3}


@register
class AppraiserService(Service):
    slug = "appraiser"
    title = "AI-оценщик антиквариата и недвижимости"
    summary = "Фото предмета → состояние, стиль и диапазон стоимости с оговорками."
    tags = ("valuation", "marketplace")
    max_images = 6

    system = (
        "Ты оценщик антиквариата и предметов интерьера. Опиши предмет: тип, "
        "стиль, вероятный период, материалы, клейма и маркировки, состояние и "
        "все видимые дефекты. Отдельно перечисли признаки подлинности и "
        "признаки, указывающие на возможную копию или позднюю реплику. Никогда "
        "не утверждай подлинность по фотографии — только перечисляй признаки."
    )
    schema = {
        "item_type": "", "style": "", "period": "", "materials": [""],
        "marks": [""], "condition": "", "defects": [{"type": "", "where": "",
                                                     "severity": ""}],
        "authenticity_signs": [""], "replica_signs": [""],
        "completeness": "", "rarity": "", "notes": [""],
    }

    def analyze(self, images: list[ImageRef], comparables: list[dict] | None = None,
                category: str = "", currency: str = "RUB", **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Оцени предмет по {len(images)} фото."
            + (f" Категория: {category}." if category else ""),
            images,
        )

        condition_key = _condition_key(data.get("condition"))
        cond_mult, cond_title = CONDITION[condition_key]
        defects = _clean_defects(data.get("defects"))
        defect_mult = _defect_multiplier(defects)

        base_low, base_high, comps_used = _base_range(comparables)
        replica_signs = [str(s) for s in (data.get("replica_signs") or []) if s]
        auth_signs = [str(s) for s in (data.get("authenticity_signs") or []) if s]

        estimate: dict[str, Any] | None = None
        if base_low and base_high:
            mult = cond_mult * defect_mult
            estimate = {
                "if_authentic": [round(base_low * mult), round(base_high * mult)],
                # Реплика стоит долю от подлинника — обычно 5–15%.
                "if_replica": [round(base_low * mult * 0.05),
                               round(base_high * mult * 0.15)],
                "currency": currency,
                "condition_multiplier": round(cond_mult, 2),
                "defect_multiplier": round(defect_mult, 2),
                "comparables_used": comps_used,
            }

        warnings = ["Оценка по фотографии носит предварительный характер. "
                    "Подлинность, материал и возраст подтверждаются только "
                    "очным осмотром эксперта."]
        if len(images) < 3:
            warnings.append("мало ракурсов: нужны клеймо, оборот и общий вид — "
                            "минимум 3 фото")
        if not comps_used:
            warnings.append("база сравнимых продаж не передана — денежная оценка "
                            "не рассчитана, только описание")
        if replica_signs:
            warnings.append("есть признаки возможной реплики — денежный диапазон "
                            "смещается к нижней границе")
        if not data.get("marks"):
            warnings.append("клейма и маркировки не обнаружены или не сняты")

        return {
            "item_type": str(data.get("item_type") or ""),
            "style": str(data.get("style") or ""),
            "period": str(data.get("period") or ""),
            "materials": [str(m) for m in (data.get("materials") or []) if m],
            "marks": [str(m) for m in (data.get("marks") or []) if m],
            "condition": condition_key,
            "condition_title": cond_title,
            "defects": defects,
            "authenticity_signs": auth_signs,
            "replica_signs": replica_signs,
            "authenticity_verdict": _verdict(auth_signs, replica_signs),
            "completeness": str(data.get("completeness") or ""),
            "rarity": str(data.get("rarity") or ""),
            "estimate": estimate,
            "expert_review_recommended": bool(replica_signs) or len(images) < 3,
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = [f"# Оценка: {data['item_type'] or 'предмет'}", ""]
        meta = [x for x in (data["style"], data["period"],
                            ", ".join(data["materials"])) if x]
        if meta:
            lines.append(" · ".join(meta))
        lines.append(f"Состояние: **{data['condition_title']}**. "
                     f"Подлинность: {data['authenticity_verdict']}.")
        lines.append("")
        est = data["estimate"]
        if est:
            cur = est["currency"]
            lines.append("## Диапазон стоимости")
            lines += table(["Сценарий", "От", "До"],
                           [["Если подлинник", f"{est['if_authentic'][0]:,} {cur}".replace(",", " "),
                             f"{est['if_authentic'][1]:,} {cur}".replace(",", " ")],
                            ["Если реплика", f"{est['if_replica'][0]:,} {cur}".replace(",", " "),
                             f"{est['if_replica'][1]:,} {cur}".replace(",", " ")]])
            lines.append("")
            lines.append(f"_Поправки: состояние ×{est['condition_multiplier']}, "
                         f"дефекты ×{est['defect_multiplier']}, "
                         f"сравнимых продаж: {est['comparables_used']}._")
            lines.append("")
        if data["defects"]:
            lines.append("## Дефекты")
            lines += table(["Дефект", "Где", "Критичность"],
                           [[d["type"], d["where"] or "—", d["severity"]]
                            for d in data["defects"]])
            lines.append("")
        if data["marks"]:
            lines.append("## Клейма и маркировки")
            lines += bullets(data["marks"])
            lines.append("")
        if data["authenticity_signs"]:
            lines.append("## Признаки подлинности")
            lines += bullets(data["authenticity_signs"])
            lines.append("")
        if data["replica_signs"]:
            lines.append("## Признаки возможной реплики")
            lines += bullets(data["replica_signs"])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "item_type": "Настольные часы каминные", "style": "ар-деко",
            "period": "1920–1930-е", "materials": ["латунь", "мрамор", "стекло"],
            "marks": ["клеймо на циферблате «JAZ», Франция"],
            "condition": "good",
            "defects": [{"type": "потёртость", "where": "патина на латуни",
                         "severity": "low"},
                        {"type": "скол", "where": "угол мраморного основания",
                         "severity": "medium"}],
            "authenticity_signs": ["характерный шрифт цифр эпохи",
                                   "следы ручной пайки корпуса"],
            "replica_signs": ["винты с современной крестовой шлицей"],
            "completeness": "механизм на месте, ключ утрачен",
            "rarity": "серийная модель",
            "notes": [],
        }
        return {
            "images": [demo_image("clock1.jpg", scene), demo_image("clock2.jpg", {}),
                       demo_image("clock-mark.jpg", {})],
            "params": {"category": "часы", "currency": "RUB",
                       "comparables": [{"title": "JAZ ар-деко, хорошее", "price": 18000},
                                       {"title": "JAZ ар-деко, отличное", "price": 26000},
                                       {"title": "JAZ, требует ремонта", "price": 9000}]},
        }


def _condition_key(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in CONDITION:
        return s
    mapping = {"отличн": "excellent", "хорош": "good", "удовлетвор": "fair",
               "плох": "poor", "реставрац": "restoration", "новый": "mint",
               "идеальн": "mint"}
    for k, v in mapping.items():
        if k in s:
            return v
    return "good"


def _clean_defects(raw: Any) -> list[dict[str, str]]:
    out = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("type") or "").strip()
        if not t:
            continue
        out.append({"type": t, "where": str(d.get("where") or "").strip(),
                    "severity": str(d.get("severity") or "medium").lower()})
    return out


def _defect_multiplier(defects: list[dict]) -> float:
    """Каждый дефект снижает цену; эффект накапливается, но не до нуля."""
    mult = 1.0
    sev_scale = {"low": 0.5, "medium": 1.0, "high": 1.5, "critical": 2.0}
    for d in defects:
        impact = next((v for k, v in DEFECT_IMPACT.items() if k in d["type"].lower()), 0.1)
        mult *= max(0.1, 1 - impact * sev_scale.get(d["severity"], 1.0))
    return max(0.1, round(mult, 3))


def _base_range(comparables: list[dict] | None) -> tuple[float, float, int]:
    prices = sorted(as_float(c.get("price")) for c in (comparables or [])
                    if as_float(c.get("price")) > 0)
    if not prices:
        return 0.0, 0.0, 0
    if len(prices) == 1:
        return prices[0] * 0.8, prices[0] * 1.2, 1
    # Отбрасываем крайние выбросы, если выборка позволяет.
    if len(prices) >= 5:
        prices = prices[1:-1]
    return prices[0], prices[-1], len(prices)


def _verdict(auth: list[str], replica: list[str]) -> str:
    if replica and not auth:
        return "признаки указывают на реплику, нужен очный осмотр"
    if replica and auth:
        return "противоречивые признаки, обязателен очный осмотр"
    if auth:
        return "признаки соответствуют заявленному периоду (не подтверждение)"
    return "признаков для суждения недостаточно"
