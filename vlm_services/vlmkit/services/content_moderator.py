"""№10 «Модератор визуального контента для сообществ».

Изображение → категории нарушений, действие и объяснение.

Исходная идея обещает «замену сотен живых модераторов». Это неверная
постановка, и сервис устроен иначе: он **фильтрует поток и расставляет
приоритеты**, а спорное отдаёт человеку. Причины две — предметная и
юридическая:

- контекст решает всё: один и тот же кадр бывает пропагандой и
  историческим документом, и модель этого различия не гарантирует;
- за ошибочную блокировку площадка отвечает перед пользователем, а за
  пропуск запрещённого — перед регулятором.

Поэтому автоматически проходят только уверенно чистые и уверенно
запрещённые случаи, а серая зона маршрутизируется в очередь модерации.
Экономия всё равно кратная — но честная.
"""

from __future__ import annotations

from typing import Any

from ..core import Service, as_float, bullets, register, table
from ..images import ImageRef

# Категории и порог, выше которого решение принимается без человека.
# Для «жёстких» категорий порог низкий: пропустить хуже, чем перестраховаться.
POLICY = {
    "sexual":        {"weight": 10, "auto_block": 0.85, "title": "Сексуализированный контент"},
    "violence":      {"weight": 9,  "auto_block": 0.85, "title": "Насилие и жестокость"},
    "hate":          {"weight": 10, "auto_block": 0.80, "title": "Ненависть и дискриминация"},
    "self_harm":     {"weight": 10, "auto_block": 0.75, "title": "Самоповреждение"},
    "drugs":         {"weight": 8,  "auto_block": 0.85, "title": "Наркотики"},
    "weapons":       {"weight": 7,  "auto_block": 0.90, "title": "Оружие"},
    "spam":          {"weight": 4,  "auto_block": 0.90, "title": "Скрытая реклама и спам"},
    "scam":          {"weight": 8,  "auto_block": 0.85, "title": "Мошенничество"},
    "shock":         {"weight": 6,  "auto_block": 0.85, "title": "Шокирующий контент"},
    "minors_risk":   {"weight": 10, "auto_block": 0.60, "title": "Риск для несовершеннолетних"},
    "copyright":     {"weight": 3,  "auto_block": 0.95, "title": "Возможное нарушение авторских прав"},
}

GREY_ZONE = (0.35, 0.85)  # ниже — чисто, выше — блок, между — человек


