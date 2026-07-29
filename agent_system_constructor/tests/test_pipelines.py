"""Сквозные прогоны всех семи пайплайнов на демо-данных (оффлайн-LLM)."""

from __future__ import annotations

import copy

import pytest

from aconstructor import REGISTRY, load_registry, run_pipeline
from aconstructor.data import samples

load_registry()


@pytest.mark.parametrize("slug", sorted(REGISTRY))
def test_pipeline_runs_end_to_end(slug):
    r = run_pipeline(slug)
    assert r["report"].startswith("#"), "отчёт должен быть markdown-документом"
    assert r["trace"], "трасса не должна быть пустой"
    assert "report" in [t["node"] for t in r["trace"]]


# --- №1 ------------------------------------------------------------------
def test_patent_finds_the_infringing_one_only():
    r = run_pipeline("patent-clearance")
    ids = {f["patent_id"] for f in r["findings"]}
    assert "US11987654B2" in ids, "дедупликация скользящим хешем — прямое пересечение"
    assert "US11955001B2" not in ids, "патент про аккумуляторы не относится к делу"


def test_patent_threshold_filters_everything():
    r = run_pipeline("patent-clearance", {"patents": samples.patents(),
                                          "product": samples.product(), "threshold": 0.99})
    assert r["findings"] == []


def test_patent_unrelated_product_is_clean():
    r = run_pipeline("patent-clearance", {
        "patents": samples.patents(),
        "product": {"name": "Ферма", "description": "Мы продаём саженцы яблонь и груш."},
        "threshold": 0.4,
    })
    assert r["findings"] == []


# --- №2 ------------------------------------------------------------------
def test_buyer_ranks_cheap_correct_lot_first():
    r = run_pipeline("synthetic-buyer")
    ranked = r["artifacts"]["ranked"]
    assert ranked[0]["id"] == "L-1", "нужный клапан за $480 должен быть первым"
    assert ranked[-1]["id"] in ("L-3", "L-4"), "шестерённый насос и клапан на 24В — в конце"


def test_buyer_identifies_part_without_matching_code():
    """Ключевая ценность: лот L-2 опознан по параметрам, артикул другой."""
    r = run_pipeline("synthetic-buyer")
    l2 = next(x for x in r["artifacts"]["ranked"] if x["id"] == "L-2")
    assert not l2["code_match"]
    assert l2["param_score"] > 0.6
    assert "seals=fkm" in l2["matched"]


def test_buyer_margin_is_positive():
    r = run_pipeline("synthetic-buyer")
    assert r["findings"], "хотя бы один лот должен пройти порог"
    assert all(f["margin_usd"] > 0 for f in r["findings"])


# --- №3 ------------------------------------------------------------------
def test_restorer_builds_connected_graph():
    r = run_pipeline("doc-restorer")
    g = r["graph"]
    assert len(g["nodes"]) == 9
    tags = {n["tag"] for n in g["nodes"]}
    assert {"Н-1", "ЗД-11", "ПВД-5", "Д-1"} <= tags
    assert {"from": "Н-1", "to": "ЗД-11", "kind": "process"} in g["edges"]


def test_restorer_emits_runnable_scripts():
    r = run_pipeline("doc-restorer")
    revit = r["artifacts"]["revit_script"]
    lisp = r["artifacts"]["autolisp_script"]
    compile(revit, "revit.py", "exec")  # синтаксически валидный Python
    assert "M_Pump - Centrifugal" in revit
    assert 'place("M_Pump - Centrifugal", "Н-1"' in revit
    assert lisp.count("(") == lisp.count(")"), "скобки AutoLISP должны быть сбалансированы"
    assert "(defun c:RESTORE" in lisp


def test_restorer_flags_signal_line_and_reports_issues():
    r = run_pipeline("doc-restorer")
    kinds = {e["kind"] for e in r["graph"]["edges"]}
    assert "signal" in kinds, "линия от датчика PT-104 — сигнальная, не технологическая"
    assert "# PIPE" in r["artifacts"]["revit_script"]


# --- №4 ------------------------------------------------------------------
def test_energy_saves_money_and_cuts_peak():
    r = run_pipeline("energy-hacker")
    sch = r["schedule"]
    assert sch["opt_cost"]["total_usd"] < sch["base_cost"]["total_usd"]
    assert r["artifacts"]["saving_usd"] > 0
    assert sch["opt_cost"]["peak_kw"] <= sch["base_cost"]["peak_kw"]


def test_energy_never_moves_fixed_job():
    r = run_pipeline("energy-hacker")
    assert r["schedule"]["starts"]["J4"] == 8, "линия формовки не сдвигаема"


def test_energy_respects_time_windows():
    r = run_pipeline("energy-hacker")
    jobs = {j["id"]: j for j in samples.energy_site()["jobs"]}
    for jid, start in r["schedule"]["starts"].items():
        j = jobs[jid]
        assert j["earliest"] <= start <= j["latest"]
        assert start + j["hours"] <= 24


def test_energy_fee_follows_annual_savings():
    r = run_pipeline("energy-hacker", {"site": copy.deepcopy(samples.energy_site()), "fee_pct": 30})
    a = r["artifacts"]
    assert a["fee_usd"] == pytest.approx(a["annual_saving_usd"] * 0.3, rel=1e-3)


