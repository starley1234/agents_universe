"""№9 «Интеллектуальное распознавание сложных документов».

Чек, рукописная накладная, справка → структурированные поля.

Обещание «100% автоматизации без человека» в исходной идее — опасное:
на рукописном тексте ошибается и человек. Поэтому сервис устроен как
**автоматизация с явной границей доверия**: он сам проверяет то, что
поддаётся проверке (суммы, НДС, контрольные разряды ИНН), и честно
маршрутизирует документ — `auto` или `manual_review`. Так клиент получает
реальную автоматизацию 80–90% потока вместо ложных 100%.
"""

from __future__ import annotations

import re
from typing import Any

from ..core import Service, as_float, bullets, register, table
from ..images import ImageRef

DOC_TYPES = ("receipt", "invoice", "waybill", "certificate", "declaration", "form", "other")
TOTAL_TOLERANCE = 0.02  # 2% на округления построчно


@register
class DocExtractorService(Service):
    slug = "doc-extractor"
    title = "Распознавание сложных документов"
    summary = "Чеки, рукописные накладные, справки → поля с самопроверкой сумм."
    tags = ("documents", "api")
    max_images = 5

    system = (
        "Ты оператор ввода данных. На изображении — документ, возможно "
        "рукописный, мятый или снятый под углом. Извлеки поля дословно, как "
        "написано. Числа переноси без округления. Если символ неразборчив, "
        "поставь на его месте «?» и снизь уверенность для этого поля. Не "
        "додумывай значения, которых не видно."
    )
    schema = {
        "doc_type": "",
        "language": "",
        "handwritten": False,
        "fields": {"number": "", "date": "", "supplier": "", "buyer": "",
                   "inn": "", "total": "", "vat": "", "currency": ""},
        "line_items": [{"name": "", "qty": 0.0, "price": 0.0, "sum": 0.0}],
        "field_confidence": {},
        "illegible": [""],
        "scan_quality": "",
    }

    def analyze(self, images: list[ImageRef], doc_type: str = "",
                required_fields: list[str] | None = None,
                min_confidence: float = 0.75, **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Документ на {len(images)} изображении(ях)."
            + (f" Ожидаемый тип: {doc_type}." if doc_type else "")
            + " Извлеки все поля и построчный состав.",
            images,
        )

        fields = {k: str(v or "").strip() for k, v in (data.get("fields") or {}).items()}
        conf = {k: round(min(1.0, max(0.0, as_float(v, 0.5))), 2)
                for k, v in (data.get("field_confidence") or {}).items()}
        items = _clean_items(data.get("line_items"))

        checks = _self_checks(fields, items)
        warnings: list[str] = []
        for u in data.get("illegible") or []:
            if str(u).strip():
                warnings.append(f"неразборчиво: {u}")

        quality = str(data.get("scan_quality") or "").lower()
        if any(w in quality for w in ("плох", "размыт", "blur", "poor", "мят")):
            warnings.append(f"качество скана: {data.get('scan_quality')}")

        required = required_fields or _required_for(fields.get("doc_type")
                                                    or data.get("doc_type") or doc_type)
        missing = [f for f in required if not fields.get(f)]
        low_conf = [f for f, c in conf.items() if c < min_confidence and fields.get(f)]
        has_unreadable = any("?" in v for v in fields.values())

        failed_checks = [c for c in checks if not c["ok"]]
        route = "auto"
        reasons: list[str] = []
        if missing:
            route = "manual_review"
            reasons.append("не заполнены обязательные поля: " + ", ".join(missing))
        if failed_checks:
            route = "manual_review"
            reasons += [c["detail"] for c in failed_checks]
        if low_conf:
            route = "manual_review"
            reasons.append("низкая уверенность в полях: " + ", ".join(low_conf))
        if has_unreadable:
            route = "manual_review"
            reasons.append("в значениях остались нечитаемые символы «?»")
        if data.get("handwritten") and route == "auto":
            reasons.append("документ рукописный — рекомендуется выборочный контроль")

        return {
            "doc_type": str(data.get("doc_type") or doc_type or "other"),
            "language": str(data.get("language") or ""),
            "handwritten": bool(data.get("handwritten")),
            "fields": fields,
            "field_confidence": conf,
            "line_items": items,
            "checks": checks,
            "missing_required": missing,
            "low_confidence_fields": low_conf,
            "routing": route,
            "routing_reasons": reasons,
            "overall_confidence": _overall(conf, checks, missing),
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = [f"# Документ: {data['doc_type']}", ""]
        badge = "автоматически" if data["routing"] == "auto" else "НА ПРОВЕРКУ ЧЕЛОВЕКУ"
        lines.append(f"Маршрут: **{badge}**, общая уверенность "
                     f"{data['overall_confidence']:.0%}"
                     + (" · рукописный" if data["handwritten"] else ""))
        lines.append("")
        rows = [[k, v or "—", f"{data['field_confidence'].get(k, 0):.0%}"
                 if k in data["field_confidence"] else "—"]
                for k, v in data["fields"].items()]
        if rows:
            lines.append("## Поля")
            lines += table(["Поле", "Значение", "Уверенность"], rows)
            lines.append("")
        if data["line_items"]:
            lines.append("## Позиции")
            lines += table(["Наименование", "Кол-во", "Цена", "Сумма"],
                           [[i["name"], i["qty"], i["price"], i["sum"]]
                            for i in data["line_items"]])
            lines.append("")
        if data["checks"]:
            lines.append("## Самопроверка")
            lines += bullets(f"{'✓' if c['ok'] else '✗'} {c['name']}: {c['detail']}"
                             for c in data["checks"])
            lines.append("")
        if data["routing_reasons"]:
            lines.append("## Почему нужна проверка")
            lines += bullets(data["routing_reasons"])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "doc_type": "waybill", "language": "ru", "handwritten": True,
            "fields": {"number": "ТН-4471", "date": "12.07.2026",
                       "supplier": "ООО «Северный склад»", "buyer": "ИП Ковалёв А.С.",
                       "inn": "7701234567", "total": "18400.00", "vat": "3066.67",
                       "currency": "RUB"},
            "line_items": [
                {"name": "Профиль стальной 40x20", "qty": 120.0, "price": 95.0,
                 "sum": 11400.0},
                {"name": "Саморез 4.2x16 (уп. 500)", "qty": 20.0, "price": 350.0,
                 "sum": 7000.0},
            ],
            "field_confidence": {"number": 0.92, "date": 0.88, "supplier": 0.81,
                                 "buyer": 0.55, "inn": 0.9, "total": 0.93, "vat": 0.7},
            "illegible": ["подпись получателя"],
            "scan_quality": "снято под углом, бумага мятая",
        }
        return {"images": [demo_image("waybill.jpg", scene)],
                "params": {"doc_type": "waybill"}}


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    out = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        qty, price = as_float(it.get("qty")), as_float(it.get("price"))
        total = as_float(it.get("sum")) or round(qty * price, 2)
        out.append({"name": name, "qty": round(qty, 3), "price": round(price, 2),
                    "sum": round(total, 2)})
    return out


