"""№5 «UX/UI критик и оптимизатор конверсии».

Скриншот лендинга → замечания по читаемости, иерархии и призыву к
действию, с оценкой влияния на конверсию.

Особенность: контраст текста и кнопок **измеряется**, а не оценивается на
глаз. Модель называет цвета, которые видит, а формула WCAG считает
коэффициент и говорит, проходит ли элемент AA. «Кнопка сливается с фоном» —
это 2.1:1 при норме 4.5:1, и в отчёте для дизайнера должно стоять число.
"""

from __future__ import annotations

import re
from typing import Any

from ..core import Service, as_float, as_int, bullets, register, table
from ..images import ImageRef

WCAG_AA_TEXT = 4.5
WCAG_AA_LARGE = 3.0

# Вес проблемы для оценки влияния на конверсию.
IMPACT = {"cta": 25, "contrast": 20, "readability": 15, "hierarchy": 12,
          "trust": 12, "clutter": 10, "mobile": 15, "form": 18, "other": 5}


@register
class UxCriticService(Service):
    slug = "ux-critic"
    title = "UX/UI критик и оптимизатор конверсии"
    summary = "Скриншот лендинга → замечания, измеренный контраст, приоритет правок."
    tags = ("design", "cro")
    max_images = 4

    system = (
        "Ты продуктовый дизайнер и специалист по конверсии. Перед тобой "
        "скриншот экрана. Оцени: виден ли главный призыв к действию, читается "
        "ли текст, есть ли визуальная иерархия, не перегружен ли экран. "
        "Для ключевых элементов назови цвета в HEX так, как ты их видишь: "
        "цвет текста и цвет фона под ним. Пиши конкретные правки, а не общие "
        "слова вроде «сделать красивее»."
    )
    schema = {
        "screen_type": "",
        "primary_cta": {"text": "", "visible": True, "above_fold": True,
                        "fg_hex": "", "bg_hex": ""},
        "text_samples": [{"where": "", "fg_hex": "", "bg_hex": "", "size": "normal"}],
        "issues": [{"category": "", "problem": "", "fix": "", "severity": ""}],
        "strengths": [""],
        "element_count": 0,
        "trust_signals": [""],
    }

    def analyze(self, images: list[ImageRef], goal: str = "",
                platform: str = "desktop", **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Экран ({platform})." + (f" Цель страницы: {goal}." if goal else "")
            + " Разбери по UX и конверсии, назови HEX-цвета текста и фона.",
            images,
        )

        contrast_checks = _contrast_checks(data)
        issues = _clean_issues(data.get("issues"))

        # Проваленный контраст — это факт, а не мнение: добавляем сами.
        for c in contrast_checks:
            if c["passes"]:
                continue
            issues.append({
                "category": "contrast",
                "problem": f"{c['where']}: контраст {c['ratio']}:1 при норме "
                           f"{c['required']}:1 — текст не читается частью аудитории",
                "fix": f"затемнить текст или осветлить фон до контраста "
                       f"{c['required']}:1 (WCAG AA)",
                "severity": "high" if c["ratio"] < c["required"] * 0.6 else "medium",
            })

        cta = data.get("primary_cta") or {}
        if not cta.get("visible", True):
            issues.append({"category": "cta", "problem": "главный призыв к действию "
                           "не найден на экране", "fix": "добавить одну заметную кнопку "
                           "с глаголом действия", "severity": "critical"})
        elif not cta.get("above_fold", True):
            issues.append({"category": "cta", "problem": "кнопка ниже первого экрана — "
                           "её увидят не все", "fix": "поднять CTA в первый экран",
                           "severity": "high"})

        elements = as_int(data.get("element_count"))
        if elements > 25:
            issues.append({"category": "clutter",
                           "problem": f"на экране {elements} активных элементов — "
                                      "внимание рассеивается",
                           "fix": "убрать второстепенные блоки, оставить один сценарий",
                           "severity": "medium"})

        if not (data.get("trust_signals") or []):
            issues.append({"category": "trust", "problem": "нет элементов доверия "
                           "(отзывы, гарантии, логотипы клиентов)",
                           "fix": "добавить социальное доказательство рядом с CTA",
                           "severity": "medium"})

        issues = _dedup(issues)
        score = _score(issues)
        return {
            "screen_type": str(data.get("screen_type") or ""),
            "platform": platform,
            "goal": goal or None,
            "primary_cta": {"text": str(cta.get("text") or ""),
                            "visible": bool(cta.get("visible", True)),
                            "above_fold": bool(cta.get("above_fold", True))},
            "contrast_checks": contrast_checks,
            "issues": issues,
            "strengths": [str(s) for s in (data.get("strengths") or []) if s],
            "element_count": elements,
            "ux_score": score,
            "conversion_impact_range_pct": _impact(issues),
            "_warnings": _warnings(contrast_checks, data),
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = ["# UX-разбор экрана", ""]
        lo, hi = data["conversion_impact_range_pct"]
        lines.append(f"Оценка: **{data['ux_score']}/100**. Ориентировочный потенциал "
                     f"роста конверсии при исправлении: {lo}–{hi}% "
                     f"(зависит от трафика и оффера, проверяйте A/B-тестом).")
        cta = data["primary_cta"]
        lines.append(f"Главный CTA: {cta['text'] or '—'} "
                     f"({'виден' if cta['visible'] else 'НЕ НАЙДЕН'}"
                     f"{', в первом экране' if cta['above_fold'] else ', ниже сгиба'}).")
        lines.append("")
        if data["contrast_checks"]:
            lines.append("## Контраст (WCAG)")
            lines += table(["Элемент", "Текст", "Фон", "Контраст", "Норма", "Итог"],
                           [[c["where"], c["fg"], c["bg"], f"{c['ratio']}:1",
                             f"{c['required']}:1", "✓" if c["passes"] else "✗"]
                            for c in data["contrast_checks"]])
            lines.append("")
        if data["issues"]:
            lines.append("## Что чинить (по приоритету)")
            for i, issue in enumerate(data["issues"], 1):
                lines.append(f"{i}. **[{issue['severity']}]** {issue['problem']}")
                if issue["fix"]:
                    lines.append(f"   → {issue['fix']}")
            lines.append("")
        if data["strengths"]:
            lines.append("## Что уже хорошо")
            lines += bullets(data["strengths"])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "screen_type": "лендинг SaaS-продукта",
            "primary_cta": {"text": "Попробовать бесплатно", "visible": True,
                            "above_fold": True, "fg_hex": "#8FB8E8", "bg_hex": "#FFFFFF"},
            "text_samples": [
                {"where": "подзаголовок героя", "fg_hex": "#B0B0B0",
                 "bg_hex": "#FFFFFF", "size": "normal"},
                {"where": "основной заголовок", "fg_hex": "#1A1A2E",
                 "bg_hex": "#FFFFFF", "size": "large"},
            ],
            "issues": [{"category": "hierarchy", "problem": "три кнопки одного веса "
                        "в первом экране", "fix": "оставить одну основную, "
                        "остальные сделать текстовыми ссылками", "severity": "medium"}],
            "strengths": ["чистая сетка", "быстро считывается суть продукта"],
            "element_count": 31,
            "trust_signals": [],
        }
        return {"images": [demo_image("landing.png", scene)],
                "params": {"goal": "регистрация на триал", "platform": "desktop"}}