def test_energy_annualization_does_not_multiply_monthly_demand_charge():
    """Плата за мощность — месячная; её нельзя умножать на число рабочих дней."""
    r = run_pipeline("energy-hacker")
    a = r["artifacts"]
    naive = a["saving_usd"] * 250
    assert a["annual_saving_usd"] < naive
    assert a["annual_saving_usd"] == pytest.approx(
        a["energy_saving_usd"] * 250 + a["demand_saving_usd_month"] * 12, rel=1e-6
    )


# --- №5 ------------------------------------------------------------------
def test_formula_identifies_peaks_and_cuts_cost():
    r = run_pipeline("formula-reverse")
    rec = r["recipe"]
    names = {row["name"] for row in rec["final_rows"]}
    assert "d-Limonene" in names
    assert rec["final_cost_usd_kg"] < rec["base_cost_usd_kg"]
    assert rec["saving_usd_kg"] > 0


def test_formula_swaps_expensive_musk_without_llm():
    """Дорогой мускус должен уйти на дешёвый аналог того же семейства."""
    r = run_pipeline("formula-reverse")
    names = {row["name"] for row in r["recipe"]["final_rows"]}
    assert "Habanolide (macrocyclic musk)" not in names
    assert r["recipe"]["saving_usd_kg"] > 10


def test_formula_substitution_respects_ifra_headroom():
    """Замена не должна приводить к превышению лимита IFRA."""
    r = run_pipeline("formula-reverse")
    for row in r["recipe"]["final_rows"]:
        if row["cas"]:
            assert row["pct"] <= row["ifra_max_pct"] + 0.5, row["name"]


def test_formula_replaces_banned_solvent():
    """Дибутилфталат (IFRA=0) обязан исчезнуть из итоговой рецептуры."""
    r = run_pipeline("formula-reverse")
    cas = {row["cas"] for row in r["recipe"]["final_rows"]}
    assert "84-74-2" not in cas


def test_formula_percentages_sum_to_100():
    r = run_pipeline("formula-reverse")
    total = sum(row["pct"] for row in r["recipe"]["final_rows"])
    assert total == pytest.approx(100.0, abs=0.5)


def test_formula_reports_unidentified_peak():
    gc = samples.gcms()
    gc["peaks"].append({"rt": 30.0, "area_pct": 5.0, "mz": [999], "hint": "неизвестно"})
    r = run_pipeline("formula-reverse", {"gcms": gc, "ingredients": samples.ingredient_db()})
    assert any(i["cas"] is None for i in r["identified"])
    assert "Неопознанные пики" in r["report"]


# --- №6 ------------------------------------------------------------------
def test_cert_covers_all_sections_once():
    r = run_pipeline("cert-validator")
    secs = [s["section"] for s in r["sections"]]
    assert len(secs) == len(set(secs)), "разделы не должны дублироваться между кругами"
    assert set(secs) == set(samples.cert_project()["sections"])


def test_cert_flags_missing_evidence():
    r = run_pipeline("cert-validator")
    texts = " ".join(d["text"] for d in r["findings"])
    assert "Biocompatibility (ISO 10993)" in {d["section"] for d in r["findings"]}
    assert "пробел" in texts.lower()


def test_cert_flags_draft_evidence():
    r = run_pipeline("cert-validator")
    gaps = " ".join(g for s in r["sections"] for g in s["gaps"])
    assert "E6" in gaps, "доказательство в статусе draft должно быть отмечено"


def test_cert_stops_after_max_rounds():
    r = run_pipeline("cert-validator")
    assert r["round"] <= 2
    assert r["artifacts"]["dossier_md"].startswith("# Досье")


# --- №7 ------------------------------------------------------------------
def test_urban_subtracts_setbacks():
    r = run_pipeline("urban-scout")
    p = next(x for x in r["buildable"] if x["cadastre"] == "77:04:0002015:118")
    assert p["buildable"]["d"] == 20.0, "40 м минус 20 м охранной зоны ЛЭП"
    assert p["buildable"]["w"] == 60.0


def test_urban_enforces_height_limit():
    r = run_pipeline("urban-scout")
    plot = next(f for f in r["fits"] if f["cadastre"] == "77:04:0002016:044")
    warehouse = next(v for v in plot["variants"] if v["building"] == "Склад-лайт 600 м2")
    assert not warehouse["fits"]
    assert any("выше лимита 8.0 м" in reason for reason in warehouse["reasons"])


def test_urban_ranks_by_yield_and_gives_verdict():
    r = run_pipeline("urban-scout")
    yields = [f["best"]["yield_pct"] for f in r["findings"] if f["best"]]
    assert yields == sorted(yields, reverse=True)
    assert all(f["verdict"] in ("покупать", "мимо", "непригоден") for f in r["findings"])


def test_urban_rejects_overpriced_parcel():
    parcels = copy.deepcopy(samples.parcels())
    for p in parcels:
        p["price_usd"] = 50_000_000
    r = run_pipeline("urban-scout", {"parcels": parcels, "buildings": samples.building_types(),
                                     "hurdle_yield_pct": 12})
    assert all(f["verdict"] != "покупать" for f in r["findings"])
