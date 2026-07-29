"""№5 «Спекулянт сложной рецептурой»: реверс ГХ-МС.

Пик хроматограммы → кандидат из базы доступного сырья (по времени
удерживания и масс-спектру) → рецептура в процентах → замена дорогих
компонентов дешёвыми аналогами того же ольфакторного семейства.

Идентификация пиков — арифметика (совпадение m/z и RT), потому что
галлюцинация в CAS-номере стоит партии сырья. LLM работает парфюмером:
решает, чем заменить дорогое, не сломав запах.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, register, step, task_input
from ..data import samples

RT_TOLERANCE = 0.15  # минуты


class FormulaState(BaseState, total=False):
    identified: list[dict]
    recipe: dict[str, Any]


PERFUMER = Agent(
    name="perfumer",
    system=(
        "Ты парфюмер-технолог. Тебе дают распознанную рецептуру и цены сырья. "
        "Предложи замены дорогих компонентов на более дешёвые из того же "
        "ольфакторного семейства, сохранив характер запаха. Для каждой замены "
        "укажи коэффициент пересчёта дозировки (сила запаха отличается) и риск "
        "искажения. Компоненты с ifra_max_pct=0 обязаны быть заменены."
    ),
    schema_hint={
        "substitutions": [{"replace_cas": "", "with_cas": "", "dose_factor": 1.0,
                           "risk": "", "note": ""}],
        "olfactory_comment": "",
    },
)

QA = Agent(
    name="qa_chemist",
    system=(
        "Ты специалист по нормативам IFRA и качеству. Тебе дают итоговую "
        "рецептуру с долями и лимитами. Оцени, пройдёт ли она регуляторно и "
        "будет ли запах близок к референсу. Кратко и по делу."
    ),
    schema_hint={"verdict": "", "similarity_estimate_pct": 0, "warnings": [""]},
)


def _spectral_match(peak_mz: list[int], ref_mz: list[int]) -> float:
    if not peak_mz or not ref_mz:
        return 0.0
    return len(set(peak_mz) & set(ref_mz)) / len(set(peak_mz) | set(ref_mz))


def node_identify(state: dict) -> dict:
    """Сопоставить пики с базой сырья: RT-окно + сходство масс-спектра."""
    task = state["task"]
    data = task_input(task, "gcms", samples.gcms)
    db = task_input(task, "ingredients", samples.ingredient_db)
    identified = []
    for peak in data["peaks"]:
        scored = []
        for ing in db:
            if abs(ing["rt"] - peak["rt"]) > RT_TOLERANCE:
                continue
            sm = _spectral_match(peak["mz"], ing["mz"])
            scored.append((0.4 * (1 - abs(ing["rt"] - peak["rt"]) / RT_TOLERANCE) + 0.6 * sm, sm, ing))
        scored.sort(key=lambda x: -x[0])
        if scored:
            score, sm, ing = scored[0]
            identified.append({
                "rt": peak["rt"], "area_pct": peak["area_pct"],
                "cas": ing["cas"], "name": ing["name"], "family": ing["family"],
                "price_usd_kg": ing["price_usd_kg"], "ifra_max_pct": ing["ifra_max_pct"],
                "odor": ing["odor"], "confidence": round(score, 3), "spectral": round(sm, 3),
                "alternatives": [a[2]["name"] for a in scored[1:3]],
            })
        else:
            identified.append({"rt": peak["rt"], "area_pct": peak["area_pct"],
                               "cas": None, "name": f"неопознан (RT {peak['rt']}, {peak['hint']})",
                               "family": "?", "price_usd_kg": 0, "ifra_max_pct": 100,
                               "odor": "", "confidence": 0.0, "spectral": 0.0, "alternatives": []})
    return {"identified": identified,
            "artifacts": {"db": db, "sample": data["sample"],
                          "target_cost": data.get("target_cost_usd_kg", 0)},
            "trace": [step("peak_matcher", peaks=len(identified),
                           unknown=sum(1 for i in identified if not i["cas"]))]}


def _cost(rows: list[dict]) -> float:
    return round(sum(r["pct"] / 100.0 * r["price_usd_kg"] for r in rows), 2)


def node_recipe(state: dict) -> dict:
    """Нормировать площади пиков в доли рецептуры — это базовая (дорогая) версия."""
    ident = state["identified"]
    total = sum(i["area_pct"] for i in ident) or 1.0
    rows = [{"cas": i["cas"], "name": i["name"], "family": i["family"],
             "pct": round(i["area_pct"] / total * 100, 2),
             "price_usd_kg": i["price_usd_kg"], "ifra_max_pct": i["ifra_max_pct"],
             "odor": i["odor"], "confidence": i["confidence"]}
            for i in ident]
    return {"recipe": {"base_rows": rows, "base_cost_usd_kg": _cost(rows)},
            "trace": [step("recipe_builder", cost=_cost(rows))]}


def node_substitute(state: dict) -> dict:
    """Замены от парфюмера + жёсткое правило по IFRA-запрещённым."""
    db = {i["cas"]: i for i in state["artifacts"]["db"]}
    rows = [dict(r) for r in state["recipe"]["base_rows"]]
    data = PERFUMER.run_json(
        json.dumps({"recipe": rows, "available": state["artifacts"]["db"],
                    "target_cost_usd_kg": state["artifacts"]["target_cost"]}, ensure_ascii=False),
        default={},
    )
    subs = [s for s in (data.get("substitutions") or []) if isinstance(s, dict)]

    # Детерминированная подстраховка: если парфюмер молчит (или модель
    # недоступна), сами предлагаем самый дешёвый аналог того же
    # ольфакторного семейства. Иначе главная ценность продукта —
    # снижение себестоимости — зависела бы от настроения LLM.
    db_list = state["artifacts"]["db"]
    for r in rows:
        if not r["cas"] or any(s_.get("replace_cas") == r["cas"] for s_ in subs):
            continue
        cheaper = [i for i in db_list
                   if i["family"] == r["family"]
                   and i["cas"] != r["cas"]
                   and i["ifra_max_pct"] >= r["pct"]
                   and i["price_usd_kg"] < r["price_usd_kg"]]
        if cheaper:
            alt = min(cheaper, key=lambda i: i["price_usd_kg"])
            subs.append({"replace_cas": r["cas"], "with_cas": alt["cas"], "dose_factor": 1.0,
                         "risk": "средний", "note": "автозамена на дешёвый аналог семейства "
                                                    f"«{r['family']}», требует ольфакторной оценки"})

    # обязательные замены: запрещённое сырьё меняем на дешёвый носитель того же RT
    for r in rows:
        if r["cas"] and r["ifra_max_pct"] == 0 and not any(s.get("replace_cas") == r["cas"] for s in subs):
            alt = next((i for i in state["artifacts"]["db"]
                        if i["family"] == db[r["cas"]]["family"] and i["ifra_max_pct"] > 0), None)
            if alt:
                subs.append({"replace_cas": r["cas"], "with_cas": alt["cas"], "dose_factor": 1.0,
                             "risk": "низкий", "note": "IFRA: исходный компонент недопустим"})

    applied = []
    for s in subs:
        src, dst = db.get(s.get("replace_cas")), db.get(s.get("with_cas"))
        if not src or not dst:
            continue
        row = next((r for r in rows if r["cas"] == src["cas"]), None)
        if row is None or dst["price_usd_kg"] >= src["price_usd_kg"] and src["ifra_max_pct"] > 0:
            continue  # менять дорогое на ещё более дорогое смысла нет
        try:
            factor = float(s.get("dose_factor") or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        factor = min(max(factor, 0.2), 3.0)
        old_pct, old_price = row["pct"], row["price_usd_kg"]
        row.update({"cas": dst["cas"], "name": dst["name"], "family": dst["family"],
                    "pct": round(old_pct * factor, 2), "price_usd_kg": dst["price_usd_kg"],
                    "ifra_max_pct": dst["ifra_max_pct"], "odor": dst["odor"]})
        applied.append({"from": src["name"], "to": dst["name"], "dose_factor": factor,
                        "saved_usd_kg": round((old_price - dst["price_usd_kg"]) * old_pct / 100, 2),
                        "risk": s.get("risk", ""), "note": s.get("note", "")})

    # нормировка обратно к 100% и проверка лимитов IFRA
    total = sum(r["pct"] for r in rows) or 1.0
    for r in rows:
        r["pct"] = round(r["pct"] / total * 100, 2)
    warnings = [f"{r['name']}: {r['pct']}% превышает лимит IFRA {r['ifra_max_pct']}%"
                for r in rows if r["pct"] > r["ifra_max_pct"]]

    base = state["recipe"]["base_cost_usd_kg"]
    new_cost = _cost(rows)
    return {
        "recipe": {**state["recipe"], "final_rows": rows, "final_cost_usd_kg": new_cost,
                   "saving_usd_kg": round(base - new_cost, 2),
                   "olfactory_comment": data.get("olfactory_comment", "")},
        "findings": applied,
        "errors": warnings,
        "trace": [step("perfumer", substitutions=len(applied), cost=new_cost)],
    }


def node_qa(state: dict) -> dict:
    rec = state["recipe"]
    data = QA.run_json(
        json.dumps({"rows": rec["final_rows"], "substitutions": state.get("findings", []),
                    "warnings": state.get("errors", [])}, ensure_ascii=False),
        default={},
    )
    try:
        sim = int(data.get("similarity_estimate_pct") or 0)
    except (TypeError, ValueError):
        sim = 0
    if not sim:  # оценка по доле неизменённого объёма
        changed = sum(f_["saved_usd_kg"] != 0 for f_ in state.get("findings", []))
        sim = max(60, 100 - changed * 7)
    return {"artifacts": {"qa": {**data, "similarity_estimate_pct": sim}},
            "trace": [step("qa_chemist", similarity=sim)]}


def node_report(state: dict) -> dict:
    rec, qa = state["recipe"], state["artifacts"].get("qa", {})
    lines = [f"# Реверс рецептуры: {state['artifacts']['sample']}", ""]
    lines.append(f"Себестоимость оригинального состава: ${rec['base_cost_usd_kg']}/кг → "
                 f"после замен **${rec['final_cost_usd_kg']}/кг** "
                 f"(экономия ${rec['saving_usd_kg']}/кг).")
    lines.append(f"Оценка близости запаха: ~{qa.get('similarity_estimate_pct', '?')}%.")
    lines.append("")
    lines.append("## Итоговая рецептура")
    lines.append("| Компонент | CAS | Доля | $/кг | Семейство |")
    lines.append("|---|---|---|---|---|")
    for r in rec["final_rows"]:
        lines.append(f"| {r['name']} | {r['cas'] or '—'} | {r['pct']}% | "
                     f"{r['price_usd_kg']} | {r['family']} |")
    if state.get("findings"):
        lines.append("")
        lines.append("## Выполненные замены")
        for s in state["findings"]:
            lines.append(f"- {s['from']} → {s['to']} (×{s['dose_factor']}), "
                         f"экономия ${s['saved_usd_kg']}/кг. {s['note']}")
    if state.get("errors"):
        lines.append("")
        lines.append("## Предупреждения IFRA")
        lines += [f"- {w}" for w in state["errors"]]
    unknown = [i for i in state["identified"] if not i["cas"]]
    if unknown:
        lines.append("")
        lines.append("## Неопознанные пики (нужен эталон)")
        lines += [f"- RT {u['rt']}, {u['area_pct']}% площади" for u in unknown]
    return {"report": "\n".join(lines), "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (PERFUMER, QA):
        a.cfg, a.llm = cfg, None
    g = StateGraph(FormulaState)
    g.add_node("peak_matcher", node_identify)
    g.add_node("recipe_builder", node_recipe)
    g.add_node("perfumer", node_substitute)
    g.add_node("qa_chemist", node_qa)
    g.add_node("report", node_report)
    g.add_edge(START, "peak_matcher")
    g.add_edge("peak_matcher", "recipe_builder")
    g.add_edge("recipe_builder", "perfumer")
    g.add_edge("perfumer", "qa_chemist")
    g.add_edge("qa_chemist", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="formula-reverse",
        title="Спекулянт сложной рецептурой",
        summary="ГХ-МС оригинала → рецептура из доступного сырья с проверкой IFRA и себестоимости.",
        build=build,
        demo_task=lambda: {"gcms": samples.gcms(), "ingredients": samples.ingredient_db()},
        agents=("perfumer", "qa_chemist"),
        tags=("chemistry", "cost"),
    )
)