def _self_checks(fields: dict[str, str], items: list[dict]) -> list[dict[str, Any]]:
    """Проверки, которые можно сделать арифметикой, — делаем сами."""
    checks: list[dict[str, Any]] = []

    total = as_float(fields.get("total"))
    if items and total:
        items_sum = round(sum(i["sum"] for i in items), 2)
        diff = abs(items_sum - total)
        ok = diff <= max(0.02, total * TOTAL_TOLERANCE)
        checks.append({
            "name": "сумма позиций = итого", "ok": ok,
            "detail": f"позиции {items_sum:.2f}, в документе {total:.2f}"
                      + ("" if ok else f", расхождение {diff:.2f}")})

    for it in items:
        if it["qty"] and it["price"]:
            expected = round(it["qty"] * it["price"], 2)
            if abs(expected - it["sum"]) > max(0.02, expected * TOTAL_TOLERANCE):
                checks.append({
                    "name": f"строка «{it['name'][:30]}»", "ok": False,
                    "detail": f"{it['qty']} × {it['price']} = {expected:.2f}, "
                              f"а указано {it['sum']:.2f}"})

    vat = as_float(fields.get("vat"))
    if total and vat:
        # НДС 20%, включённый в сумму: total/6
        expected_vat = round(total / 6, 2)
        ok = abs(expected_vat - vat) <= max(0.05, expected_vat * 0.03)
        checks.append({"name": "НДС 20% от суммы", "ok": ok,
                       "detail": f"ожидалось ≈{expected_vat:.2f}, указано {vat:.2f}"})

    inn = re.sub(r"\D", "", fields.get("inn", ""))
    if inn:
        ok = _check_inn(inn)
        checks.append({"name": "контрольная сумма ИНН", "ok": ok,
                       "detail": f"{inn} — {'корректен' if ok else 'не проходит проверку'}"})
    return checks


def _check_inn(inn: str) -> bool:
    """Контрольные разряды ИНН — проверяются без обращения к реестру."""
    def dig(nums: str, weights: list[int]) -> int:
        return sum(int(d) * w for d, w in zip(nums, weights)) % 11 % 10

    if len(inn) == 10:
        return dig(inn, [2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(inn[9])
    if len(inn) == 12:
        d11 = dig(inn, [7, 2, 4, 10, 3, 5, 9, 4, 6, 8])
        d12 = dig(inn, [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8])
        return d11 == int(inn[10]) and d12 == int(inn[11])
    return False


def _required_for(doc_type: str) -> list[str]:
    t = str(doc_type or "").lower()
    if "receipt" in t or "чек" in t:
        return ["date", "total"]
    if "invoice" in t or "waybill" in t or "накладн" in t or "счёт" in t:
        return ["number", "date", "supplier", "total"]
    return ["date"]


def _overall(conf: dict[str, float], checks: list[dict], missing: list[str]) -> float:
    base = sum(conf.values()) / len(conf) if conf else 0.6
    if checks:
        passed = sum(1 for c in checks if c["ok"]) / len(checks)
        base = 0.6 * base + 0.4 * passed
    if missing:
        base *= 0.7
    return round(min(1.0, max(0.0, base)), 2)
