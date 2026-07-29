"""№2 «Инспектор качества выкладки» (Retail Audit AI).

Фото полки → соответствие планограмме, доля полки (SOS), out-of-stock,
ценники.

Здесь особенно важно, что считает код: доля полки — это деньги в споре
между производителем и сетью, и «на глаз 30%» не годится. Модель
перечисляет фейсинги по брендам, а SOS, отклонение от планограммы и
статус аудита считаются арифметикой.
"""

from __future__ import annotations

from typing import Any

from ..core import Service, as_float, as_int, bullets, pct, register, table
from ..images import ImageRef


@register
class RetailAuditService(Service):
    slug = "retail-audit"
    title = "Инспектор качества выкладки (Retail Audit)"
    summary = "Фото полки → доля полки, out-of-stock, ценники, соответствие планограмме."
    tags = ("retail", "fmcg")
    max_images = 6

    system = (
        "Ты аудитор торговой точки. На фото — стеллаж магазина. Посчитай "
        "фейсинги (видимые лицевые стороны упаковок) по брендам, найди пустые "
        "места, товары без ценника и нарушения выкладки. Считай только то, что "
        "реально видно на снимке; если полка обрезана краем кадра, скажи об этом."
    )
    schema = {
        "shelf_levels": 0,
        "facings": [{"brand": "", "product": "", "count": 0, "shelf_level": 0,
                     "price_tag": True, "price": ""}],
        "empty_slots": 0,
        "issues": [{"type": "", "detail": "", "severity": ""}],
        "photo_quality": "",
        "cropped": False,
    }

    def analyze(self, images: list[ImageRef], planogram: dict | None = None,
                our_brand: str = "", min_sos_pct: float = 30.0,
                **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Аудит выкладки, {len(images)} фото. "
            + (f"Наш бренд: {our_brand}. " if our_brand else "")
            + "Перечисли фейсинги по брендам и найди нарушения.",
            images,
        )

        facings = _clean_facings(data.get("facings"))
        total = sum(f["count"] for f in facings)
        by_brand: dict[str, int] = {}
        for f in facings:
            by_brand[f["brand"]] = by_brand.get(f["brand"], 0) + f["count"]

        shares = sorted(
            ({"brand": b, "facings": n, "sos_pct": pct(n, total)} for b, n in by_brand.items()),
            key=lambda x: -x["facings"])

        our = next((s for s in shares if our_brand and
                    s["brand"].lower() == our_brand.lower()), None)
        our_sos = our["sos_pct"] if our else 0.0

        no_tag = [f for f in facings if not f["price_tag"]]
        empty = as_int(data.get("empty_slots"))

        issues = _clean_issues(data.get("issues"))
        for f in no_tag:
            issues.append({"type": "нет ценника",
                           "detail": f"{f['brand']} {f['product']}".strip(),
                           "severity": "high"})
        if empty:
            issues.append({"type": "out-of-stock",
                           "detail": f"пустых мест на полке: {empty}",
                           "severity": "critical" if empty > 2 else "medium"})

        # соответствие планограмме: сравниваем требуемые доли с фактом
        compliance = _compliance(planogram, by_brand, total)
        for c in compliance:
            if c["status"] != "ок":
                issues.append({
                    "type": "отклонение от планограммы",
                    "detail": f"{c['brand']}: план {c['required_pct']}%, "
                              f"факт {c['actual_pct']}%",
                    "severity": "high" if c["gap_pct"] < -10 else "medium"})

        warnings: list[str] = []
        if data.get("cropped"):
            warnings.append("полка обрезана краем кадра — доля полки занижена, "
                            "переснимите стеллаж целиком")
        quality = str(data.get("photo_quality") or "").lower()
        if any(w in quality for w in ("плох", "размыт", "тёмн", "blur", "dark")):
            warnings.append(f"качество снимка: {data.get('photo_quality')}")
        if not total:
            warnings.append("на фото не распознано ни одного фейсинга — "
                            "проверьте, что снят товар, а не пустой стеллаж")

        sos_ok = our_sos >= min_sos_pct if our_brand else None
        if our_brand and not our:
            issues.append({"type": "бренд отсутствует",
                           "detail": f"{our_brand} не найден на полке",
                           "severity": "critical"})

        score = _score(issues, sos_ok, total)
        return {
            "total_facings": total,
            "shelf_levels": as_int(data.get("shelf_levels")),
            "share_of_shelf": shares,
            "our_brand": our_brand or None,
            "our_sos_pct": our_sos,
            "sos_target_pct": min_sos_pct,
            "sos_ok": sos_ok,
            "empty_slots": empty,
            "missing_price_tags": len(no_tag),
            "issues": issues,
            "compliance": compliance,
            "audit_score": score,
            "verdict": "принято" if score >= 80 else "требует исправления"
                       if score >= 50 else "критично",
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = ["# Аудит выкладки", ""]
        lines.append(f"Оценка: **{data['audit_score']}/100** — {data['verdict']}. "
                     f"Фейсингов: {data['total_facings']}, пустых мест: {data['empty_slots']}, "
                     f"без ценника: {data['missing_price_tags']}.")
        if data["our_brand"]:
            mark = "✓" if data["sos_ok"] else "✗"
            lines.append(f"Доля полки {data['our_brand']}: **{data['our_sos_pct']}%** "
                         f"при цели {data['sos_target_pct']}% {mark}")
        lines.append("")
        if data["share_of_shelf"]:
            lines.append("## Доля полки")
            lines += table(["Бренд", "Фейсингов", "SOS"],
                           [[s["brand"], s["facings"], f"{s['sos_pct']}%"]
                            for s in data["share_of_shelf"]])
            lines.append("")
        if data["compliance"]:
            lines.append("## Планограмма")
            lines += table(["Бренд", "План", "Факт", "Отклонение", "Статус"],
                           [[c["brand"], f"{c['required_pct']}%", f"{c['actual_pct']}%",
                             f"{c['gap_pct']:+.1f}%", c["status"]]
                            for c in data["compliance"]])
            lines.append("")
        if data["issues"]:
            lines.append("## Нарушения")
            lines += table(["Тип", "Детали", "Критичность"],
                           [[i["type"], i["detail"], i["severity"]] for i in data["issues"]])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "shelf_levels": 3,
            "facings": [
                {"brand": "Аква", "product": "вода 0.5", "count": 6, "shelf_level": 1,
                 "price_tag": True, "price": "45"},
                {"brand": "Аква", "product": "вода 1.5", "count": 4, "shelf_level": 2,
                 "price_tag": False, "price": ""},
                {"brand": "Родник", "product": "вода 0.5", "count": 12, "shelf_level": 1,
                 "price_tag": True, "price": "39"},
                {"brand": "Кристалл", "product": "вода 1.0", "count": 8, "shelf_level": 3,
                 "price_tag": True, "price": "52"},
            ],
            "empty_slots": 3,
            "issues": [{"type": "грязная полка", "detail": "нижний уровень запылён",
                        "severity": "low"}],
            "photo_quality": "хорошее", "cropped": False,
        }
        return {
            "images": [demo_image("shelf.jpg", scene)],
            "params": {"our_brand": "Аква", "min_sos_pct": 40.0,
                       "planogram": {"Аква": 40, "Родник": 35, "Кристалл": 25}},
        }


