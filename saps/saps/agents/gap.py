"""Агент-Gap-аналитик: поиск «дыр» в покрытии (ТЗ п.3.2).

ЧТО СЧИТАЕТСЯ ДЫРОЙ. Пять видов, по убыванию тяжести:

  no_rule_link   — требование не привязано ни к одному пункту АП
                   (подтверждённому). Непонятно, зачем оно в базисе.
  no_moc         — не назначен метод подтверждения соответствия.
                   Требование есть, доказывать его нечем.
  no_evidence    — MoC назначен, но ни одного документа не приложено.
                   Самая частая дыра перед сдачей регулятору.
  status_conflict— пункт помечен «соответствует», а доказательства нет.
                   Опаснее пустого места: создаёт ложную уверенность.
  low_quality    — формулировка ниже порога качества, подтверждать
                   нечего (см. Агент-Редактор).

ПОЧЕМУ ЭТОТ АГЕНТ БЕЗ LLM. Здесь нечего понимать: покрытие — это
структурный факт из базы. Спрашивать модель «есть ли доказательство»
означало бы получить вероятностный ответ на детерминированный вопрос.
Единственное, что агент делает «умного», — предлагает подходящий MoC по
типу требования, и это тоже правила, а не догадки: расчётное требование
-> MC2, требование к оборудованию -> MC9, и так далее.

ИНДИКАТОР «ЗДОРОВЬЯ СЕРТИФИКАЦИИ» (ТЗ п.3.3) считается здесь же:
health() сводит покрытие по узлу изделия в один процент готовности и
список того, что мешает. Считается по тем же данным, что показывает
дашборд, — иначе цифры разойдутся.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..db.schema import MOC_CODES
from .base import Agent, AgentReport

#: Правила подсказки метода подтверждения по содержанию требования.
#: Порядок важен: первое совпадение выигрывает. Это эвристика для
#: ЧЕРНОВИКА плана сертификации, окончательный выбор — за инженером.
MOC_HINTS: list[tuple[str, str, str]] = [
    (r"лётн\w* испытан|в полёте|в полете|полётн\w* провер", "MC6",
     "речь о поведении в полёте — подтверждается лётными испытаниями"),
    (r"наземн\w* испытан|на изделии|на самолёте|на самолете", "MC5",
     "проверка на собранном изделии — наземные испытания"),
    (r"стендов|лаборатор|образц\w* материал|испытан\w* на прочность", "MC4",
     "проверка на образцах/стенде — лабораторные испытания"),
    (r"отказобезопасн|отказн\w* состоян|вероятност\w* отказа|катастрофическ|"
     r"безопасност\w* полёт", "MC3",
     "требование к безопасности при отказах — оценка безопасности"),
    (r"расчёт|расчет|прочност|нагрузк|масс\w|центровк|аэродинамическ|"
     r"не менее|не более", "MC2",
     "количественное требование — подтверждается расчётом/анализом"),
    (r"оборудован|прибор|блок|агрегат\w* поставк|покупн\w* издел", "MC9",
     "требование к оборудованию — квалификация оборудования"),
    (r"моделирован|имитацион|цифров\w* двойник", "MC8",
     "проверка моделированием"),
    (r"чертёж|чертеж|конструктивн|компоновк|размещен|маркировк|табличк", "MC1",
     "конструктивное требование — анализ проекта, чертежи"),
    (r"осмотр|инспекц|контрол\w* качества|аудит", "MC7",
     "подтверждается инспекцией/аудитом"),
]

#: Вес каждого фактора в индикаторе здоровья. Сумма = 1.0.
#: Доказательство весит больше всего: именно его спрашивает регулятор.
HEALTH_WEIGHTS = {
    "rule_link": 0.2,
    "moc": 0.25,
    "evidence": 0.35,
    "quality": 0.2,
}


@dataclass
class Gap:
    requirement_id: int
    external_id: str
    kind: str
    message: str
    severity: str = "major"

    def to_dict(self) -> dict[str, Any]:
        return {"requirement_id": self.requirement_id,
                "external_id": self.external_id, "kind": self.kind,
                "message": self.message, "severity": self.severity}


def suggest_moc(text: str) -> tuple[str, str]:
    """Подсказать метод подтверждения по тексту требования.

    Возвращает (код, обоснование). Пустой код — подсказки нет, и это
    честнее случайного MC2.
    """
    low = (text or "").lower()
    for pattern, moc, reason in MOC_HINTS:
        if re.search(pattern, low):
            return moc, reason
    return "", ""


class GapAgent(Agent):
    """Ищет дыры в покрытии и предлагает методы подтверждения."""

    name = "gap"

    def run(self, *, owner: str = "", node_code: str = "",
            suggest_moc_for_gaps: bool = True) -> AgentReport:
        report = self._report()
        rows = self.store.coverage(node_code=node_code, owner=owner)

        for row in rows:
            report.processed += 1
            req_id = int(row["id"])
            external = row["external_id"]
            gaps: list[Gap] = []

            if int(row["links"]) == 0:
                gaps.append(Gap(req_id, external, "no_rule_link",
                                "Не подтверждена связь ни с одним пунктом "
                                "авиационных правил"))
            if int(row["moc_count"]) == 0:
                gaps.append(Gap(req_id, external, "no_moc",
                                "Не назначен метод подтверждения соответствия "
                                "(MoC)"))
            elif int(row["evidence_count"]) == 0:
                gaps.append(Gap(req_id, external, "no_evidence",
                                "Метод подтверждения назначен, но не приложено "
                                "ни одного доказательного документа"))
            if int(row["compliant_count"]) > 0 and int(row["evidence_count"]) == 0:
                gaps.append(Gap(req_id, external, "status_conflict",
                                "Пункт помечен как «соответствует», но "
                                "доказательства отсутствуют — статус ничем "
                                "не обеспечен", "critical"))
            score = row.get("quality_score")
            if score is not None and float(score) < self.cfg.quality_min_score:
                gaps.append(Gap(req_id, external, "low_quality",
                                f"Качество формулировки {float(score):.2f} ниже "
                                f"порога {self.cfg.quality_min_score:.2f} — "
                                "требование трудно подтвердить", "minor"))

            if not gaps:
                continue
            report.findings.extend(g.to_dict() for g in gaps)

            if suggest_moc_for_gaps and any(g.kind == "no_moc" for g in gaps):
                req = self.store.get_requirement(req_id)
                moc, reason = suggest_moc(req["text"] if req else "")
                if moc:
                    self._suggest(
                        report, req_id, kind="moc", payload={"moc": moc},
                        rationale=f"{reason}. Предлагается {moc} — "
                                  f"{MOC_CODES[moc]}. Окончательный выбор "
                                  f"метода за инженером.")
                else:
                    report.add_skip(
                        req_id, external,
                        "не удалось подсказать метод подтверждения по тексту — "
                        "назначьте MoC вручную")

        self._log(report)
        return report

    # ------------------------------------------------------------------
    def health(self, *, node_code: str = "", owner: str = "") -> dict[str, Any]:
        """Индикатор «здоровья сертификации» (ТЗ п.3.3).

        Один процент готовности + разбор, из чего он складывается.
        Проценты без объяснения бесполезны: инженеру нужно знать, что
        именно закрыть, чтобы цифра выросла.
        """
        rows = self.store.coverage(node_code=node_code, owner=owner)
        total = len(rows)
        if total == 0:
            return {"node_code": node_code, "owner": owner, "total": 0,
                    "health": 0.0, "status": "нет данных",
                    "factors": {}, "gaps": {}, "blocking": []}

        with_link = sum(1 for r in rows if int(r["links"]) > 0)
        with_moc = sum(1 for r in rows if int(r["moc_count"]) > 0)
        with_evidence = sum(1 for r in rows if int(r["evidence_count"]) > 0)
        good_quality = sum(
            1 for r in rows
            if r["quality_score"] is None
            or float(r["quality_score"]) >= self.cfg.quality_min_score)

        factors = {
            "rule_link": with_link / total,
            "moc": with_moc / total,
            "evidence": with_evidence / total,
            "quality": good_quality / total,
        }
        health = sum(factors[k] * w for k, w in HEALTH_WEIGHTS.items())

        gaps = {
            "no_rule_link": total - with_link,
            "no_moc": total - with_moc,
            "no_evidence": total - with_evidence,
            "low_quality": total - good_quality,
            "status_conflict": sum(
                1 for r in rows
                if int(r["compliant_count"]) > 0 and int(r["evidence_count"]) == 0),
        }
        blocking = [r["external_id"] for r in rows
                    if int(r["compliant_count"]) > 0
                    and int(r["evidence_count"]) == 0][:20]

        return {
            "node_code": node_code, "owner": owner, "total": total,
            "health": round(health, 4),
            "status": _health_label(health),
            "factors": {k: round(v, 4) for k, v in factors.items()},
            "weights": HEALTH_WEIGHTS,
            "gaps": gaps,
            "blocking": blocking,
        }

    def health_by_node(self) -> list[dict[str, Any]]:
        """Здоровье по каждому узлу изделия — для дашборда."""
        out = []
        for node in self.store.list_nodes():
            info = self.health(node_code=node["code"])
            if info["total"]:
                info["node_name"] = node.get("name", "")
                out.append(info)
        out.sort(key=lambda i: i["health"])
        return out


def _health_label(value: float) -> str:
    if value >= 0.9:
        return "готово к проверке"
    if value >= 0.7:
        return "почти готово"
    if value >= 0.4:
        return "в работе"
    return "не готово"