@register
class ContentModeratorService(Service):
    slug = "content-moderator"
    title = "Модератор визуального контента"
    summary = "Изображение → категории нарушений, действие и очередь на ручную проверку."
    tags = ("moderation", "enterprise")
    max_images = 4

    system = (
        "Ты модератор платформы. Оцени изображение по категориям нарушений и "
        "объясни решение. Учитывай контекст: мем может нести скрытое "
        "оскорбление, а безобидное фото — завуалированную рекламу. Отдельно "
        "отметь, если кадр допускает несколько прочтений (например, "
        "историческая фотография против пропаганды) — такие случаи должен "
        "смотреть человек. Оценка по каждой категории от 0 до 1."
    )
    schema = {
        "scores": {"sexual": 0.0, "violence": 0.0, "hate": 0.0, "self_harm": 0.0,
                   "drugs": 0.0, "weapons": 0.0, "spam": 0.0, "scam": 0.0,
                   "shock": 0.0, "minors_risk": 0.0, "copyright": 0.0},
        "detected": [{"category": "", "evidence": "", "score": 0.0}],
        "hidden_meaning": "",
        "ambiguous": False,
        "ambiguity_reason": "",
        "text_in_image": "",
        "depicts_minors": False,
    }

    def analyze(self, images: list[ImageRef], strictness: str = "normal",
                categories: list[str] | None = None, **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Проверь {len(images)} изображение(й) по правилам сообщества.", images)

        active = [c for c in (categories or POLICY) if c in POLICY]
        raw_scores = data.get("scores") or {}
        scores = {c: round(min(1.0, max(0.0, as_float(raw_scores.get(c), 0.0))), 2)
                  for c in active}
        shift = {"strict": -0.15, "normal": 0.0, "lenient": 0.10}.get(strictness, 0.0)

        flagged = []
        for cat, score in scores.items():
            if score <= 0.05:
                continue
            policy = POLICY[cat]
            threshold = max(0.3, policy["auto_block"] + shift)
            flagged.append({
                "category": cat,
                "title": policy["title"],
                "score": score,
                "weight": policy["weight"],
                "auto_block_at": round(threshold, 2),
                "blocks": score >= threshold,
                "evidence": _evidence(data, cat),
            })
        flagged.sort(key=lambda f: -(f["score"] * f["weight"]))

        ambiguous = bool(data.get("ambiguous"))
        minors = bool(data.get("depicts_minors"))
        top = flagged[0]["score"] if flagged else 0.0

        action, reasons = _decide(flagged, ambiguous, minors, top, strictness)
        risk = round(sum(f["score"] * f["weight"] for f in flagged), 1)

        warnings: list[str] = []
        if ambiguous and data.get("ambiguity_reason"):
            warnings.append(f"неоднозначный кадр: {data['ambiguity_reason']}")
        if minors:
            warnings.append("в кадре возможны несовершеннолетние — "
                            "любые сомнения решаются в пользу проверки человеком")

        return {
            "action": action,
            "action_reasons": reasons,
            "risk_score": risk,
            "scores": scores,
            "flagged": flagged,
            "hidden_meaning": str(data.get("hidden_meaning") or ""),
            "text_in_image": str(data.get("text_in_image") or ""),
            "ambiguous": ambiguous,
            "depicts_minors": minors,
            "strictness": strictness,
            "human_review_required": action == "review",
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        titles = {"allow": "ПРОПУСТИТЬ", "review": "НА ПРОВЕРКУ МОДЕРАТОРУ",
                  "block": "ЗАБЛОКИРОВАТЬ", "limit": "ОГРАНИЧИТЬ ПОКАЗ"}
        lines = ["# Модерация изображения", ""]
        lines.append(f"Решение: **{titles.get(data['action'], data['action'])}** "
                     f"(индекс риска {data['risk_score']}, режим {data['strictness']})")
        lines.append("")
        if data["action_reasons"]:
            lines += bullets(data["action_reasons"])
            lines.append("")
        if data["flagged"]:
            lines.append("## Сработавшие категории")
            lines += table(["Категория", "Оценка", "Порог блокировки", "Блокирует"],
                           [[f["title"], f["score"], f["auto_block_at"],
                             "да" if f["blocks"] else "нет"] for f in data["flagged"]])
            lines.append("")
        if data["hidden_meaning"]:
            lines.append(f"**Скрытый смысл:** {data['hidden_meaning']}")
            lines.append("")
        if data["text_in_image"]:
            lines.append(f"**Текст на изображении:** {data['text_in_image']}")
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "scores": {"sexual": 0.02, "violence": 0.1, "hate": 0.62, "self_harm": 0.0,
                       "drugs": 0.0, "weapons": 0.05, "spam": 0.4, "scam": 0.1,
                       "shock": 0.05, "minors_risk": 0.0, "copyright": 0.3},
            "detected": [
                {"category": "hate", "evidence": "мем с уничижительной подписью "
                 "в адрес национальной группы", "score": 0.62},
                {"category": "spam", "evidence": "в углу QR-код и ссылка на канал",
                 "score": 0.4},
            ],
            "hidden_meaning": "подпись обыгрывает стереотип, прямых оскорблений нет",
            "ambiguous": True,
            "ambiguity_reason": "возможна сатира на сам стереотип, а не его поддержка",
            "text_in_image": "«они всегда так делают» + t.me/somechannel",
            "depicts_minors": False,
        }
        return {"images": [demo_image("meme.png", scene)],
                "params": {"strictness": "normal"}}


def _evidence(data: dict, category: str) -> str:
    for d in data.get("detected") or []:
        if isinstance(d, dict) and str(d.get("category")) == category:
            return str(d.get("evidence") or "")
    return ""


def _decide(flagged: list[dict], ambiguous: bool, minors: bool, top: float,
            strictness: str) -> tuple[str, list[str]]:
    """Решение с явной серой зоной: спорное — человеку, а не автоматике."""
    reasons: list[str] = []
    blocking = [f for f in flagged if f["blocks"]]

    if minors and top >= 0.2:
        return "review", ["в кадре возможны несовершеннолетние — обязательная "
                          "проверка человеком независимо от оценок"]
    if ambiguous and top >= GREY_ZONE[0]:
        reasons.append("кадр допускает несколько прочтений — решение за модератором")
        return "review", reasons
    if blocking:
        reasons += [f"{f['title']}: оценка {f['score']} ≥ порога {f['auto_block_at']}"
                    for f in blocking]
        return "block", reasons
    if top >= GREY_ZONE[0]:
        reasons.append(f"максимальная оценка {top} в серой зоне "
                       f"{GREY_ZONE[0]}–{GREY_ZONE[1]} — нужен человек")
        return "review", reasons
    if strictness == "strict" and top >= 0.2:
        return "limit", [f"строгий режим: оценка {top} — ограничить показ "
                         "до проверки"]
    return "allow", ["уверенных признаков нарушений не найдено"]
