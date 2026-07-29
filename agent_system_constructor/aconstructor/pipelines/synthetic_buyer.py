"""№2 «Синтетический Байер» для неликвидных запчастей.

Агент-технарь превращает чертёж в спецификацию (параметры + допустимые
аналоги), агент-разведчик читает «мусорные» прайсы и вытаскивает
параметры из свободного текста, а матчер сводит их по физике, а не по
артикулу — потому что артикул на складе банкрота почти всегда неверный.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, merge_lists, register, step, task_input
from ..data import samples
from ..textutil import cosine, norm_code

# Параметры, по которым деталь опознаётся однозначно; вес = важность.
SPEC_WEIGHTS = {
    "pressure_bar": 1.0,
    "flow_lpm": 0.8,
    "voltage_vdc": 1.0,
    "coil_ohm": 0.9,
    "interface": 1.2,
    "material": 0.6,
    "seals": 0.7,
}
TOLERANCE = {"pressure_bar": 0.05, "flow_lpm": 0.10, "voltage_vdc": 0.01, "coil_ohm": 0.05}


class BuyerState(BaseState, total=False):
    spec: dict[str, Any]
    candidates: Annotated[list[dict], merge_lists]


ENGINEER = Agent(
    name="engineer",
    system=(
        "Ты инженер-снабженец по гидравлике и авиакомпонентам. По описанию "
        "чертежа выпиши измеримые физические параметры детали и перечисли "
        "признаки, по которым её можно опознать в чужом прайсе без артикула. "
        "Числа — числами, без единиц в значении."
    ),
    schema_hint={
        "params": {"pressure_bar": 0, "flow_lpm": 0, "voltage_vdc": 0, "coil_ohm": 0,
                   "interface": "", "material": "", "seals": ""},
        "synonyms": [""],
        "must_match": [""],
    },
)

SCOUT = Agent(
    name="scout",
    system=(
        "Ты разведчик по складским остаткам. Тебе дают строку прайс-листа "
        "мелкого поставщика: мусорный текст, часто с неверным артикулом. "
        "Извлеки физические параметры детали из описания."
    ),
    schema_hint={
        "params": {"pressure_bar": 0, "flow_lpm": 0, "voltage_vdc": 0, "coil_ohm": 0,
                   "interface": "", "material": "", "seals": ""},
        "category": "",
    },
)


# --- эвристический экстрактор: страховка, когда LLM оффлайн или врёт -------
_PATTERNS = {
    "pressure_bar": r"(\d{2,4})\s*(?:bar|бар)",
    "flow_lpm": r"(\d{1,4})\s*(?:lpm|l/min|л/мин)",
    "voltage_vdc": r"(\d{1,3})\s*(?:vdc|v dc|в\s*пост|v\b)",
    "coil_ohm": r"(\d{1,4})\s*(?:ohm|ом)\b",
}


def extract_params(text: str) -> dict[str, Any]:
    low = (text or "").lower()
    out: dict[str, Any] = {}
    for key, pat in _PATTERNS.items():
        found = [float(m) for m in re.findall(pat, low)]
        if found:
            # «38 lpm at 70 bar drop»: рабочее давление — наибольшее из названных,
            # перепад и прочие вторичные числа всегда меньше номинала
            out[key] = max(found) if key == "pressure_bar" else found[0]
    iso = re.search(r"iso\s*\d{3,6}[\w\-]*", low)
    if iso:
        out["interface"] = iso.group(0)
    size = re.search(r"size\s*0?(\d{1,2})", low)
    if size:
        out["interface"] = (out.get("interface", "") + f" size {size.group(1)}").strip()
    for mat in ("17-4ph", "stainless", "cast iron", "aluminium", "нержав"):
        if mat in low:
            out["material"] = mat
            break
    for seal in ("fkm", "viton", "nbr", "epdm"):
        if seal in low:
            out["seals"] = seal
            break
    return out


def _merge(primary: dict, fallback: dict) -> dict:
    out = dict(fallback)
    for k, v in (primary or {}).items():
        if v not in (None, "", 0, 0.0):
            out[k] = v
    return out


def node_engineer(state: dict) -> dict:
    req = task_input(state["task"], "request", samples.part_request)
    text = f"{req['name']}\n{req['drawing_notes']}"
    data = ENGINEER.run_json(text, default={})
    params = _merge(data.get("params") or {}, extract_params(text))
    spec = {
        "part_number": req["part_number"],
        "name": req["name"],
        "params": params,
        "synonyms": data.get("synonyms") or [],
        "must_match": data.get("must_match") or [],
        "reference_text": text,
        "list_price_usd": req.get("list_price_usd", 0),
        "quantity": req.get("quantity", 1),
    }
    return {"spec": spec, "trace": [step("engineer", params=len(params))]}


def node_scout(state: dict) -> dict:
    """Каждый лот прайса — в нормализованные параметры."""
    listings = task_input(state["task"], "listings", samples.supplier_listings)
    out = []
    for lot in listings:
        data = SCOUT.run_json(f"{lot['raw_code']} | {lot['text']}", default={})
        params = _merge(data.get("params") or {}, extract_params(lot["text"]))
        out.append({**lot, "params": params})
    return {"candidates": out, "trace": [step("scout", listings=len(out))]}


MISSING_PENALTY = 0.4  # «не указано» — это неопределённость, а не противоречие


def _param_score(want: dict, have: dict) -> tuple[float, list[str], list[str]]:
    """Взвешенное совпадение параметров. Возвращает (0..1, совпало, разошлось).

    Умолчание поставщика штрафуется мягче прямого расхождения: в мусорных
    прайсах половину характеристик просто не пишут, и лот с тремя из семи
    совпавших параметров и нулём конфликтов — хороший кандидат на запрос фото.
    """
    hit = denom = 0.0
    ok: list[str] = []
    bad: list[str] = []
    for key, weight in SPEC_WEIGHTS.items():
        w = want.get(key)
        if w in (None, "", 0, 0.0):
            continue
        h = have.get(key)
        if h in (None, "", 0, 0.0):
            denom += weight * MISSING_PENALTY
            continue
        denom += weight
        if isinstance(w, (int, float)) and isinstance(h, (int, float)):
            tol = TOLERANCE.get(key, 0.05)
            if abs(h - w) <= abs(w) * tol:
                hit += weight
                ok.append(f"{key}={h:g}")
            else:
                bad.append(f"{key}: нужно {w:g}, есть {h:g}")
        else:
            sw, sh = str(w).lower(), str(h).lower()
            if sw in sh or sh in sw or cosine(sw, sh) > 0.5:
                hit += weight
                ok.append(f"{key}={h}")
            else:
                bad.append(f"{key}: нужно {w}, есть {h}")
    return (hit / denom if denom else 0.0), ok, bad


def node_match(state: dict) -> dict:
    """Свести спецификацию с лотами: физика + текст, артикул — лишь бонус."""
    spec = state["spec"]
    want = spec["params"]
    results = []
    for lot in state.get("candidates", []):
        pscore, ok, bad = _param_score(want, lot["params"])
        tscore = cosine(spec["reference_text"], lot["text"])
        code_hit = norm_code(lot["raw_code"]) == norm_code(spec["part_number"])
        score = 0.65 * pscore + 0.25 * tscore + (0.10 if code_hit else 0.0)
        results.append({
            **{k: lot[k] for k in ("id", "supplier", "raw_code", "price_usd", "qty",
                                   "condition", "country")},
            "param_score": round(pscore, 3),
            "text_score": round(tscore, 3),
            "code_match": code_hit,
            "score": round(score, 3),
            "matched": ok,
            "mismatched": bad,
        })
    results.sort(key=lambda r: -r["score"])
    return {"artifacts": {"ranked": results}, "trace": [step("matcher", ranked=len(results))]}


def node_deal(state: dict) -> dict:
    """Экономика: что купить, за сколько перепродать, какая маржа."""
    spec = state["spec"]
    min_score = float(state["task"].get("min_score", 0.55))
    resale_ratio = float(state["task"].get("resale_ratio", 0.5))
    qty_needed = spec["quantity"]
    findings = []
    for r in state["artifacts"]["ranked"]:
        if r["score"] < min_score:
            continue
        units = min(qty_needed, r["qty"])
        buy = r["price_usd"] * units
        resale = spec["list_price_usd"] * resale_ratio * units
        findings.append({
            **r,
            "units": units,
            "buy_total_usd": buy,
            "resale_usd": round(resale),
            "margin_usd": round(resale - buy),
            "confidence": "высокая" if r["score"] >= 0.8 else "средняя",
            "action": "закупать" if r["score"] >= 0.8 and not r["mismatched"] else "запросить фото/сертификат",
        })
    return {"findings": findings, "trace": [step("deal", offers=len(findings))]}


def node_report(state: dict) -> dict:
    spec = state["spec"]
    fnd = state.get("findings", [])
    lines = [f"# Поиск: {spec['name']} ({spec['part_number']})", ""]
    lines.append("Параметры из чертежа: " + ", ".join(f"{k}={v}" for k, v in spec["params"].items()))
    lines.append("")
    if not fnd:
        lines.append("Подходящих лотов не найдено.")
    else:
        lines.append("| Лот | Поставщик | Цена | Совпадение | Маржа | Действие |")
        lines.append("|---|---|---|---|---|---|")
        for f in fnd:
            lines.append(
                f"| {f['id']} | {f['supplier']} | ${f['price_usd']:,} | {f['score']:.0%} | "
                f"${f['margin_usd']:,} | {f['action']} |"
            )
        lines.append("")
        for f in fnd:
            lines.append(f"## {f['id']} — {f['supplier']} ({f['country']})")
            lines.append(f"Артикул в прайсе: `{f['raw_code']}`"
                         + ("" if f["code_match"] else " — не совпадает, опознано по параметрам."))
            if f["matched"]:
                lines.append("Совпало: " + ", ".join(f["matched"]))
            if f["mismatched"]:
                lines.append("Расхождения: " + "; ".join(f["mismatched"]))
            lines.append("")
    return {"report": "\n".join(lines), "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (ENGINEER, SCOUT):
        a.cfg, a.llm = cfg, None
    g = StateGraph(BuyerState)
    g.add_node("engineer", node_engineer)
    g.add_node("scout", node_scout)
    g.add_node("matcher", node_match)
    g.add_node("deal", node_deal)
    g.add_node("report", node_report)
    g.add_edge(START, "engineer")
    g.add_edge(START, "scout")
    g.add_edge("engineer", "matcher")
    g.add_edge("scout", "matcher")
    g.add_edge("matcher", "deal")
    g.add_edge("deal", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="synthetic-buyer",
        title="Синтетический Байер",
        summary="Чертёж → физическая спецификация → поиск детали в мусорных прайсах по описанию, а не по ID.",
        build=build,
        demo_task=lambda: {
            "request": samples.part_request(),
            "listings": samples.supplier_listings(),
            "min_score": 0.55,
        },
        agents=("engineer", "scout"),
        tags=("sourcing", "arbitrage"),
    )
)
