"""№6 «Анализатор трендов из Instagram/TikTok».

Пачка кадров с метаданными → визуальные признаки → растущие тренды.

Смысл продукта — поймать тренд **до** массовости, поэтому решает не
частота, а динамика: признак, встречавшийся в свежих постах вдвое чаще,
чем в старых, интереснее того, что стабильно популярен. Модель называет
признаки на кадрах, а рост, долю и уверенность считает код по датам и
вовлечённости.

Честная оговорка: по десятку кадров тренд не доказать. Сервис явно
помечает выводы, под которыми мало наблюдений.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..core import Service, as_float, as_int, bullets, pct, register, table
from ..images import ImageRef

MIN_OBSERVATIONS = 5  # меньше — статистики нет, только гипотеза


@register
class TrendScoutService(Service):
    slug = "trend-scout"
    title = "Анализатор визуальных трендов"
    summary = "Кадры из соцсетей → растущие визуальные признаки с динамикой."
    tags = ("marketing", "trends")
    max_images = 8

    system = (
        "Ты аналитик визуальных трендов в моде и интерьере. Для каждого "
        "изображения перечисли конкретные визуальные признаки: цветовая "
        "палитра, фасон, крой, материал, стиль съёмки, локация, реквизит. "
        "Пиши признаки короткими устойчивыми формулировками, одинаковыми для "
        "похожих кадров, чтобы их можно было сравнивать между собой."
    )
    schema = {
        "frames": [{"name": "", "features": [""], "palette": [""], "style": "",
                    "objects": [""]}],
    }

    def analyze(self, images: list[ImageRef], meta: list[dict] | None = None,
                niche: str = "", **params: Any) -> dict[str, Any]:
        data = self.ask(
            f"Опиши визуальные признаки {len(images)} кадров."
            + (f" Ниша: {niche}." if niche else ""),
            images,
        )
        frames = [f for f in (data.get("frames") or []) if isinstance(f, dict)]
        meta = meta or []

        # сопоставляем кадры с метаданными по порядку — так их прислал клиент
        observations: list[dict[str, Any]] = []
        for idx, frame in enumerate(frames):
            m = meta[idx] if idx < len(meta) else {}
            observations.append({
                "name": str(frame.get("name") or (images[idx].name if idx < len(images) else idx)),
                "features": _norm_features(frame),
                "ts": _parse_ts(m.get("date") or m.get("ts")),
                "engagement": as_float(m.get("engagement") or m.get("likes"), 0.0),
            })

        stats = _aggregate(observations)
        median_ts = _median_ts(observations)
        trends = _score_trends(stats, len(observations), median_ts)

        rising = [t for t in trends if t["direction"] == "растёт"]
        warnings: list[str] = []
        if len(observations) < MIN_OBSERVATIONS:
            warnings.append(
                f"кадров всего {len(observations)} — это гипотезы, а не тренды; "
                f"для статистики нужно от {MIN_OBSERVATIONS}, а лучше сотни")
        if not any(o["ts"] for o in observations):
            warnings.append("нет дат публикации — динамику посчитать нельзя, "
                            "показана только частота")
        if not any(o["engagement"] for o in observations):
            warnings.append("нет данных о вовлечённости — вес признаков по популярности "
                            "не учтён")

        return {
            "niche": niche or None,
            "frames_analyzed": len(observations),
            "trends": trends,
            "rising": rising[:10],
            "palette": _top_palette(observations),
            "_warnings": warnings,
        }

    def report(self, data: dict[str, Any], images: list[ImageRef], **params: Any) -> str:
        lines = ["# Визуальные тренды", ""]
        if data["niche"]:
            lines.append(f"Ниша: {data['niche']}. Кадров: {data['frames_analyzed']}.")
        lines.append("")
        if data["rising"]:
            lines.append("## Набирают популярность")
            lines += table(["Признак", "Встречается", "Доля", "Динамика", "Уверенность"],
                           [[t["feature"], t["count"], f"{t['share_pct']}%",
                             f"{t['growth']:+.0%}", t["confidence"]]
                            for t in data["rising"]])
            lines.append("")
        if data["trends"]:
            lines.append("## Все признаки")
            lines += table(["Признак", "Кадров", "Доля", "Статус"],
                           [[t["feature"], t["count"], f"{t['share_pct']}%", t["direction"]]
                            for t in data["trends"][:20]])
            lines.append("")
        if data["palette"]:
            lines.append("## Палитра")
            lines.append(", ".join(f"{c} ({n})" for c, n in data["palette"]))
        return "\n".join(lines)

    def demo(self) -> dict[str, Any]:
        from ..demo import demo_image

        recent = ["бархат", "глубокий бордо", "объёмные рукава"]
        older = ["минимализм", "бежевая палитра"]
        frames = []
        meta = []
        for i in range(6):
            fresh = i < 3
            feats = (recent if fresh else older) + ["съёмка при дневном свете"]
            frames.append(demo_image(f"post{i}.jpg", {
                "frames": [{"name": f"post{i}", "features": feats,
                            "palette": ["бордо", "бежевый"] if fresh else ["бежевый"],
                            "style": "lifestyle", "objects": ["одежда"]}]}))
            meta.append({"date": f"2026-0{7 if fresh else 3}-1{i}",
                         "engagement": 5000 if fresh else 1200})
        return {"images": frames, "params": {"meta": meta, "niche": "женская одежда"}}


def _norm_features(frame: dict) -> list[str]:
    """Признаки приводим к нижнему регистру: «Бархат» и «бархат» — одно и то же."""
    out: list[str] = []
    for key in ("features", "palette", "objects"):
        for f in frame.get(key) or []:
            s = str(f).strip().lower()
            if s and s not in out:
                out.append(s)
    style = str(frame.get("style") or "").strip().lower()
    if style and style not in out:
        out.append(style)
    return out


def _parse_ts(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value)[:19], fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _median_ts(obs: list[dict]) -> float | None:
    stamps = sorted(o["ts"] for o in obs if o["ts"])
    if len(stamps) < 2:
        return None
    mid = len(stamps) // 2
    return stamps[mid] if len(stamps) % 2 else (stamps[mid - 1] + stamps[mid]) / 2


def _aggregate(obs: list[dict]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "engagement": 0.0, "stamps": []})
    for o in obs:
        for f in o["features"]:
            s = stats[f]
            s["count"] += 1
            s["engagement"] += o["engagement"]
            if o["ts"]:
                s["stamps"].append(o["ts"])
    return stats


def _score_trends(stats: dict, total: int, median_ts: float | None) -> list[dict[str, Any]]:
    """Динамика = доля в свежей половине минус доля в старой."""
    out = []
    for feature, s in stats.items():
        count = s["count"]
        growth = 0.0
        direction = "стабильно"
        if median_ts and s["stamps"]:
            new = sum(1 for t in s["stamps"] if t >= median_ts)
            old = len(s["stamps"]) - new
            if new or old:
                growth = (new - old) / max(1, new + old)
            direction = "растёт" if growth > 0.3 else "угасает" if growth < -0.3 else "стабильно"
        confidence = _confidence(count, total)
        out.append({
            "feature": feature,
            "count": count,
            "share_pct": pct(count, total),
            "growth": round(growth, 2),
            "direction": direction,
            "avg_engagement": round(s["engagement"] / count) if count else 0,
            "confidence": confidence,
        })
    return sorted(out, key=lambda t: (-t["growth"], -t["count"]))


def _confidence(count: int, total: int) -> str:
    if total < MIN_OBSERVATIONS or count < 2:
        return "гипотеза"
    if count >= max(3, total * 0.4):
        return "высокая"
    return "средняя"


def _top_palette(obs: list[dict], limit: int = 8) -> list[tuple[str, int]]:
    colors: dict[str, int] = defaultdict(int)
    words = ("бордо", "бежев", "чёрн", "белый", "син", "зелён", "красн", "розов",
             "серый", "золот", "терракот", "лаванд")
    for o in obs:
        for f in o["features"]:
            if any(w in f for w in words):
                colors[f] += 1
    return sorted(colors.items(), key=lambda kv: -kv[1])[:limit]
