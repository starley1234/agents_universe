"""№1 «Арбитраж патентной чистоты».

Три агента, как в ТЗ:
  scout   — разбирает свежие патенты в независимые признаки (claim elements);
  product — вытаскивает из README/описания продукта техническую суть;
  legal   — ищет пересечение и выносит вердикт по правилу «all elements».

Ключевое решение: вердикт не отдан на откуп LLM. Агенты формулируют
признаки, а покрытие считает `textutil.coverage` — так результат
воспроизводим и его можно показать юристу как таблицу.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..core import Agent, BaseState, Pipeline, agent_node, merge_lists, register, step
from ..data import samples
from ..textutil import coverage, cosine

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class PatentState(BaseState, total=False):
    patents: Annotated[list[dict], merge_lists]
    features: Annotated[list[dict], merge_lists]


SCOUT = Agent(
    name="patent_scout",
    system=(
        "Ты патентный аналитик USPTO/WIPO. Тебе дают текст независимого пункта "
        "формулы. Разбей его на атомарные технические признаки (claim elements): "
        "короткие фразы 2-6 слов, каждая — отдельный ограничительный признак. "
        "Отдельно укажи область техники."
    ),
    schema_hint={"domain": "", "elements": [""]},
)

PRODUCT = Agent(
    name="product_analyst",
    system=(
        "Ты reverse-engineering инженер. По README репозитория или описанию "
        "продукта восстанови, что система делает технически: алгоритмы, "
        "структуры данных, порядок операций. Пиши фактами, без маркетинга."
    ),
    schema_hint={"capabilities": [""], "tech_summary": ""},
)

LEGAL = Agent(
    name="legal_llm",
    system=(
        "Ты патентный поверенный. Тебе дают признаки патента и признаки "
        "продукта, а также посчитанное машиной покрытие. Оцени риск нарушения "
        "по доктрине all-elements, назови обходной путь (design-around) и "
        "оцени сумму потенциального иска в USD."
    ),
    schema_hint={
        "risk": "medium",
        "rationale": "",
        "design_around": "",
        "exposure_usd": 0,
    },
)


def node_scout(state: dict) -> dict:
    """Каждый свежий патент — в набор признаков."""
    task = state["task"]
    out: list[dict] = []
    for pat in task.get("patents", samples.patents()):
        data = SCOUT.run_json(
            f"Патент {pat['id']} «{pat['title']}».\nФормула:\n{pat['claim']}",
            default={"domain": pat.get("domain", ""), "elements": []},
        )
        elements = [e.strip() for e in (data.get("elements") or [])
                    if isinstance(e, str) and e.strip()]
        if not elements:  # оффлайн-модель или пустой ответ — падаем на текст формулы
            elements = [c.strip() for c in pat["claim"].split(",") if len(c.strip()) > 12]
        out.append({**pat, "elements": elements, "domain": data.get("domain") or pat.get("domain", "")})
    return {"patents": out, "trace": [step("patent_scout", patents=len(out))]}


def node_product(state: dict) -> dict:
    """Продукт стартапа — в перечень технических возможностей."""
    task = state["task"]
    product = task.get("product") or samples.product()
    data = PRODUCT.run_json(
        f"Продукт «{product['name']}».\nОписание:\n{product['description']}",
        default={"capabilities": [], "tech_summary": product["description"]},
    )
    caps = [c.strip() for c in (data.get("capabilities") or [])
            if isinstance(c, str) and c.strip()]
    corpus = product["description"] + " " + " ".join(caps) + " " + str(data.get("tech_summary", ""))
    return {
        "artifacts": {"product": {**product, "capabilities": caps, "corpus": corpus}},
        "trace": [step("product_analyst", capabilities=len(caps))],
    }


def node_match(state: dict) -> dict:
    """Детерминированное пересечение: сколько признаков патента покрыто продуктом."""
    corpus = state["artifacts"]["product"]["corpus"]
    features: list[dict] = []
    for pat in state.get("patents", []):
        cov, hits = coverage(pat["elements"], corpus)
        features.append(
            {
                "patent_id": pat["id"],
                "title": pat["title"],
                "assignee": pat.get("assignee", ""),
                "coverage": round(cov, 3),
                "similarity": round(cosine(pat["claim"], corpus), 3),
                "matched": hits,
                "total_elements": len(pat["elements"]),
            }
        )
    features.sort(key=lambda f: (-f["coverage"], -f["similarity"]))
    return {"features": features, "trace": [step("claim_matcher", scored=len(features))]}


def node_legal(state: dict) -> dict:
    """Юридический вердикт — только по кандидатам с реальным пересечением."""
    threshold = float(state["task"].get("threshold", 0.4))
    findings: list[dict] = []
    for f in state.get("features", []):
        if f["coverage"] < threshold:
            continue
        verdict = LEGAL.run_json(
            "Патент: {t} ({p}, правообладатель {a}).\n"
            "Покрыто признаков: {m} из {n} ({c:.0%}).\n"
            "Совпавшие признаки: {hits}\n"
            "Продукт: {prod}".format(
                t=f["title"], p=f["patent_id"], a=f["assignee"],
                m=len(f["matched"]), n=f["total_elements"], c=f["coverage"],
                hits="; ".join(f["matched"]) or "—",
                prod=state["artifacts"]["product"]["corpus"][:1200],
            ),
            default={},
        )
        risk = str(verdict.get("risk") or "").lower()
        # вердикт без обоснования не считается вердиктом: степень риска
        # выводим из измеренного покрытия признаков
        if risk not in RISK_ORDER or not str(verdict.get("rationale") or "").strip():
            risk = "critical" if f["coverage"] >= 0.8 else "high" if f["coverage"] >= 0.6 else "medium"
        findings.append(
            {
                **f,
                "risk": risk,
                "rationale": verdict.get("rationale", ""),
                "design_around": verdict.get("design_around", ""),
                "exposure_usd": int(verdict.get("exposure_usd") or 0),
            }
        )
    findings.sort(key=lambda x: -RISK_ORDER[x["risk"]])
    return {"findings": findings, "trace": [step("legal_llm", flagged=len(findings))]}


def node_report(state: dict) -> dict:
    prod = state["artifacts"]["product"]["name"]
    fnd = state.get("findings", [])
    lines = [f"# Патентная чистота: {prod}", ""]
    if not fnd:
        lines.append("Пересечений выше порога не найдено. Проверено патентов: "
                     f"{len(state.get('patents', []))}.")
    else:
        lines.append(f"Найдено {len(fnd)} рисковых пересечений.\n")
        lines.append("| Патент | Правообладатель | Покрытие | Риск | Экспозиция |")
        lines.append("|---|---|---|---|---|")
        for f in fnd:
            lines.append(
                f"| {f['patent_id']} | {f['assignee']} | {f['coverage']:.0%} | "
                f"{f['risk']} | ${f['exposure_usd']:,} |"
            )
        lines.append("")
        for f in fnd:
            lines.append(f"## {f['patent_id']} — {f['title']}")
            lines.append(f"Совпало признаков: {len(f['matched'])}/{f['total_elements']}.")
            if f["rationale"]:
                lines.append(f"Обоснование: {f['rationale']}")
            if f["design_around"]:
                lines.append(f"Обход: {f['design_around']}")
            lines.append("")
    return {"report": "\n".join(lines), "trace": [step("report")]}


def build(cfg: Settings | None = None):
    for a in (SCOUT, PRODUCT, LEGAL):
        a.cfg, a.llm = cfg, None
    g = StateGraph(PatentState)
    g.add_node("patent_scout", node_scout)
    g.add_node("product_analyst", node_product)
    g.add_node("claim_matcher", node_match)
    g.add_node("legal_llm", node_legal)
    g.add_node("report", node_report)
    # разведка патентов и разбор продукта независимы — идут параллельно
    g.add_edge(START, "patent_scout")
    g.add_edge(START, "product_analyst")
    g.add_edge("patent_scout", "claim_matcher")
    g.add_edge("product_analyst", "claim_matcher")
    g.add_edge("claim_matcher", "legal_llm")
    g.add_edge("legal_llm", "report")
    g.add_edge("report", END)
    return g.compile()


register(
    Pipeline(
        slug="patent-clearance",
        title="Арбитраж патентной чистоты",
        summary="Свежие патенты × описание продукта → карта рисков нарушения и обходные пути.",
        build=build,
        demo_task=lambda: {"patents": samples.patents(), "product": samples.product(), "threshold": 0.4},
        agents=("patent_scout", "product_analyst", "legal_llm"),
        tags=("legal", "monitoring"),
    )
)
