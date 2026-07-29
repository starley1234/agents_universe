"""№7 «Urban-Scout» для микро-девелопмента.

Картограф вычитает из участка охранные зоны и отступы и получает
пятно застройки; архитектор «примеряет» типовые объёмы, проверяя
габарит, высотное ограничение, парковку и инсоляцию; аналитик считает
доходность и говорит, стоит ли выкупать.

Геометрия и нормы — код. LLM переводит юридическую формулировку
ограничения в числовой отступ и объясняет решение человеку.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, merge_lists, register, step
from ..data import samples

# упрощённые нормы: коэффициент застройки и парковка на 100 м2
MAX_SITE_COVERAGE = 0.6
PARKING_PER_100M2 = 1.5
PARKING_STALL_M2 = 25.0
INSOLATION_SHADOW_RATIO = 1.2  # длина тени = высота × коэффициент (широта средней полосы)


class UrbanState(BaseState, total=False):
    buildable: Annotated[list[dict], merge_lists]
    fits: Annotated[list[dict], merge_lists]


CARTOGRAPHER = Agent(
    name="cartographer",
    system=(
        "Ты кадастровый инженер. Тебе дают ограничения участка (ЗОУИТ, "
        "охранные зоны, красные линии) человеческим языком. Переведи каждое в "
        "числовой параметр: отступ в метрах от указанной стороны либо предел "
        "высоты в метрах. Если ограничение полностью запрещает застройку — "
        "отметь это."
    ),
    schema_hint={
        "rules": [{"type": "", "side": "", "offset_m": 0, "max_height_m": 0, "blocking": False}],
        "comment": "",
    },
)

ARCHITECT = Agent(
    name="architect",
    system=(
        "Ты архитектор-градостроитель. Тебе дают пятно застройки и результаты "
        "машинной проверки посадки типовых зданий. Объясни, какой сценарий "
        "лучший и что можно сделать с теми, что не влезли (развернуть, "
        "уменьшить, поднять этажность)."
    ),
    schema_hint={"best": "", "reasoning": "", "alternatives": [""]},
)


def node_cartographer(state: dict) -> dict:
    """Вычесть охранные зоны из габарита участка."""
    parcels = state["task"].get("parcels") or samples.parcels()
    out = []
    for p in parcels:
        data = CARTOGRAPHER.run_json(
            json.dumps({"cadastre": p["cadastre"], "zoning": p["zoning"],
                        "shape": p["shape"], "constraints": p["constraints"]}, ensure_ascii=False),
            default={},
        )
        # пустые заготовки схемы («offset_m: 0» без стороны) отбрасываем:
        # это не ограничение, а неотвеченный вопрос
        rules = [
            r for r in (data.get("rules") or [])
            if isinstance(r, dict)
            and (r.get("offset_m") or r.get("max_height_m") or r.get("blocking"))
        ]
        if not rules:  # оффлайн/пустой ответ — берём ограничения как есть
            rules = [dict(c) for c in p["constraints"]]

        w, h = float(p["shape"]["w"]), float(p["shape"]["h"])
        cut = {"north": 0.0, "south": 0.0, "east": 0.0, "west": 0.0}
        height_cap = float("inf")
        height_source = ""
        blocking = []
        for r in rules:
            if r.get("blocking"):
                blocking.append(r.get("type", "ограничение"))
            side = str(r.get("side") or "").lower()
            off = float(r.get("offset_m") or 0)
            if side in cut:
                cut[side] = max(cut[side], off)
            elif off:  # сторона не указана — отступ по периметру
                for k in cut:
                    cut[k] = max(cut[k], off)
            mh = float(r.get("max_height_m") or 0)
            if mh and mh < height_cap:
                height_cap, height_source = mh, r.get("type", "ограничение высоты")

        bw = max(0.0, w - cut["west"] - cut["east"])
        bh = max(0.0, h - cut["north"] - cut["south"])
        out.append({
            **p,
            "buildable": {"w": round(bw, 1), "d": round(bh, 1), "area_m2": round(bw * bh, 1)},
            "height_cap_m": None if height_cap == float("inf") else height_cap,
            "height_source": height_source,
            "cut_m": cut,
            "blocking": blocking,
            "legal_comment": data.get("comment", ""),
        })
    return {"buildable": out, "trace": [step("cartographer", parcels=len(out))]}


def _fit(plot: dict, b: dict) -> dict:
    """Влезает ли объём: габарит (в двух ориентациях), высота, парковка, инсоляция."""
    bw, bd = plot["buildable"]["w"], plot["buildable"]["d"]
    reasons: list[str] = []

    orientations = [(b["w"], b["d"], "как есть"), (b["d"], b["w"], "повёрнуто на 90°")]
    placed = next(((ow, od, name) for ow, od, name in orientations if ow <= bw and od <= bd), None)
    if placed is None:
        reasons.append(f"габарит {b['w']}×{b['d']} м не входит в пятно {bw}×{bd} м")

    cap = plot.get("height_cap_m")
    if cap is not None and b["height_m"] > cap:
        src = plot.get("height_source") or "ЗОУИТ"
        reasons.append(f"высота {b['height_m']} м выше лимита {cap} м ({src})")

    footprint = b["w"] * b["d"]
    if footprint > plot["area_m2"] * MAX_SITE_COVERAGE:
        reasons.append(f"коэффициент застройки выше {MAX_SITE_COVERAGE:.0%}")

    need_parking = max(b["parking"], round(footprint / 100 * PARKING_PER_100M2))
    free_area = plot["buildable"]["area_m2"] - (footprint if placed else 0)
    if free_area < need_parking * PARKING_STALL_M2:
        reasons.append(f"нужно {need_parking} машиномест ({need_parking * PARKING_STALL_M2:.0f} м2), "
                       f"свободно {free_area:.0f} м2")

    # тень не должна выходить за границу участка на соседний надел
    shadow = b["height_m"] * INSOLATION_SHADOW_RATIO
    if shadow > bd:
        reasons.append(f"тень {shadow:.1f} м выходит за границу участка (инсоляция соседей)")

    return {
        "building": b["name"], "fits": not reasons, "orientation": placed[2] if placed else "—",
        "footprint_m2": footprint, "parking_required": need_parking,
        "shadow_m": round(shadow, 1), "reasons": reasons,
        "capex_usd": b["capex_usd"], "noi_usd_year": b["noi_usd_year"],
    }


def node_architect(state: dict) -> dict:
    """Примерка Lego-объёмов на каждое пятно."""
    types = state["task"].get("buildings") or samples.building_types()
    fits = []
    for plot in state["buildable"]:
        variants = [_fit(plot, b) for b in types]
        ok = [v for v in variants if v["fits"]]
        note = ARCHITECT.run_json(
            json.dumps({"parcel": plot["cadastre"], "buildable": plot["buildable"],
                        "variants": variants}, ensure_ascii=False),
            default={},
        ) if variants else {}
        fits.append({"cadastre": plot["cadastre"], "address": plot["address"],
                     "variants": variants, "feasible": ok,
                     "architect_note": note.get("reasoning", "")})
    return {"fits": fits, "trace": [step("architect", plots=len(fits))]}


def node_underwrite(state: dict) -> dict:
    """Экономика: доходность на вложенный капитал и вердикт по выкупу."""
    plots = {p["cadastre"]: p for p in state["buildable"]}
    hurdle = float(state["task"].get("hurdle_yield_pct", 12))
    findings = []
    for f in state["fits"]:
        plot = plots[f["cadastre"]]
        best = None
        for v in f["feasible"]:
            total = plot["price_usd"] + v["capex_usd"]
            yld = v["noi_usd_year"] / total * 100 if total else 0
            cand = {**v, "total_investment_usd": total, "yield_pct": round(yld, 1),
                    "payback_years": round(total / v["noi_usd_year"], 1) if v["noi_usd_year"] else None}
            if best is None or cand["yield_pct"] > best["yield_pct"]:
                best = cand
        blocked = [r for v in f["variants"] for r in v["reasons"]]
        findings.append({
            "cadastre": f["cadastre"], "address": f["address"], "zoning": plot["zoning"],
            "price_usd": plot["price_usd"], "buildable_m2": plot["buildable"]["area_m2"],
            "best": best,
            "verdict": ("покупать" if best and best["yield_pct"] >= hurdle
                        else "мимо" if best else "непригоден"),
            "why_not": sorted(set(blocked))[:4] if not best else [],
            "architect_note": f["architect_note"],
        })
    findings.sort(key=lambda x: -(x["best"]["yield_pct"] if x["best"] else -1))
    return {"findings": findings, "trace": [step("underwriter", deals=len(findings))]}


def node_report(state: dict) -> dict:
    lines = ["# Urban-Scout: скрининг участков", ""]
    lines.append("| Кадастр | Адрес | Цена | Пятно | Сценарий | Доходность | Вердикт |")
    lines.append("|---|---|---|---|---|---|---|")
    for f in state["findings"]:
        b = f["best"]
        lines.append(
            f"| {f['cadastre']} | {f['address']} | ${f['price_usd']:,} | {f['buildable_m2']:.0f} м² | "
            f"{b['building'] if b else '—'} | {str(b['yield_pct']) + '%' if b else '—'} | {f['verdict']} |"
        )
    lines.append("")
    for f in state["findings"]:
        lines.append(f"## {f['cadastre']} — {f['address']} ({f['zoning']})")
        if f["best"]:
            b = f["best"]
            lines.append(f"Лучший сценарий: **{b['building']}** ({b['orientation']}), "
                         f"пятно {b['footprint_m2']:.0f} м², парковка {b['parking_required']} мест.")
            lines.append(f"Вложения ${b['total_investment_usd']:,} → NOI ${b['noi_usd_year']:,}/год, "
                         f"окупаемость {b['payback_years']} лет.")
        else:
            lines.append("Ни один типовой объём не проходит по нормам.")
        if f["why_not"]:
            lines.append("Ограничения: " + "; ".join(f["why_not"]))
        if f["architect_note"]:
            lines.append(f"Архитектор: {f['architect_note']}")
        lines.append("")
    return {"report": "\n".join(lines), "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (CARTOGRAPHER, ARCHITECT):
        a.cfg, a.llm = cfg, None
    g = StateGraph(UrbanState)
    g.add_node("cartographer", node_cartographer)
    g.add_node("architect", node_architect)
    g.add_node("underwriter", node_underwrite)
    g.add_node("report", node_report)
    g.add_edge(START, "cartographer")
    g.add_edge("cartographer", "architect")
    g.add_edge("architect", "underwriter")
    g.add_edge("underwriter", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="urban-scout",
        title="Urban-Scout для микро-девелопмента",
        summary="Кадастр × ЗОУИТ → пятно застройки → посадка типовых объектов и доходность.",
        build=build,
        demo_task=lambda: {"parcels": samples.parcels(), "buildings": samples.building_types(),
                           "hurdle_yield_pct": 12},
        agents=("cartographer", "architect"),
        tags=("realestate", "geo"),
    )
)
