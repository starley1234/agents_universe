"""№12 «Виртуальный ассистент по ремонту техники».

Кадр разобранного устройства → что видно и какой следующий шаг.

Главное отличие от «просто описания картинки»: прежде чем советовать
касаться железа, сервис проверяет **безопасность шага**. Конденсатор
блока питания держит опасное напряжение спустя минуты после отключения,
а совет «просто вытащи вон ту плату» без предупреждения — это удар током
и наша ответственность. Опасные шаги блокируются до подтверждения
предусловий (обесточено, разряжено, снят аккумулятор).
"""

from __future__ import annotations

from typing import Any

from ..core import Service, as_float, as_int, bullets, register, table
from ..images import ImageRef

# Узлы, требующие обязательных предусловий перед прикосновением.
HAZARDS = {
    "capacitor": ("накопительный конденсатор", ["обесточено", "разряжено"],
                  "Конденсатор держит напряжение после отключения от сети. "
                  "Разрядите через резистор, не замыкайте отвёрткой."),
    "psu": ("блок питания", ["обесточено", "разряжено"],
            "Сетевая часть под опасным напряжением. Отключите от розетки."),
    "battery": ("аккумулятор", ["снят аккумулятор"],
                "Литиевый аккумулятор при проколе или замыкании горит. "
                "Не поддевайте металлом."),
    "crt": ("кинескоп", ["обесточено", "разряжено"],
            "Анодное напряжение до 25 кВ сохраняется долго. Без опыта не трогать."),
    "mains": ("сетевая проводка", ["обесточено"],
              "Обесточьте и убедитесь индикатором отсутствия напряжения."),
    "laser": ("лазерный узел", ["обесточено"],
              "Излучение опасно для глаз. Не включайте со снятой крышкой."),
    "fuel": ("топливная система", ["сброшено давление"],
             "Топливо под давлением. Сбросьте давление в рампе."),
    "hydraulic": ("гидравлика", ["сброшено давление"],
                  "Струя под давлением пробивает кожу. Сбросьте давление."),
    "hot": ("горячий узел", ["остыло"],
            "Дайте узлу остыть, иначе ожог."),
}


