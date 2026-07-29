"""№6 «Авто-Валидатор» для сертификации.

Агент-инженер раскладывает доказательную базу (логи тестов, чертежи,
FMEA) по разделам досье, агент-редактор пишет текст языком регулятора,
а ревьюер играет за FDA/нотифицированный орган и выписывает deficiency
letter. Цикл «пиши → ругай → правь» повторяется, пока раздел не пройдёт
или не кончится лимит итераций — это условное ребро LangGraph.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, register, step
from ..data import samples

MAX_ROUNDS = 2


class CertState(BaseState, total=False):
    coverage: dict[str, Any]
    # без merge_lists: разделы переписываются на каждом круге правок, а не копятся
    sections: list[dict]
    round: int


ENGINEER = Agent(
    name="engineer",
    system=(
        "Ты инженер по сертификации медицинских изделий. Тебе дают перечень "
        "разделов досье и имеющиеся доказательства. Для каждого раздела скажи, "
        "какие доказательства его закрывают и чего физически не хватает "
        "(конкретный тест, протокол, расчёт)."
    ),
    schema_hint={"mapping": [{"section": "", "evidence_ids": [""], "gaps": [""]}]},
)

WRITER = Agent(
    name="writer",
    system=(
        "Ты технический писатель, обученный на IEC 60601-1, ISO 14971 и "
        "руководствах FDA 510(k). Пиши раздел досье: сухо, в третьем лице, "
        "со ссылками на пункты стандарта и на идентификаторы доказательств. "
        "Никаких маркетинговых утверждений и никаких данных, которых нет во "
        "входных доказательствах."
    ),
    schema_hint={"title": "", "body": "", "cited_evidence": [""], "standard_refs": [""]},
)

REVIEWER = Agent(
    name="reviewer",
    system=(
        "Ты рецензент регулятора. Прочитай раздел досье и выпиши замечания "
        "(deficiencies) так, как это сделал бы FDA: что не подтверждено "
        "данными, где нет ссылки на стандарт, где формулировка допускает "
        "двойное толкование. Если замечаний нет — верни пустой список."
    ),
    schema_hint={"deficiencies": [{"severity": "", "text": ""}], "accepted": True},
)


def node_engineer(state: dict) -> dict:
    proj = state["task"].get("project") or samples.cert_project()
    data = ENGINEER.run_json(
        json.dumps({"sections": proj["sections"], "evidence": proj["evidence"]}, ensure_ascii=False),
        default={},
    )
    by_section = {m.get("section"): m for m in (data.get("mapping") or []) if isinstance(m, dict)}
    coverage = {}
    for sec in proj["sections"]:
        declared = by_section.get(sec, {})
        # доказательства берём из самих данных: covers — это факт, а не мнение модели
        actual = [e["id"] for e in proj["evidence"] if sec in e.get("covers", [])]
        gaps = [g for g in (declared.get("gaps") or []) if isinstance(g, str)]
        if not actual and not gaps:
            gaps = ["нет ни одного доказательства, закрывающего раздел"]
        drafts = [e["id"] for e in proj["evidence"]
                  if sec in e.get("covers", []) and e.get("result") in ("draft", "pending")]
        if drafts:
            gaps.append(f"доказательства в статусе draft: {', '.join(drafts)}")
        coverage[sec] = {"evidence_ids": actual, "gaps": gaps,
                         "ready": bool(actual) and not drafts}
    ready = sum(1 for c in coverage.values() if c["ready"])
    return {"coverage": coverage, "round": 0,
            "artifacts": {"project": proj},
            "trace": [step("engineer", ready=ready, total=len(coverage))]}


def node_writer(state: dict) -> dict:
    """Пишем разделы. На повторном круге — только те, что завернул ревьюер."""
    proj = state["artifacts"]["project"]
    rnd = state.get("round", 0)
    ev = {e["id"]: e for e in proj["evidence"]}
    prior = {s["section"]: s for s in state.get("sections", [])}
    todo = [s for s in proj["sections"] if rnd == 0 or not prior.get(s, {}).get("accepted", True)]
    out = []
    for sec in todo:
        cov = state["coverage"][sec]
        evidence = [ev[i] for i in cov["evidence_ids"]]
        feedback = ""
        if sec in prior and prior[sec].get("deficiencies"):
            feedback = "\nЗамечания регулятора к прошлой редакции (устрани их):\n" + "\n".join(
                f"- [{d.get('severity', '')}] {d.get('text', '')}" for d in prior[sec]["deficiencies"]
            )
        data = WRITER.run_json(
            f"Изделие: {proj['product']}\nСтандарт: {proj['standard']}\nРаздел: {sec}\n"
            f"Доказательства: {json.dumps(evidence, ensure_ascii=False)}\n"
            f"Пробелы: {cov['gaps']}{feedback}",
            default={},
        )
        out.append({
            "section": sec,
            "title": data.get("title") or sec,
            "body": data.get("body") or "",
            "cited_evidence": data.get("cited_evidence") or cov["evidence_ids"],
            "standard_refs": data.get("standard_refs") or [],
            "gaps": cov["gaps"],
            "round": rnd,
            "accepted": False,
            "deficiencies": [],
        })
    merged = {**prior, **{s["section"]: s for s in out}}
    ordered = [merged[s] for s in proj["sections"] if s in merged]
    return {"sections": ordered, "trace": [step("writer", written=len(out), round=rnd)]}


def node_reviewer(state: dict) -> dict:
    """Регулятор придирается. Незакрытые пробелы — автоматический deficiency."""
    latest: dict[str, dict] = {}
    for s in state.get("sections", []):
        latest[s["section"]] = s
    reviewed = []
    for sec, s in latest.items():
        if s.get("accepted"):
            reviewed.append(s)
            continue
        data = REVIEWER.run_json(
            json.dumps({"section": sec, "body": s["body"], "cited": s["cited_evidence"],
                        "refs": s["standard_refs"]}, ensure_ascii=False),
            default={},
        )
        defs_ = [d for d in (data.get("deficiencies") or []) if isinstance(d, dict) and d.get("text")]
        for g in s["gaps"]:
            defs_.append({"severity": "major", "text": f"Не закрыт пробел: {g}"})
        if not s["standard_refs"]:
            defs_.append({"severity": "minor", "text": "Нет ссылок на пункты стандарта."})
        reviewed.append({**s, "deficiencies": defs_, "accepted": not defs_})
    ordered = [latest[k] for k in latest]
    ordered = [next(r for r in reviewed if r["section"] == o["section"]) for o in ordered]
    blocked = sum(1 for r in ordered if not r["accepted"])
    return {"sections": ordered, "round": state.get("round", 0) + 1,
            "trace": [step("reviewer", blocked=blocked)]}


def route(state: dict) -> str:
    """Ещё круг правок или сборка досье."""
    if state.get("round", 0) >= MAX_ROUNDS:
        return "assemble"
    return "writer" if any(not s.get("accepted") for s in state.get("sections", [])) else "assemble"


def node_assemble(state: dict) -> dict:
    proj = state["artifacts"]["project"]
    latest: dict[str, dict] = {s["section"]: s for s in state.get("sections", [])}
    ordered = [latest[s] for s in proj["sections"] if s in latest]
    doc = [f"# Досье на соответствие: {proj['product']}", f"_{proj['standard']}_", ""]
    open_items = []
    for s in ordered:
        doc.append(f"## {s['title']}")
        doc.append(s["body"] or "_(текст не сформирован — недостаточно данных)_")
        if s["cited_evidence"]:
            doc.append(f"\nДоказательства: {', '.join(s['cited_evidence'])}.")
        if s["standard_refs"]:
            doc.append(f"Ссылки: {', '.join(s['standard_refs'])}.")
        doc.append("")
        for d in s.get("deficiencies", []):
            open_items.append({"section": s["section"], **d})
    ready = sum(1 for s in ordered if s.get("accepted"))
    return {
        "artifacts": {"dossier_md": "\n".join(doc), "ready_sections": ready,
                      "total_sections": len(ordered)},
        "findings": open_items,
        "trace": [step("assemble", ready=ready, open_items=len(open_items))],
    }


def node_report(state: dict) -> dict:
    a = state["artifacts"]
    proj = a["project"]
    lines = [f"# Готовность досье: {proj['product']}", ""]
    lines.append(f"Разделов принято рецензентом: **{a['ready_sections']}/{a['total_sections']}**.")
    lines.append(f"Открытых замечаний: {len(state.get('findings', []))}.")
    lines.append("")
    if state.get("findings"):
        lines.append("## Что закрыть до подачи")
        lines.append("| Раздел | Критичность | Замечание |")
        lines.append("|---|---|---|")
        for d in state["findings"]:
            lines.append(f"| {d['section']} | {d.get('severity', '')} | {d.get('text', '')} |")
    else:
        lines.append("Блокирующих замечаний нет — досье готово к подаче.")
    lines.append("")
    lines.append("Полный текст досье — в артефакте `dossier_md`.")
    return {"report": "\n".join(lines), "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (ENGINEER, WRITER, REVIEWER):
        a.cfg, a.llm = cfg, None
    g = StateGraph(CertState)
    g.add_node("engineer", node_engineer)
    g.add_node("writer", node_writer)
    g.add_node("reviewer", node_reviewer)
    g.add_node("assemble", node_assemble)
    g.add_node("report", node_report)
    g.add_edge(START, "engineer")
    g.add_edge("engineer", "writer")
    g.add_edge("writer", "reviewer")
    g.add_conditional_edges("reviewer", route, {"writer": "writer", "assemble": "assemble"})
    g.add_edge("assemble", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="cert-validator",
        title="Авто-Валидатор сертификации",
        summary="Логи тестов и чертежи → досье ISO/FDA с внутренним циклом рецензирования.",
        build=build,
        demo_task=lambda: {"project": samples.cert_project()},
        agents=("engineer", "writer", "reviewer"),
        tags=("regulatory", "docs"),
    )
)
