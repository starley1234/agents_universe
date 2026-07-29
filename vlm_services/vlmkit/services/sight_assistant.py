"""№8 «Помощник для слабовидящих нового поколения».

Кадр с камеры → описание сцены голосом, с приоритетом безопасности.

Здесь цена ошибки выше, чем в остальных одиннадцати сервисах: человек
принимает решение шагнуть, опираясь на наш текст. Отсюда три правила,
зашитые в код, а не в промпт:

1. **Опасности идут первыми.** Ступенька и открытый люк озвучиваются
   раньше, чем цвет вывески, — даже если модель упомянула их последними.
2. **Короткие фразы.** Длинное описание в наушнике бесполезно: пока оно
   договорит, обстановка изменится. Есть жёсткий лимит на длину.
3. **Неуверенность проговаривается.** «Кажется, ступенька» лучше, чем
   уверенное молчание или уверенная ошибка.
"""

from __future__ import annotations

import re
from typing import Any

from ..core import Service, as_float, bullets, register
from ..images import ImageRef

# Категории опасностей в порядке приоритета озвучивания.
HAZARD_ORDER = ("транспорт", "лестница", "ступень", "яма", "люк", "препятствие",
                "стекло", "край платформы", "скользко", "низкий проём")

MODES = ("navigation", "reading", "scene", "shopping")
MAX_SENTENCE_CHARS = 90


