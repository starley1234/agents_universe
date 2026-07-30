"""Фоновое обслуживание ("Deep Thinking" фаза), ТЗ п.6.

Три задачи, каждая — детерминированная и идемпотентная (можно звать
сколько угодно раз подряд без порчи данных):

  distill()  — диалоги длиннее distill_after_messages сообщений
               переписываются в один "квант памяти" question/answer,
               чтобы контекстное окно не разбухало от старых реплик.
               Само сообщение диалога НЕ удаляется (это лог), но
               появляется компактная альтернатива для semantic search.
  dedup()    — среди memory_quantum находит пары с косинусным сходством
               embedding'ов выше maintenance_dedup_similarity и удаляет
               более старый дубль, оставляя новый (предполагается, что
               новее — точнее/актуальнее).
  synthesize_graph() — очень простой пример "связывания разрозненных
               фактов": сливает сущности графа с одинаковым kind и
               высоким сходством description-эмбеддингов через
               store.merge_entities (защита от опечаток/дублей вида
               "Иванов" / "иванов").

run_forever() — вызывает все три с интервалом maintenance_interval_seconds,
кооперативно останавливается по threading.Event (тот же паттерн, что
AutoRunner.stop_event в agent_system).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..llm.embeddings import BaseEmbedder, cosine
from ..memory.store import Store


@dataclass
class MaintenanceReport:
    distilled: int = 0
    deduped: int = 0
    merged_entities: int = 0
    extracted_entities: int = 0
    extracted_relations: int = 0
    errors: list[str] = field(default_factory=list)


class MaintenanceService:
    def __init__(self, cfg: Config, store: Store, embedder: BaseEmbedder,
                on_event: Callable[[str, dict[str, Any]], None] | None = None,
                summarizer: Callable[[str], str] | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.on_event = on_event or (lambda kind, data: None)
        self.summarizer = summarizer

    def _emit(self, kind: str, **data: Any) -> None:
        try:
            self.on_event(kind, data)
        except Exception:
            pass

    # -------------------------------------------------------- дистилляция
    def distill(self) -> int:
        """Длинные диалоги -> один компактный квант памяти на диалог.

        Берём диалоги, длина которых достигла порога, и для каждого,
        которого ЕЩЁ нет соответствующего кванта (простая проверка —
        по количеству сообщений с последней дистилляции, отслеживаемой
        через число уже существующих квантов этого conversation_id),
        сворачиваем историю в вопрос-ответ пару.
        """
        from .distill import distill_conversation
        count = 0
        for conv in self.store.list_conversations(limit=1000):
            n = self.store.message_count(conv["id"])
            if n < self.cfg.maintenance_distill_after_messages:
                continue
            existing = len([q for q in self.store.all_quanta()
                          if q["conversation_id"] == conv["id"]])
            # уже дистиллировали этот диалог примерно на этом объёме —
            # не создаём дубли при повторном вызове maintenance
            if existing > 0:
                continue
            messages = self.store.messages(conv["id"])
            q, a = distill_conversation(messages, self.summarizer)
            if not q or not a:
                continue
            try:
                emb = self.embedder.embed_one(f"{q} {a}")
            except Exception:
                emb = None
            self.store.add_memory_quantum(
                conv["id"], q, a, provider_model="maintenance::distill",
                confidence_score=0.8, embedding=emb)
            count += 1
        self._emit("distilled", count=count)
        return count

    # ------------------------------------------------------------- dedup
    def dedup(self) -> int:
        """Удаляет дубли квантов памяти по высокому косинусному сходству."""
        quanta = [q for q in self.store.all_quanta() if q["embedding"]]
        quanta.sort(key=lambda q: q["id"])
        removed = 0
        dropped: set[int] = set()
        for i in range(len(quanta)):
            if quanta[i]["id"] in dropped:
                continue
            for j in range(i + 1, len(quanta)):
                if quanta[j]["id"] in dropped:
                    continue
                sim = cosine(quanta[i]["embedding"], quanta[j]["embedding"])
                if sim >= self.cfg.maintenance_dedup_similarity:
                    # оставляем более новый (больший id), убираем старый
                    older = quanta[i] if quanta[i]["id"] < quanta[j]["id"] else quanta[j]
                    if older["id"] not in dropped:
                        self.store.delete_quantum(older["id"])
                        dropped.add(older["id"])
                        removed += 1
        self._emit("deduped", count=removed)
        return removed

    # ------------------------------------------------------- граф-синтез
    def synthesize_graph(self, similarity_threshold: float = 0.95) -> int:
        """Сливает похожие сущности графа одного kind (устранение дублей)."""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT kind, name, embedding FROM onto_entity "
            "WHERE embedding IS NOT NULL ORDER BY kind, id")
        from ..memory.store import _parse_vec
        rows = [(k, n, _parse_vec(e)) for k, n, e in cur.fetchall()]
        merged = 0
        seen: set[tuple[str, str]] = set()
        for i in range(len(rows)):
            ki, ni, ei = rows[i]
            if (ki, ni) in seen:
                continue
            for j in range(i + 1, len(rows)):
                kj, nj, ej = rows[j]
                if kj != ki or (kj, nj) in seen:
                    continue
                if cosine(ei, ej) >= similarity_threshold:
                    if self.store.merge_entities(ki, ni, nj):
                        seen.add((kj, nj))
                        merged += 1
        self._emit("graph_synthesized", merged=merged)
        return merged

    # ----------------------------------------------- экстракция сущностей
    def extract_graph(self) -> tuple[int, int]:
        """Автоизвлечение сущностей и связей из диалогов в онтологический граф.

        Проходит по последним диалогам и пополняет long-term память (onto_entity,
        onto_relation) новыми сущностями и связями, генерируя эмбеддинги
        для описаний.
        """
        if not getattr(self.cfg, "maintenance_extract_entities", True):
            return 0, 0
        from .extract import extract_graph_from_messages
        entities_added = 0
        relations_added = 0
        for conv in self.store.list_conversations(limit=20):
            messages = self.store.messages(conv["id"])
            if not messages:
                continue
            items = extract_graph_from_messages(messages)
            for item in items:
                if item["type"] == "entity":
                    kind, name, desc = item["kind"], item["name"], item["description"]
                    ent = self.store.get_entity(kind, name)
                    if not ent:
                        try:
                            emb = self.embedder.embed_one(f"{kind}:{name} {desc}")
                        except Exception:
                            emb = None
                        self.store.upsert_entity(
                            kind, name, description=desc, embedding=emb)
                        entities_added += 1
                elif item["type"] == "relation":
                    subj, pred, obj = item["subj"], item["pred"], item["obj"]
                    for k, n in (subj, obj):
                        if not self.store.get_entity(k, n):
                            try:
                                emb = self.embedder.embed_one(f"{k}:{n}")
                            except Exception:
                                emb = None
                            self.store.upsert_entity(k, n, embedding=emb)
                    if self.store.link(subj, pred, obj):
                        relations_added += 1
        if entities_added or relations_added:
            self._emit("graph_extracted", entities=entities_added,
                       relations=relations_added)
        return entities_added, relations_added

    def run_once(self) -> MaintenanceReport:
        report = MaintenanceReport()
        try:
            report.distilled = self.distill()
        except Exception as exc:
            report.errors.append(f"distill: {exc}")
        try:
            report.deduped = self.dedup()
        except Exception as exc:
            report.errors.append(f"dedup: {exc}")
        try:
            report.merged_entities = self.synthesize_graph()
        except Exception as exc:
            report.errors.append(f"synthesize_graph: {exc}")
        try:
            ent, rel = self.extract_graph()
            report.extracted_entities = ent
            report.extracted_relations = rel
        except Exception as exc:
            report.errors.append(f"extract_graph: {exc}")
        self._emit("maintenance_cycle", distilled=report.distilled,
                   deduped=report.deduped, merged=report.merged_entities,
                   extracted_entities=report.extracted_entities,
                   extracted_relations=report.extracted_relations,
                   errors=report.errors)
        return report

    def run_forever(self, stop_event: threading.Event) -> None:
        """Цикл фонового обслуживания. Кооперативная остановка через Event —
        проверяется между циклами, а не прерывает середину работы."""
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self.cfg.maintenance_interval_seconds)