# --- контраст по WCAG ------------------------------------------------------
def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    lo, hi = sorted((l1, l2))
    return round((hi + 0.05) / (lo + 0.05), 2)


def parse_hex(value: Any) -> tuple[int, int, int] | None:
    s = str(value or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", s):
        return None
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _contrast_checks(data: dict) -> list[dict[str, Any]]:
    out = []
    samples = list(data.get("text_samples") or [])
    cta = data.get("primary_cta") or {}
    if cta.get("fg_hex") or cta.get("bg_hex"):
        samples.append({"where": f"кнопка «{cta.get('text', 'CTA')}»",
                        "fg_hex": cta.get("fg_hex"), "bg_hex": cta.get("bg_hex"),
                        "size": "large"})
    for s in samples:
        if not isinstance(s, dict):
            continue
        fg, bg = parse_hex(s.get("fg_hex")), parse_hex(s.get("bg_hex"))
        if not fg or not bg:
            continue
        ratio = contrast_ratio(fg, bg)
        required = WCAG_AA_LARGE if str(s.get("size")) == "large" else WCAG_AA_TEXT
        out.append({"where": str(s.get("where") or "элемент"),
                    "fg": str(s.get("fg_hex")), "bg": str(s.get("bg_hex")),
                    "ratio": ratio, "required": required, "passes": ratio >= required})
    return out


def _clean_issues(raw: Any) -> list[dict[str, str]]:
    out = []
    for i in raw or []:
        if not isinstance(i, dict) or not (i.get("problem") or i.get("fix")):
            continue
        out.append({"category": str(i.get("category") or "other").lower(),
                    "problem": str(i.get("problem") or ""),
                    "fix": str(i.get("fix") or ""),
                    "severity": str(i.get("severity") or "medium").lower()})
    return out


def _dedup(issues: list[dict]) -> list[dict]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    seen: set[str] = set()
    out = []
    for i in sorted(issues, key=lambda x: order.get(x["severity"], 4)):
        key = i["problem"][:60].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out


def _score(issues: list[dict]) -> int:
    penalty = {"critical": 25, "high": 14, "medium": 7, "low": 3}
    return max(0, 100 - sum(penalty.get(i["severity"], 5) for i in issues))


def _impact(issues: list[dict]) -> list[int]:
    """Диапазон потенциала роста конверсии, а не одно число.

    По одному скриншоту точный процент не предсказать: он зависит от
    трафика, оффера и цены. Поэтому возвращаем широкую вилку и держим
    верхнюю границу скромной — обещание «+45% конверсии» дизайнер
    проверит на своих данных и больше не вернётся.
    """
    total = sum(IMPACT.get(i["category"], 5) *
                (1.0 if i["severity"] in ("critical", "high") else 0.4)
                for i in issues)
    high = int(min(25, round(total * 0.5)))
    return [int(high * 0.3), high]


def _warnings(checks: list[dict], data: dict) -> list[str]:
    w = []
    if not checks:
        w.append("модель не назвала цвета — контраст не проверен, "
                 "оценка только по композиции")
    if not (data.get("text_samples") or []):
        w.append("нет образцов текста для проверки читаемости")
    return w
