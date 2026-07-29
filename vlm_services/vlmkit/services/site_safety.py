"""№3 «AI-аудит безопасности на стройке».

Фото/кадры со стройплощадки → нарушения СИЗ и опасные ситуации, со
ссылкой на правило и уровнем риска.

Осознанное решение: сервис **не выносит окончательного вердикта о людях**.
Он ранжирует кадры для инспектора по охране труда. Ложное «каски нет»
из-за бликующего козырька дешевле разобрать глазами, чем пропустить
падение с высоты, поэтому спорные случаи попадают в отдельный список
`needs_review`, а не в статистику нарушений.
"""

from __future__ import annotations

from typing import Any

from ..core import Service, as_float, as_int, bullets, register, table
from ..images import ImageRef

# Каталог правил: код нарушения, вес риска, формулировка для предписания.
RULES = {
    "no_helmet": (9, "Работа без защитной каски"),
    "no_harness": (10, "Работа на высоте без страховочной привязи"),
    "no_vest": (5, "Отсутствует сигнальный жилет"),
    "no_goggles": (6, "Отсутствует защита глаз при работах с искрообразованием"),
    "no_gloves": (3, "Отсутствуют защитные перчатки"),
    "crane_zone": (10, "Нахождение人 в опасной зоне работы крана"),
    "edge_unprotected": (9, "Незащищённый край перекрытия / отсутствие ограждения"),
    "blocked_exit": (7, "Загромождён эвакуационный выход или проход"),
    "damaged_scaffold": (8, "Повреждённые или незакреплённые леса"),
    "open_pit": (7, "Незакрытый приямок или траншея без ограждения"),
    "fire_hazard": (8, "Нарушение пожарной безопасности"),
}
RULES["crane_zone"] = (10, "Нахождение людей в опасной зоне работы крана")

SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@register
class SiteSafetyService(Service):
    slug = "site-safety"
    title = "AI-аудит безопасности на стройке"
    summary = "Фото площадки → нарушения СИЗ и опасные зоны с уровнем риска."
    tags = ("construction", "hse")
    max_images = 8

    system = (
        "Ты инженер по охране труда на строительной площадке. Осмотри кадр и "
        "перечисли нарушения: люди без каски, работа на высоте без страховки, "
        "нахождение в зоне крана, незащищённые края, загромождённые проходы. "
        "Для каждого человека укажи, что именно видно. Если ракурс, засветка "
        "или расстояние не позволяют утверждать наверняка — так и напиши, "
        "поставив низкую уверенность. Лучше сомнение, чем выдуманное нарушение."
    )
    schema = {
        "people_count": 0,
        "violations": [{"rule": "", "description": "", "people": 1,
                        "confidence": 0.0, "location": ""}],
        "hazards": [{"type": "", "description": "", "severity": ""}],
        "ppe_seen": [""],
        "visibility": "",
        "site_type": "",
    }

    def analyze(self, images: list[ImageRef], site: str = "",
                confidence_threshold: float = 0.6, **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Кадров: {len(images)}." + (f" Объект: {site}." if site else "")
            + " Найди нарушения охраны труда и опасные ситуации.",
            images,
        )

        confirmed: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        for v in data.get("violations") or []:
            if not isinstance(v, dict):
                continue
            rule = str(v.get("rule") or "").strip().lower().replace(" ", "_")
            weight, title = RULES.get(rule, (5, str(v.get("description") or "Нарушение")))
            conf = round(min(1.0, max(0.0, as_float(v.get("confidence"), 0.5))), 2)
            item = {
                "rule": rule or "other",
                "title": title,
                "description": str(v.get("description") or "").strip(),
                "people": max(1, as_int(v.get("people"), 1)),
                "location": str(v.get("location") or "").strip(),
                "confidence": conf,
                "risk_weight": weight,
                "risk_score": round(weight * conf * max(1, as_int(v.get("people"), 1)), 1),
            }
            (confirmed if conf >= confidence_threshold else review).append(item)

        confirmed.sort(key=lambda x: -x["risk_score"])
        review.sort(key=lambda x: -x["risk_weight"])

        hazards = [
            {"type": str(h.get("type") or "опасность"),
             "description": str(h.get("description") or ""),
             "severity": str(h.get("severity") or "medium").lower()}
            for h in (data.get("hazards") or []) if isinstance(h, dict)
        ]

        people = as_int(data.get("people_count"))
        stop_work = any(v["rule"] in ("no_harness", "crane_zone", "edge_unprotected")
                        and v["confidence"] >= 0.8 for v in confirmed)
        risk = _risk_index(confirmed, hazards)

        warnings: list[str] = []
        vis = str(data.get("visibility") or "").lower()
        if any(w in vis for w in ("плох", "тёмн", "туман", "poor", "dark", "blur")):
            warnings.append(f"условия съёмки: {data.get('visibility')} — "
                            "часть нарушений могла остаться незамеченной")
        if review:
            warnings.append(f"{len(review)} наблюдений ниже порога уверенности "
                            f"{confidence_threshold} — нужен глаз инспектора")
        if people and not confirmed and not review:
            warnings.append("люди в кадре есть, нарушений не найдено — "
                            "проверьте, что видны каски и жилеты")

        return {
            "site": site or None,
            "people_count": people,
            "violations": confirmed,
            "needs_review": review,
            "hazards": hazards,
            "ppe_seen": [str(p) for p in (data.get("ppe_seen") or []) if p],
            "risk_index": risk,
            "risk_level": _level(risk, confirmed, stop_work),
            "stop_work_required": stop_work,
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = ["# Аудит охраны труда", ""]
        if data["site"]:
            lines.append(f"Объект: {data['site']}")
        lines.append(f"Индекс риска: **{data['risk_index']}** ({data['risk_level']}). "
                     f"Людей в кадре: {data['people_count']}.")
        if data["stop_work_required"]:
            lines.append("")
            lines.append("> **ТРЕБУЕТСЯ ОСТАНОВКА РАБОТ.** Зафиксирован риск падения "
                         "с высоты или нахождение в зоне крана.")
        lines.append("")
        if data["violations"]:
            lines.append("## Подтверждённые нарушения")
            lines += table(["Нарушение", "Людей", "Где", "Уверенность", "Риск"],
                           [[v["title"], v["people"], v["location"] or "—",
                             f"{v['confidence']:.0%}", v["risk_score"]]
                            for v in data["violations"]])
            lines.append("")
        if data["hazards"]:
            lines.append("## Опасные факторы")
            lines += bullets(f"[{h['severity']}] {h['type']}: {h['description']}"
                             for h in data["hazards"])
            lines.append("")
        if data["needs_review"]:
            lines.append("## Требует проверки инспектором")
            lines += bullets(f"{v['title']} — уверенность {v['confidence']:.0%}"
                             + (f", {v['description']}" if v["description"] else "")
                             for v in data["needs_review"])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "people_count": 4,
            "violations": [
                {"rule": "no_helmet", "description": "рабочий у бетономешалки без каски",
                 "people": 1, "confidence": 0.92, "location": "передний план справа"},
                {"rule": "no_harness", "description": "монтажник на перекрытии 3 этажа "
                 "без страховочной привязи", "people": 1, "confidence": 0.87,
                 "location": "верхний ярус"},
                {"rule": "no_vest", "description": "возможно отсутствует жилет, "
                 "человек в тени", "people": 1, "confidence": 0.35, "location": "фон слева"},
            ],
            "hazards": [{"type": "незакрытый приямок", "description":
                         "траншея без ограждения у входа", "severity": "high"}],
            "ppe_seen": ["каски у двух рабочих", "сигнальные жилеты"],
            "visibility": "хорошая", "site_type": "жилое строительство",
        }
        return {"images": [demo_image("site.jpg", scene)],
                "params": {"site": "ЖК Северный, корпус 3"}}


def _risk_index(violations: list[dict], hazards: list[dict]) -> int:
    """Сводный индекс 0–100: сумма взвешенных рисков, срезанная сверху."""
    total = sum(v["risk_score"] for v in violations)
    total += sum(SEVERITY.get(h["severity"], 2) * 2 for h in hazards)
    return int(min(100, round(total)))


def _level(risk: int, violations: list[dict], stop_work: bool) -> str:
    """Уровень определяется не только суммой, но и худшим одиночным риском.

    Одно нарушение «работа на высоте без страховки» даёт небольшую сумму,
    но это смертельный риск. Усреднять его с мелочью нельзя: инспектор,
    увидев «средний», не поедет на объект.
    """
    if stop_work:
        return "критический"
    worst = max((v["risk_weight"] * v["confidence"] for v in violations), default=0)
    if risk >= 60 or worst >= 8:
        return "критический" if risk >= 60 else "высокий"
    if risk >= 30 or worst >= 6:
        return "высокий" if risk >= 30 else "средний"
    if risk >= 10:
        return "средний"
    return "низкий"
