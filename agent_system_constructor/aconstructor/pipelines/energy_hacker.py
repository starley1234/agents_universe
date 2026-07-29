"""№4 «Энерго-Хакер»: Demand Response Optimization.

Агент-пророк корректирует прогноз цен и нагрузки по погоде, агент-диспетчер
предлагает сдвиги операций, а оптимизатор проверяет их арифметикой:
экономия = дельта по энергии + дельта по demand charge (пиковая мощность
за месяц). Считает деньги код — иначе перед клиентом с performance-based
контрактом не отчитаешься.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, Pipeline as _P, register, step  # noqa: F401
from ..data import samples

HOURS = 24


class EnergyState(BaseState, total=False):
    forecast: dict[str, Any]
    schedule: dict[str, Any]


PROPHET = Agent(
    name="prophet",
    system=(
        "Ты аналитик энергорынка. По базовому профилю цен день-вперёд, погоде "
        "и сезону дай почасовые множители к цене (1.0 = без изменений) и "
        "множители к базовой нагрузке площадки: жара поднимает и цену, и "
        "потребление вентиляции. Ровно 24 значения в каждом списке."
    ),
    schema_hint={"price_multipliers": [1.0] * HOURS, "load_multipliers": [1.0] * HOURS,
                 "comment": ""},
)

DISPATCHER = Agent(
    name="dispatcher",
    system=(
        "Ты диспетчер производства. Тебе дают сменные операции с окнами "
        "допустимого запуска и почасовой прогноз цены. Предложи для каждой "
        "сдвигаемой операции новый час старта так, чтобы уйти из пикового окна "
        "и не сорвать техпроцесс. Не трогай операции с shiftable=false."
    ),
    schema_hint={"moves": [{"id": "", "new_start": 0, "reason": ""}]},
)


def _profile(site: dict, starts: dict[str, int], load_mult: list[float]) -> list[float]:
    """Почасовая мощность площадки при заданных стартах операций."""
    kw = [b * load_mult[h] for h, b in enumerate(site["baseline_kw"])]
    for job in site["jobs"]:
        s = starts.get(job["id"], job["preferred_start"])
        for h in range(s, s + job["hours"]):
            kw[h % HOURS] += job["kw"]
    return kw


def _cost(site: dict, kw: list[float], prices: list[float]) -> dict[str, float]:
    """Счёт = энергия по часам + плата за пик сверх зафиксированного billing peak."""
    tariff = site["tariff"]
    energy = sum(k / 1000.0 * p for k, p in zip(kw, prices))
    peak = max(kw)
    billed_peak = max(peak, tariff["billing_peak_kw"])
    demand = billed_peak * tariff["demand_charge_usd_per_kw"]
    return {"energy_usd": round(energy, 2), "peak_kw": round(peak, 1),
            "demand_usd": round(demand, 2), "total_usd": round(energy + demand, 2)}


def _feasible(job: dict, start: int) -> bool:
    return (
        job.get("shiftable", True)
        and job["earliest"] <= start <= job["latest"]
        and start + job["hours"] <= HOURS
    )


def node_prophet(state: dict) -> dict:
    site = state["task"].get("site") or samples.energy_site()
    data = PROPHET.run_json(
        json.dumps({"prices": site["tariff"]["energy_usd_mwh"], "weather": site["weather"],
                    "baseline_kw": site["baseline_kw"]}, ensure_ascii=False),
        default={},
    )

    def clean(key: str) -> list[float]:
        raw = data.get(key) or []
        vals = [float(v) for v in raw[:HOURS] if isinstance(v, (int, float))]
        vals += [1.0] * (HOURS - len(vals))
        return [min(max(v, 0.5), 2.0) for v in vals]

    pm, lm = clean("price_multipliers"), clean("load_multipliers")
    prices = [p * m for p, m in zip(site["tariff"]["energy_usd_mwh"], pm)]
    return {
        "forecast": {"prices": prices, "load_multipliers": lm, "comment": data.get("comment", "")},
        "artifacts": {"site": site},
        "trace": [step("prophet", peak_price=round(max(prices), 1))],
    }


def node_dispatcher(state: dict) -> dict:
    """Предложения LLM + жадный перебор как страховка и как эталон."""
    site, fc = state["artifacts"]["site"], state["forecast"]
    base_starts = {j["id"]: j["preferred_start"] for j in site["jobs"]}
    base_kw = _profile(site, base_starts, fc["load_multipliers"])
    base_cost = _cost(site, base_kw, fc["prices"])

    data = DISPATCHER.run_json(
        json.dumps({"jobs": site["jobs"], "prices": [round(p, 1) for p in fc["prices"]],
                    "peak_window": site["tariff"]["peak_window_hours"]}, ensure_ascii=False),
        default={},
    )
    jobs_by_id = {j["id"]: j for j in site["jobs"]}
    starts = dict(base_starts)
    accepted, rejected = [], []
    for mv in data.get("moves") or []:
        job = jobs_by_id.get(mv.get("id"))
        if not job:
            continue
        try:
            new = int(mv.get("new_start"))
        except (TypeError, ValueError):
            continue
        if _feasible(job, new):
            starts[job["id"]] = new
            accepted.append({"id": job["id"], "from": base_starts[job["id"]], "to": new,
                             "reason": mv.get("reason", ""), "source": "llm"})
        else:
            rejected.append({"id": job["id"], "new_start": new,
                             "why": "вне технологического окна"})

    # жадное улучшение: по одной операции ищем час с минимальным счётом
    improved = True
    while improved:
        improved = False
        for job in site["jobs"]:
            if not job.get("shiftable", True):
                continue
            cur = starts[job["id"]]
            best, best_cost = cur, _cost(site, _profile(site, starts, fc["load_multipliers"]),
                                         fc["prices"])["total_usd"]
            for h in range(job["earliest"], job["latest"] + 1):
                if h == cur or not _feasible(job, h):
                    continue
                trial = dict(starts)
                trial[job["id"]] = h
                c = _cost(site, _profile(site, trial, fc["load_multipliers"]), fc["prices"])["total_usd"]
                if c < best_cost - 0.01:
                    best, best_cost = h, c
            if best != cur:
                starts[job["id"]] = best
                improved = True

    for job in site["jobs"]:
        jid = job["id"]
        if starts[jid] != base_starts[jid] and not any(a["id"] == jid for a in accepted):
            accepted.append({"id": jid, "from": base_starts[jid], "to": starts[jid],
                             "reason": "оптимизатор: минимум суммарного счёта", "source": "solver"})
        elif starts[jid] != base_starts[jid]:
            for a in accepted:
                if a["id"] == jid:
                    a["to"] = starts[jid]

    opt_kw = _profile(site, starts, fc["load_multipliers"])
    opt_cost = _cost(site, opt_kw, fc["prices"])
    return {
        "schedule": {"base_starts": base_starts, "starts": starts,
                     "base_cost": base_cost, "opt_cost": opt_cost,
                     "base_kw": base_kw, "opt_kw": opt_kw},
        "findings": accepted,
        "errors": [f"отклонено: {r['id']} → {r['new_start']} ({r['why']})" for r in rejected],
        "trace": [step("dispatcher", moves=len(accepted))],
    }


def node_report(state: dict) -> dict:
    site, sch = state["artifacts"]["site"], state["schedule"]
    b, o = sch["base_cost"], sch["opt_cost"]
    saving = round(b["total_usd"] - o["total_usd"], 2)
    # плата за мощность выставляется раз в месяц, энергия — каждый день.
    # Складывать их в «экономию за сутки × 250» нельзя: это завысит эффект
    # в разы, а контракт у нас performance-based.
    energy_saving = round(b["energy_usd"] - o["energy_usd"], 2)
    demand_saving = round(b["demand_usd"] - o["demand_usd"], 2)
    workdays = int(state["task"].get("workdays_per_year", 250))
    annual = round(energy_saving * workdays + demand_saving * 12, 2)
    fee_pct = float(state["task"].get("fee_pct", 20))
    names = {j["id"]: j["name"] for j in site["jobs"]}
    lines = [f"# Demand Response: {site['site']}", ""]
    lines.append(f"Экономия на энергии за сутки: **${energy_saving:,.0f}**.")
    lines.append(f"Снижение платы за мощность: **${demand_saving:,.0f}/мес** "
                 f"(пик {b['peak_kw']:,.0f} → {o['peak_kw']:,.0f} кВт).")
    lines.append(f"Годовой эффект: ~${annual:,.0f} "
                 f"({energy_saving:,.0f} × {workdays} дней + {demand_saving:,.0f} × 12 мес).")
    lines.append(f"Наш гонорар {fee_pct:.0f}% от годовой экономии = ${annual * fee_pct / 100:,.0f}.")
    lines.append("")
    lines.append("| Показатель | Было | Стало |")
    lines.append("|---|---|---|")
    lines.append(f"| Энергия | ${b['energy_usd']:,.0f} | ${o['energy_usd']:,.0f} |")
    lines.append(f"| Пик, кВт | {b['peak_kw']:,.0f} | {o['peak_kw']:,.0f} |")
    lines.append(f"| Плата за мощность | ${b['demand_usd']:,.0f} | ${o['demand_usd']:,.0f} |")
    lines.append(f"| Итого | ${b['total_usd']:,.0f} | ${o['total_usd']:,.0f} |")
    lines.append("")
    if state.get("findings"):
        lines.append("## Что сдвинуть")
        for m in state["findings"]:
            lines.append(f"- {names.get(m['id'], m['id'])}: {m['from']}:00 → {m['to']}:00 — {m['reason']}")
    else:
        lines.append("Сдвиги не требуются: план уже оптимален.")
    if state.get("errors"):
        lines.append("")
        lines.append("## Отклонённые предложения")
        lines += [f"- {e}" for e in state["errors"]]
    return {"report": "\n".join(lines),
            "artifacts": {"saving_usd": saving, "energy_saving_usd": energy_saving,
                          "demand_saving_usd_month": demand_saving,
                          "annual_saving_usd": annual,
                          "fee_usd": round(annual * fee_pct / 100, 2)},
            "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (PROPHET, DISPATCHER):
        a.cfg, a.llm = cfg, None
    g = StateGraph(EnergyState)
    g.add_node("prophet", node_prophet)
    g.add_node("dispatcher", node_dispatcher)
    g.add_node("report", node_report)
    g.add_edge(START, "prophet")
    g.add_edge("prophet", "dispatcher")
    g.add_edge("dispatcher", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="energy-hacker",
        title="Энерго-Хакер (Demand Response)",
        summary="Прогноз цен и погоды × план производства → сдвиги операций и деньги на счёте.",
        build=build,
        demo_task=lambda: {"site": deepcopy(samples.energy_site()), "fee_pct": 20},
        agents=("prophet", "dispatcher"),
        tags=("energy", "optimization"),
    )
)
