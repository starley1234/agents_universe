"""Агент-Классификатор: сопоставление требований с пунктами АП (ТЗ п.3.2).

ДВА ЭТАПА, И ПЕРВЫЙ РАБОТАЕТ БЕЗ МОДЕЛИ.

  Этап 1 — ОТБОР КАНДИДАТОВ векторным поиском по pgvector (плюс
  подстраховка по ключевым словам, если у пункта ещё нет эмбеддинга).
  Дёшево, воспроизводимо, не требует внешнего API.

  Этап 2 — УТОЧНЕНИЕ моделью, опционально. Модель выбирает из УЖЕ
  ОТОБРАННОГО списка и не может назвать пункт, которого в нём нет.
  Ограничение принципиальное: без него модель уверенно «вспоминает»
  пункты вроде «АП-25.1309(b)», которых нет в загруженном справочнике
  или которые звучат правдоподобно, но относятся к другому разделу.
  Такая привязка попадёт в сертификационный базис и всплывёт на защите
  перед регулятором.

ПОЧЕМУ РЕЗУЛЬТАТ — НЕПОДТВЕРЖДЁННАЯ СВЯЗЬ, А НЕ ФАКТ. Классификатор
создаёт requirement_rule_link с confirmed=false и предложение для
инженера. В отчёт о соответствии попадают только подтверждённые связи
(см. Store.coverage): система не имеет права утверждать за человека, к
какому пункту правил относится требование.

ЧЕСТНОСТЬ ПОРОГА. Если лучший кандидат ниже classify_min_score, агент
НЕ предлагает ничего и записывает это в пропуски. Навязать инженеру
случайный пункт хуже, чем честно сказать «не знаю»: непроверенная
привязка создаёт видимость покрытия там, где его нет.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from ..llm.base import BaseLLM, LLMError
from ..llm.embeddings import BaseEmbedder, EmbeddingError
from .base import Agent, AgentReport
from .prompts import CLASSIFIER_SYSTEM, CLASSIFIER_TEMPLATE, extract_json


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яёa-z]{4,}", (text or "").lower())}


class ClassifierAgent(Agent):
    """Подбирает пункты авиационных правил для требований."""

    name = "classifier"

    def __init__(self, cfg, store, embedder: BaseEmbedder,
                 llm: BaseLLM | None = None) -> None:
        super().__init__(cfg, store)
        self.embedder = embedder
        self.llm = llm

    def run(self, *, requirement_ids: Sequence[int] | None = None,
            owner: str = "", node_code: str = "", ruleset: str = "",
            limit: int = 200, use_llm: bool = False) -> AgentReport:
        report = self._report()
        requirements = self._select(requirement_ids, owner, node_code, limit)
        # Порог зависит от шкалы эмбеддера (см. Config.effective_classify_min):
        # у офлайн-hash она принципиально ниже, чем у семантической модели.
        min_score = self.cfg.effective_classify_min()

        if use_llm and self.llm is None:
            report.errors.append(
                "Уточнение моделью запрошено, но LLM не настроена — "
                "использован только векторный подбор.")
            use_llm = False

        for req in requirements:
            report.processed += 1
            text = req["text"] or ""
            if not text.strip():
                report.add_skip(int(req["id"]), req["external_id"],
                                "пустой текст требования")
                continue

            try:
                candidates = self._candidates(text, ruleset)
            except EmbeddingError as exc:
                report.errors.append(
                    f"{req['external_id']}: не удалось получить эмбеддинг — {exc}")
                continue

            if not candidates:
                report.add_skip(int(req["id"]), req["external_id"],
                                "в справочнике нет подходящих пунктов "
                                "(проверьте, загружены ли правила)")
                continue

            if use_llm:
                candidates = self._refine(text, candidates) or candidates

            best = candidates[0]
            if float(best["score"]) < min_score:
                report.add_skip(
                    int(req["id"]), req["external_id"],
                    f"лучший кандидат {best['ruleset']} {best['clause']} имеет "
                    f"уверенность {float(best['score']):.2f} < порога "
                    f"{min_score:.2f} — привязка не предложена")
                continue

            for cand in candidates[: self.cfg.classify_top_k]:
                score = float(cand["score"])
                if score < min_score:
                    break
                # Связь заводится сразу, но НЕподтверждённой: инженер
                # увидит её в карточке требования и подтвердит явно.
                self.store.link_requirement_clause(
                    int(req["id"]), int(cand["id"]), score=score,
                    source="agent", confirmed=False)
                self._suggest(
                    report, int(req["id"]), kind="rule_link",
                    payload={"clause_id": int(cand["id"]),
                             "ruleset": cand["ruleset"],
                             "clause": cand["clause"]},
                    rationale=(cand.get("reason")
                               or f"Векторное сходство формулировки с пунктом "
                                  f"{cand['ruleset']} {cand['clause']} "
                                  f"«{cand.get('title', '')}»: {score:.2f}"),
                    score=score)
            report.findings.append({
                "requirement_id": int(req["id"]),
                "external_id": req["external_id"],
                "candidates": [{"clause": f"{c['ruleset']} {c['clause']}",
                                "score": round(float(c["score"]), 3)}
                               for c in candidates[: self.cfg.classify_top_k]],
            })

        self._log(report)
        return report

    # ------------------------------------------------------------------
    def _select(self, ids: Sequence[int] | None, owner: str, node_code: str,
                limit: int) -> list[dict[str, Any]]:
        if ids:
            return [r for r in (self.store.get_requirement(int(i)) for i in ids)
                    if r]
        return self.store.list_requirements(owner=owner, node_code=node_code,
                                            limit=limit)

    def _candidates(self, text: str, ruleset: str) -> list[dict[str, Any]]:
        """Векторный подбор + keyword-фолбэк для пунктов без эмбеддинга."""
        vector = self.embedder.embed_one(text)
        rows = self.store.search_clauses(
            vector, limit=max(self.cfg.classify_top_k * 2, 10), ruleset=ruleset)
        found = [dict(r) for r in rows]

        # Фолбэк: пункты без эмбеддинга не участвуют в векторном поиске,
        # но могут совпасть по ключевым словам. Без этого только что
        # загруженный справочник выглядел бы пустым.
        if len(found) < self.cfg.classify_top_k:
            words = _keywords(text)
            for clause in self.store.list_clauses(ruleset):
                if any(int(clause["id"]) == int(f["id"]) for f in found):
                    continue
                pool = _keywords(f"{clause['title']} {clause['keywords']}")
                if not pool:
                    continue
                overlap = len(words & pool) / max(1, len(pool))
                if overlap > 0:
                    c = dict(clause)
                    # Keyword-совпадение заведомо слабее векторного:
                    # понижаем, чтобы оно не обгоняло семантику.
                    c["score"] = round(overlap * 0.6, 4)
                    found.append(c)

        found.sort(key=lambda c: float(c["score"]), reverse=True)
        return found[: max(self.cfg.classify_top_k * 2, 10)]

    def _refine(self, text: str, candidates: list[dict[str, Any]]
                ) -> list[dict[str, Any]] | None:
        """Уточнение моделью. Выбор ТОЛЬКО из переданных кандидатов."""
        if self.llm is None:
            return None
        listing = "\n".join(
            f"- {c['ruleset']} {c['clause']}: {c.get('title', '')} "
            f"(векторное сходство {float(c['score']):.2f})"
            for c in candidates)
        try:
            reply = self.llm.chat([
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": CLASSIFIER_TEMPLATE.format(
                    text=text, candidates=listing)},
            ])
        except LLMError:
            return None

        data = extract_json(reply.text)
        if not isinstance(data, dict):
            return None
        matches = data.get("matches")
        if not isinstance(matches, list):
            return None

        by_clause = {f"{c['ruleset']} {c['clause']}".strip(): c
                     for c in candidates}
        by_short = {str(c["clause"]).strip(): c for c in candidates}
        out: list[dict[str, Any]] = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            key = str(m.get("clause", "")).strip()
            cand = by_clause.get(key) or by_short.get(key)
            if cand is None:
                # Модель назвала пункт вне списка — игнорируем молча в
                # результате, но это ровно тот случай, ради которого
                # ограничение и введено.
                continue
            enriched = dict(cand)
            try:
                enriched["score"] = max(0.0, min(1.0, float(m.get("score", 0))))
            except (TypeError, ValueError):
                enriched["score"] = float(cand["score"])
            reason = str(m.get("reason", "")).strip()
            if reason:
                enriched["reason"] = f"{reason} (уточнено моделью)"
            out.append(enriched)
        if not out:
            return None
        out.sort(key=lambda c: float(c["score"]), reverse=True)
        return out


def index_clauses(store, embedder: BaseEmbedder, *, limit: int = 1000) -> int:
    """Досчитать эмбеддинги для пунктов правил. Возвращает число обновлённых."""
    rows = store.clauses_without_embedding(limit=limit)
    count = 0
    for row in rows:
        text = " ".join(filter(None, [row.get("clause", ""), row.get("title", ""),
                                      row.get("text", ""),
                                      row.get("keywords", "")]))
        store.set_clause_embedding(int(row["id"]), embedder.embed_one(text))
        count += 1
    return count


def index_requirements(store, embedder: BaseEmbedder, *, limit: int = 500) -> int:
    """Досчитать эмбеддинги для требований."""
    rows = store.requirements_without_embedding(limit=limit)
    count = 0
    for row in rows:
        text = " ".join(filter(None, [row.get("title", ""), row.get("text", "")]))
        store.set_requirement_embedding(int(row["id"]), embedder.embed_one(text))
        count += 1
    return count