@register
class RepairGuideService(Service):
    slug = "repair-guide"
    title = "Виртуальный ассистент по ремонту техники"
    summary = "Фото разобранного устройства → следующий шаг с проверкой безопасности."
    tags = ("service", "diy")
    max_images = 4

    system = (
        "Ты опытный мастер сервисного центра. На фото — разобранное "
        "устройство. Назови видимые узлы и их положение в кадре (слева, "
        "справа, в центре), отметь неисправные признаки: вздутые "
        "конденсаторы, следы перегрева, обрывы, окисление. Предложи "
        "следующий конкретный шаг. Обязательно перечисли опасные узлы, "
        "которых нельзя касаться до обесточивания и разрядки. Если по кадру "
        "нельзя понять модель устройства — скажи об этом."
    )
    schema = {
        "device": "", "device_confident": False, "stage": "",
        "visible_parts": [{"name": "", "position": "", "state": ""}],
        "faults": [{"part": "", "symptom": "", "confidence": 0.0}],
        "hazards": [""],
        "next_steps": [{"action": "", "target": "", "tool": "", "caution": ""}],
        "tools_needed": [""],
        "unclear": [""],
    }

    def analyze(self, images: list[ImageRef], device: str = "", problem: str = "",
                confirmed: list[str] | None = None, skill: str = "novice",
                **params: Any) -> dict[str, Any]:
        confirmed_set = {str(c).strip().lower() for c in (confirmed or [])}
        data = self.ask(
            (f"Устройство: {device}. " if device else "")
            + (f"Симптом: {problem}. " if problem else "")
            + f"Уровень пользователя: {skill}. Что видно и какой следующий шаг?",
            images,
        )

        parts = [{"name": str(p.get("name") or ""), "position": str(p.get("position") or ""),
                  "state": str(p.get("state") or "")}
                 for p in (data.get("visible_parts") or []) if isinstance(p, dict)
                 and p.get("name")]
        faults = [{"part": str(f.get("part") or ""), "symptom": str(f.get("symptom") or ""),
                   "confidence": round(min(1.0, max(0.0, as_float(f.get("confidence"), 0.5))), 2)}
                  for f in (data.get("faults") or []) if isinstance(f, dict)]

        hazards = _detect_hazards(data, parts)
        required = sorted({req for h in hazards for req in h["requires"]})
        unmet = [r for r in required if r not in confirmed_set]

        steps = _clean_steps(data.get("next_steps"))
        blocked = bool(unmet) and bool(steps)
        for s in steps:
            s["blocked"] = blocked
        warnings: list[str] = []
        if not data.get("device_confident", True) and not device:
            warnings.append("модель устройства определена неуверенно — "
                            "укажите её вручную, иначе шаги могут не подойти")
        for u in data.get("unclear") or []:
            if str(u).strip():
                warnings.append(f"не видно на кадре: {u}")
        if skill == "novice" and hazards:
            warnings.append("для новичка: работы с этими узлами лучше доверить "
                            "сервисному центру")

        return {
            "device": device or str(data.get("device") or ""),
            "stage": str(data.get("stage") or ""),
            "visible_parts": parts,
            "faults": sorted(faults, key=lambda f: -f["confidence"]),
            "hazards": hazards,
            "safety_requirements": required,
            "unmet_requirements": unmet,
            "steps_blocked": blocked,
            "next_steps": steps,
            "tools_needed": [str(t) for t in (data.get("tools_needed") or []) if t],
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = [f"# Ремонт: {data['device'] or 'устройство'}", ""]
        if data["stage"]:
            lines.append(f"Этап: {data['stage']}")
            lines.append("")
        if data["hazards"]:
            lines.append("## ⚠ Опасные узлы")
            for h in data["hazards"]:
                lines.append(f"- **{h['title']}** — {h['warning']}")
            lines.append("")
        if data["unmet_requirements"]:
            lines.append("> **Шаги заблокированы.** Сначала подтвердите: "
                         + ", ".join(data["unmet_requirements"])
                         + ". Передайте их в параметре `confirmed`.")
            lines.append("")
        if data["faults"]:
            lines.append("## Найденные неисправности")
            lines += table(["Узел", "Признак", "Уверенность"],
                           [[f["part"], f["symptom"], f"{f['confidence']:.0%}"]
                            for f in data["faults"]])
            lines.append("")
        if data["next_steps"]:
            lines.append("## Что делать дальше")
            for i, s in enumerate(data["next_steps"], 1):
                mark = "🔒 " if s["blocked"] else ""
                line = f"{i}. {mark}{s['action']}"
                if s["target"]:
                    line += f" — {s['target']}"
                if s["tool"]:
                    line += f" (инструмент: {s['tool']})"
                lines.append(line)
                if s["caution"]:
                    lines.append(f"   ⚠ {s['caution']}")
            lines.append("")
        if data["visible_parts"]:
            lines.append("## Что видно на фото")
            lines += bullets(f"{p['name']} — {p['position'] or 'положение неясно'}"
                             + (f", {p['state']}" if p["state"] else "")
                             for p in data["visible_parts"])
            lines.append("")
        if data["tools_needed"]:
            lines.append("## Понадобится")
            lines += bullets(data["tools_needed"])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "device": "Лазерный принтер HP LaserJet", "device_confident": True,
            "stage": "снята правая крышка, виден блок питания",
            "visible_parts": [
                {"name": "плата блока питания", "position": "в центре",
                 "state": "пыль, следы перегрева у радиатора"},
                {"name": "электролитический конденсатор 450В",
                 "position": "слева от радиатора", "state": "верхняя крышка вздута"},
                {"name": "шлейф на плату форматтера", "position": "справа сверху",
                 "state": "норма"},
            ],
            "faults": [{"part": "конденсатор C7", "symptom": "вздутие корпуса, "
                        "вероятная причина отказа питания", "confidence": 0.86}],
            "hazards": ["capacitor", "psu", "laser"],
            "next_steps": [
                {"action": "Отключить принтер от сети и подождать 5 минут",
                 "target": "шнур питания", "tool": "", "caution": ""},
                {"action": "Разрядить конденсатор через резистор 2 кОм",
                 "target": "конденсатор C7 слева от радиатора",
                 "tool": "резистор 2 кОм, изолированный пинцет",
                 "caution": "не замыкать отвёрткой — дуга и порча платы"},
                {"action": "Выпаять и заменить конденсатор",
                 "target": "C7, 450В 100мкФ", "tool": "паяльник 40 Вт",
                 "caution": "соблюсти полярность"},
            ],
            "tools_needed": ["крестовая отвёртка PH2", "паяльник", "резистор 2 кОм",
                             "мультиметр"],
            "unclear": ["маркировка на второй плате не читается"],
        }
        return {"images": [demo_image("printer.jpg", scene)],
                "params": {"problem": "не включается", "skill": "novice"}}


def _detect_hazards(data: dict, parts: list[dict]) -> list[dict[str, Any]]:
    """Опасные узлы: и те, что назвала модель, и те, что видны по названиям деталей."""
    keys: list[str] = []
    for h in data.get("hazards") or []:
        k = str(h).strip().lower()
        if k in HAZARDS:
            keys.append(k)
        else:  # модель могла написать словами — ищем по вхождению
            keys += [name for name in HAZARDS if name in k]

    blob = " ".join(f"{p['name']} {p['state']}" for p in parts).lower()
    words = {"конденсатор": "capacitor", "блок питания": "psu", "аккумулятор": "battery",
             "батаре": "battery", "кинескоп": "crt", "лазер": "laser",
             "топлив": "fuel", "гидравл": "hydraulic", "220": "mains"}
    for word, key in words.items():
        if word in blob:
            keys.append(key)

    out = []
    for key in dict.fromkeys(keys):
        title, requires, warning = HAZARDS[key]
        out.append({"key": key, "title": title, "requires": requires, "warning": warning})
    return out


def _clean_steps(raw: Any) -> list[dict[str, Any]]:
    out = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action") or "").strip()
        if not action:
            continue
        out.append({"action": action, "target": str(s.get("target") or "").strip(),
                    "tool": str(s.get("tool") or "").strip(),
                    "caution": str(s.get("caution") or "").strip(), "blocked": False})
    return out