def _clean_facings(raw: Any) -> list[dict[str, Any]]:
    out = []
    for f in raw or []:
        if not isinstance(f, dict):
            continue
        brand = str(f.get("brand") or "").strip()
        count = as_int(f.get("count"))
        if not brand or count <= 0:
            continue
        out.append({
            "brand": brand,
            "product": str(f.get("product") or "").strip(),
            "count": count,
            "shelf_level": as_int(f.get("shelf_level")),
            "price_tag": bool(f.get("price_tag", True)),
            "price": str(f.get("price") or "").strip(),
        })
    return out


def _clean_issues(raw: Any) -> list[dict[str, str]]:
    out = []
    for i in raw or []:
        if isinstance(i, dict) and (i.get("type") or i.get("detail")):
            out.append({"type": str(i.get("type") or "нарушение"),
                        "detail": str(i.get("detail") or ""),
                        "severity": str(i.get("severity") or "medium").lower()})
    return out


def _compliance(planogram: dict | None, by_brand: dict[str, int],
                total: int) -> list[dict[str, Any]]:
    if not planogram or not total:
        return []
    out = []
    for brand, required in planogram.items():
        req = as_float(required)
        actual = pct(by_brand.get(brand, 0), total)
        gap = round(actual - req, 1)
        out.append({"brand": brand, "required_pct": req, "actual_pct": actual,
                    "gap_pct": gap,
                    "status": "ок" if gap >= -2 else "недовыкладка"})
    return sorted(out, key=lambda c: c["gap_pct"])


def _score(issues: list[dict], sos_ok: bool | None, total: int) -> int:
    """Балл аудита: штрафы за нарушения по критичности."""
    if not total:
        return 0
    penalty = {"critical": 25, "high": 12, "medium": 6, "low": 2}
    score = 100 - sum(penalty.get(i["severity"], 5) for i in issues)
    if sos_ok is False:
        score -= 15
    return max(0, min(100, score))