@register
class SightAssistantService(Service):
    slug = "sight-assistant"
    title = "Помощник для слабовидящих"
    summary = "Кадр с камеры → короткое описание вслух, опасности в первую очередь."
    tags = ("accessibility", "b2c")
    max_images = 2

    system = (
        "Ты озвучиваешь окружение незрячему человеку. Говори коротко и "
        "конкретно, как человек рядом: сначала то, что опасно или мешает "
        "идти, потом всё остальное. Указывай направление (слева, прямо, "
        "справа) и расстояние в шагах, если можешь его оценить. Не "
        "приукрашивай и не философствуй. Если чего-то не разглядеть, скажи "
        "об этом прямо. Если в кадре есть текст — прочитай его дословно."
    )
    schema = {
        "hazards": [{"what": "", "direction": "", "distance_steps": 0, "confidence": 0.0}],
        "path_clear": True,
        "objects": [{"what": "", "direction": "", "distance_steps": 0}],
        "text_found": [{"text": "", "where": ""}],
        "people": [{"what": "", "direction": ""}],
        "scene": "",
        "lighting": "",
    }

    def analyze(self, images: list[ImageRef], mode: str = "navigation",
                verbosity: str = "short", **params: Any) -> dict[str, Any]:
        mode = mode if mode in MODES else "navigation"
        data = self.ask(
            {"navigation": "Куда можно идти и что мешает? Опиши путь.",
             "reading": "Прочитай весь текст в кадре дословно, укажи структуру.",
             "scene": "Опиши обстановку вокруг.",
             "shopping": "Что за товар передо мной? Прочитай упаковку и цену."}[mode],
            images,
        )

        hazards = _clean_hazards(data.get("hazards"))
        objects = _clean_objects(data.get("objects"))
        texts = [{"text": str(t.get("text") or "").strip(),
                  "where": str(t.get("where") or "").strip()}
                 for t in (data.get("text_found") or [])
                 if isinstance(t, dict) and str(t.get("text") or "").strip()]
        people = [{"what": str(p.get("what") or "человек"),
                   "direction": str(p.get("direction") or "")}
                  for p in (data.get("people") or []) if isinstance(p, dict)]

        speech = _speech(hazards, objects, texts, people, data, mode, verbosity)
        warnings: list[str] = []
        light = str(data.get("lighting") or "").lower()
        if any(w in light for w in ("темн", "тёмн", "плох", "dark", "night")):
            warnings.append("плохое освещение — описание может быть неполным")
        if not hazards and not objects and not texts:
            warnings.append("в кадре ничего не распознано — возможно, камера закрыта")

        urgent = [h for h in hazards if h["urgent"]]
        return {
            "mode": mode,
            "speech": speech,
            "speech_text": " ".join(speech),
            "hazards": hazards,
            "urgent_count": len(urgent),
            "path_clear": bool(data.get("path_clear", True)) and not urgent,
            "objects": objects,
            "text_found": texts,
            "people": people,
            "scene": str(data.get("scene") or ""),
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = [f"# Озвучивание ({data['mode']})", ""]
        if not data["path_clear"]:
            lines.append("> **Внимание: путь не свободен.**")
            lines.append("")
        lines.append("## Что будет сказано")
        lines += bullets(data["speech"])
        lines.append("")
        if data["hazards"]:
            lines.append("## Опасности")
            lines += bullets(
                f"{h['what']} — {h['direction'] or 'направление неясно'}"
                + (f", {h['distance_steps']} шаг(ов)" if h["distance_steps"] else "")
                + f" (уверенность {h['confidence']:.0%})"
                for h in data["hazards"])
            lines.append("")
        if data["text_found"]:
            lines.append("## Найденный текст")
            lines += bullets(f"«{t['text']}»" + (f" — {t['where']}" if t["where"] else "")
                             for t in data["text_found"])
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        scene = {
            "hazards": [
                {"what": "ступенька вниз", "direction": "прямо", "distance_steps": 3,
                 "confidence": 0.9},
                {"what": "велосипед на тротуаре", "direction": "справа",
                 "distance_steps": 5, "confidence": 0.75},
            ],
            "path_clear": False,
            "objects": [{"what": "вход в магазин", "direction": "слева",
                         "distance_steps": 8},
                        {"what": "скамейка", "direction": "справа", "distance_steps": 4}],
            "text_found": [{"text": "Продукты 24 часа", "where": "вывеска слева"}],
            "people": [{"what": "человек идёт навстречу", "direction": "прямо"}],
            "scene": "тротуар вдоль улицы, солнечно",
            "lighting": "хорошее",
        }
        return {"images": [demo_image("street.jpg", scene)],
                "params": {"mode": "navigation"}}


def _clean_hazards(raw: Any) -> list[dict[str, Any]]:
    """Опасности сортируем по срочности, а не по порядку от модели."""
    out = []
    for h in raw or []:
        if not isinstance(h, dict):
            continue
        what = str(h.get("what") or "").strip()
        if not what:
            continue
        steps = max(0, int(as_float(h.get("distance_steps"))))
        conf = round(min(1.0, max(0.0, as_float(h.get("confidence"), 0.6))), 2)
        rank = next((i for i, k in enumerate(HAZARD_ORDER) if k in what.lower()), 99)
        out.append({
            "what": what,
            "direction": str(h.get("direction") or "").strip(),
            "distance_steps": steps,
            "confidence": conf,
            "urgent": (steps <= 4 or steps == 0) and conf >= 0.5,
            "_rank": rank,
        })
    out.sort(key=lambda h: (not h["urgent"], h["_rank"],
                            h["distance_steps"] or 99, -h["confidence"]))
    for h in out:
        h.pop("_rank", None)
    return out


def _clean_objects(raw: Any) -> list[dict[str, Any]]:
    out = []
    for o in raw or []:
        if not isinstance(o, dict):
            continue
        what = str(o.get("what") or "").strip()
        if not what:
            continue
        out.append({"what": what, "direction": str(o.get("direction") or "").strip(),
                    "distance_steps": max(0, int(as_float(o.get("distance_steps"))))})
    return out


def _phrase(what: str, direction: str, steps: int, hedge: bool = False) -> str:
    parts = [what]
    if direction:
        parts.append(direction)
    if steps:
        parts.append(f"{steps} {_steps_word(steps)}")
    text = ", ".join(parts)
    if hedge:
        text = "кажется, " + text
    text = text[0].upper() + text[1:] if text else text
    return _shorten(text) + "."


def _steps_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "шаг"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "шага"
    return "шагов"


def _shorten(text: str, limit: int = MAX_SENTENCE_CHARS) -> str:
    """Длинную фразу в наушнике не дослушают — режем по границе слова."""
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;-")


def _speech(hazards: list[dict], objects: list[dict], texts: list[dict],
            people: list[dict], data: dict, mode: str, verbosity: str) -> list[str]:
    """Собрать реплики: опасности первыми, дальше по режиму."""
    out: list[str] = []
    for h in hazards:
        if h["urgent"] or verbosity != "short":
            out.append(_phrase(h["what"], h["direction"], h["distance_steps"],
                               hedge=h["confidence"] < 0.7))

    if mode == "reading":
        out += [_shorten(f"Написано: {t['text']}") + "." for t in texts]
        return out or ["Текст не найден."]

    if mode == "navigation":
        if not hazards:
            out.append("Путь свободен.")
        for p in people[:2]:
            out.append(_phrase(p["what"], p["direction"], 0))
        limit = 2 if verbosity == "short" else 5
        for o in objects[:limit]:
            out.append(_phrase(o["what"], o["direction"], o["distance_steps"]))
        for t in texts[:1]:
            out.append(_shorten(f"Вывеска: {t['text']}") + ".")
        return out

    # scene / shopping
    if data.get("scene"):
        out.append(_shorten(str(data["scene"])) + ".")
    limit = 3 if verbosity == "short" else 8
    for o in objects[:limit]:
        out.append(_phrase(o["what"], o["direction"], o["distance_steps"]))
    for t in texts[: 2 if verbosity == "short" else 6]:
        out.append(_shorten(f"Надпись: {t['text']}") + ".")
    return out or ["Не удалось разобрать, что перед вами."]
