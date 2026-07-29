"""№3 «AI-Реставратор» технической документации.

Vision-агент отдаёт распознанные символы и линии, конструктор сшивает их
в граф технологической схемы, а генератор выдаёт исполняемые скрипты для
Revit (pyRevit/IronPython) и AutoCAD (AutoLISP).

Сшивка графа — чистая геометрия: LLM не должна «додумывать» топологию
трубопровода, иначе в BIM попадёт вымысел. LLM отвечает за семантику
(что за среда, какая функция у ветки) и за проверку невязок.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, merge_lists, register, step
from ..data import samples

SNAP_PX = 30.0  # допуск привязки конца линии к штуцеру оборудования
TAP_PX = 12.0   # допуск попадания отбора КИП на ось трубопровода

REVIT_FAMILY = {
    "pump": "M_Pump - Centrifugal",
    "gate_valve": "M_Valve - Gate",
    "check_valve": "M_Valve - Check",
    "heat_exchanger": "M_Heat Exchanger - Shell and Tube",
    "tank": "M_Tank - Vertical",
    "instrument": "M_Instrument - Generic",
}


class RestoreState(BaseState, total=False):
    symbols: Annotated[list[dict], merge_lists]
    graph: dict[str, Any]


VISION = Agent(
    name="vision",
    system=(
        "Ты специалист по чтению советских технологических схем 1970-х. Тебе "
        "дают список распознанных на листе примитивов и надписей. Уточни для "
        "каждого символа его роль и привяжи ближайшие надписи (Ду, Ру, марка "
        "стали) к оборудованию или к участку трубопровода."
    ),
    schema_hint={"annotations": [{"id": "", "role": "", "notes": ""}], "medium": ""},
)

DESIGNER = Agent(
    name="designer",
    system=(
        "Ты инженер-конструктор, восстанавливающий П-схему по графу. Тебе дают "
        "узлы и рёбра. Определи направление потока, назови технологические "
        "линии и укажи невязки: висящие концы, отсутствующую арматуру, "
        "неоднозначные соединения. Не выдумывай элементов, которых нет в графе."
    ),
    schema_hint={
        "lines": [{"name": "", "path": [""], "medium": ""}],
        "issues": [{"severity": "", "where": "", "what": ""}],
    },
)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_on_segment(pt, a, b) -> float:
    """Расстояние от точки до отрезка — нужно, чтобы найти врезку КИП в трубу."""
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return _dist(pt, a)
    t = ((pt[0] - ax) * dx + (pt[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return _dist(pt, (ax + t * dx, ay + t * dy))


def node_vision(state: dict) -> dict:
    """Символы + привязка надписей. Геометрия — кодом, смысл — моделью."""
    dwg = state["task"].get("drawing") or samples.drawing()
    syms = [dict(s) for s in dwg["symbols"]]
    for s in syms:  # ближайшая надпись в радиусе — это его подпись
        near = [t for t in dwg["texts"] if _dist(s["xy"], t["xy"]) < 90]
        s["nearby_text"] = [t["text"] for t in near]
    payload = json.dumps({"symbols": syms, "texts": dwg["texts"]}, ensure_ascii=False)
    data = VISION.run_json(f"Лист: {dwg['sheet']}\n{payload}", default={})
    ann = {a.get("id"): a for a in (data.get("annotations") or []) if isinstance(a, dict)}
    for s in syms:
        a = ann.get(s["id"], {})
        s["role"] = a.get("role") or s["type"]
        s["notes"] = a.get("notes") or "; ".join(s["nearby_text"])
    return {
        "symbols": syms,
        "artifacts": {"sheet": dwg["sheet"], "scale": dwg["scale_mm_per_px"],
                      "lines_raw": dwg["lines"], "medium": data.get("medium", "")},
        "trace": [step("vision", symbols=len(syms))],
    }


def _tap_target(pt, state: dict, syms: list[dict]) -> str | None:
    """К какому аппарату отнести врезку датчика в трубопровод."""
    for other in state["artifacts"]["lines_raw"]:
        if other["kind"] != "process" or _point_on_segment(pt, other["a"], other["b"]) > TAP_PX:
            continue
        near = [s for s in syms
                if min(_dist(s["xy"], other["a"]), _dist(s["xy"], other["b"])) <= SNAP_PX]
        if near:
            return min(near, key=lambda s: _dist(s["xy"], pt))["tag"]
    return None


def node_graph(state: dict) -> dict:
    """Каждая линия — ребро, если оба конца притянулись к символам."""
    syms = state["symbols"]
    edges, dangling = [], []
    for i, ln in enumerate(state["artifacts"]["lines_raw"]):
        ends = []
        for pt in (ln["a"], ln["b"]):
            best = min(syms, key=lambda s: _dist(s["xy"], pt))
            if _dist(best["xy"], pt) <= SNAP_PX:
                ends.append(best["tag"])
            elif ln["kind"] == "signal":
                # импульсная линия обрывается на оси трубы: находим эту трубу
                # и относим отбор к ближайшему по ней аппарату
                ends.append(_tap_target(pt, state, syms))
            else:
                ends.append(None)
        if all(ends) and ends[0] != ends[1]:
            edges.append({"from": ends[0], "to": ends[1], "kind": ln["kind"]})
        else:
            dangling.append({"line": i, "ends": ends, "kind": ln["kind"]})
    nodes = [{"tag": s["tag"], "type": s["type"], "role": s["role"], "notes": s["notes"]}
             for s in syms]
    degree = {n["tag"]: 0 for n in nodes}
    for e in edges:
        degree[e["from"]] = degree.get(e["from"], 0) + 1
        degree[e["to"]] = degree.get(e["to"], 0) + 1
    orphans = [t for t, d in degree.items() if d == 0]
    graph = {"nodes": nodes, "edges": edges, "dangling": dangling, "orphans": orphans}
    return {"graph": graph, "trace": [step("graph_builder", nodes=len(nodes), edges=len(edges))]}


def node_designer(state: dict) -> dict:
    """Семантика линий и список невязок для инженера-приёмщика."""
    g = state["graph"]
    data = DESIGNER.run_json(json.dumps(g, ensure_ascii=False), default={})
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
    for d in g["dangling"]:  # машинные невязки добавляем всегда
        issues.append({"severity": "high", "where": f"линия #{d['line']}",
                       "what": "конец не привязан к оборудованию"})
    for o in g["orphans"]:
        issues.append({"severity": "medium", "where": o, "what": "элемент без единого соединения"})
    return {
        "findings": issues,
        "artifacts": {"logical_lines": data.get("lines") or []},
        "trace": [step("designer", issues=len(issues))],
    }


def _revit_script(state: dict) -> str:
    g, scale = state["graph"], state["artifacts"]["scale"]
    syms = {s["tag"]: s for s in state["symbols"]}
    out = [
        '# -*- coding: utf-8 -*-',
        f'# Автогенерация из листа: {state["artifacts"]["sheet"]}',
        '# Запуск: pyRevit -> Run Script. Единицы модели — миллиметры.',
        'from Autodesk.Revit.DB import (Transaction, XYZ, FilteredElementCollector,',
        '                               BuiltInCategory, Structure)',
        'doc = __revit__.ActiveUIDocument.Document',
        'MM_TO_FT = 1.0 / 304.8',
        '',
        'def place(family_name, tag, x_mm, y_mm):',
        '    sym = next((s for s in FilteredElementCollector(doc)',
        '                .OfCategory(BuiltInCategory.OST_MechanicalEquipment)',
        '                .WhereElementIsElementType()',
        '                if s.FamilyName == family_name), None)',
        '    if sym is None:',
        '        print("НЕ НАЙДЕНО семейство: %s (тег %s)" % (family_name, tag))',
        '        return None',
        '    if not sym.IsActive: sym.Activate(); doc.Regenerate()',
        '    pt = XYZ(x_mm * MM_TO_FT, y_mm * MM_TO_FT, 0)',
        '    inst = doc.Create.NewFamilyInstance(pt, sym, Structure.StructuralType.NonStructural)',
        '    inst.LookupParameter("Mark").Set(tag)',
        '    return inst',
        '',
        't = Transaction(doc, "Restore from scan")',
        't.Start()',
        'placed = {}',
    ]
    for n in g["nodes"]:
        s = syms[n["tag"]]
        x, y = s["xy"][0] * scale, s["xy"][1] * scale
        fam = REVIT_FAMILY.get(n["type"], "M_Instrument - Generic")
        out.append(f'placed["{n["tag"]}"] = place("{fam}", "{n["tag"]}", {x:.1f}, {y:.1f})')
    out.append('')
    out.append('# соединения (трассировка труб по осям оборудования)')
    for e in g["edges"]:
        if e["kind"] == "process":
            out.append(f'# PIPE {e["from"]} -> {e["to"]}')
    out += ['t.Commit()', 'print("Размещено: %d" % len([v for v in placed.values() if v]))']
    return "\n".join(out)


def _lisp_script(state: dict) -> str:
    g, scale = state["graph"], state["artifacts"]["scale"]
    syms = {s["tag"]: s for s in state["symbols"]}
    out = [f';; Автогенерация из листа: {state["artifacts"]["sheet"]}',
           '(defun c:RESTORE ( / )', '  (setvar "CMDECHO" 0)',
           '  (command "._LAYER" "_M" "PROCESS" "_C" "1" "" "")']
    for n in g["nodes"]:
        s = syms[n["tag"]]
        x, y = s["xy"][0] * scale, s["xy"][1] * scale
        out.append(f'  (command "._INSERT" "{n["type"].upper()}" (list {x:.1f} {y:.1f} 0.0) 1 1 0)')
        out.append(f'  (command "._TEXT" (list {x:.1f} {y + 400:.1f} 0.0) 250 0 "{n["tag"]}")')
    for e in g["edges"]:
        if e["kind"] != "process":
            continue
        a, b = syms[e["from"]]["xy"], syms[e["to"]]["xy"]
        out.append(f'  (command "._PLINE" (list {a[0]*scale:.1f} {a[1]*scale:.1f} 0.0)'
                   f' (list {b[0]*scale:.1f} {b[1]*scale:.1f} 0.0) "")')
    out += ['  (princ "\\nСхема восстановлена.")', '  (princ)', ')']
    return "\n".join(out)


def node_export(state: dict) -> dict:
    return {
        "artifacts": {"revit_script": _revit_script(state), "autolisp_script": _lisp_script(state)},
        "trace": [step("exporter")],
    }


def node_report(state: dict) -> dict:
    g = state["graph"]
    issues = state.get("findings", [])
    lines = [f"# Реставрация: {state['artifacts']['sheet']}", ""]
    lines.append(f"Восстановлено узлов: {len(g['nodes'])}, связей: {len(g['edges'])}.")
    lines.append(f"Невязок к проверке инженером: {len(issues)}.")
    lines.append("")
    lines.append("## Топология")
    for e in g["edges"]:
        lines.append(f"- {e['from']} → {e['to']} ({e['kind']})")
    if issues:
        lines.append("")
        lines.append("## Требует ручной проверки")
        for i in issues:
            lines.append(f"- [{i.get('severity', '?')}] {i.get('where', '')}: {i.get('what', '')}")
    lines.append("")
    lines.append("Артефакты: `revit_script` (pyRevit), `autolisp_script` (AutoCAD).")
    return {"report": "\n".join(lines), "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (VISION, DESIGNER):
        a.cfg, a.llm = cfg, None
    g = StateGraph(RestoreState)
    g.add_node("vision", node_vision)
    g.add_node("graph_builder", node_graph)
    g.add_node("designer", node_designer)
    g.add_node("exporter", node_export)
    g.add_node("report", node_report)
    g.add_edge(START, "vision")
    g.add_edge("vision", "graph_builder")
    g.add_edge("graph_builder", "designer")
    g.add_edge("designer", "exporter")
    g.add_edge("exporter", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="doc-restorer",
        title="AI-Реставратор техдокументации",
        summary="Синька 70-х → граф схемы → готовые скрипты Revit и AutoCAD.",
        build=build,
        demo_task=lambda: {"drawing": samples.drawing()},
        agents=("vision", "designer"),
        tags=("bim", "vision"),
    )
)
