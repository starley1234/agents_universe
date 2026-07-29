"""Конвейер импорта: файл/Teamcenter -> staging -> production.

ЕДИНАЯ ТОЧКА ВХОДА ДЛЯ ОБОИХ РЕЖИМОВ (ТЗ п.3.1). Word, Excel и
Teamcenter отличаются только тем, как получить список
ParsedRequirement; дальше идёт общий путь: запись в staging, проверка
дублей, промоушен в production. Благодаря этому «дыры» в обработке не
могут появиться в одном источнике и отсутствовать в другом.

ДВА ШАГА ВМЕСТО ОДНОГО — ОСОЗНАННО. `import_file()` только принимает
данные в staging и ничего не трогает в production. `promote()` уже
создаёт требования. Разделение нужно, потому что между этими шагами
стоит человек: он смотрит, что распозналось, и решает. Автоматический
импорт «одной кнопкой» тоже есть (`promote_all`), но это ЯВНЫЙ выбор
оператора, а не поведение по умолчанию.

ПОВТОРНЫЙ ИМПОРТ ТОГО ЖЕ ФАЙЛА. Считается sha256 содержимого. Если файл
уже импортировали, конвейер сообщает об этом и по умолчанию отказывается
дублировать — типовая ошибка оператора, которая иначе порождает сотни
требований-двойников и портит статистику покрытия.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..db.store import Store, StoreError
from .excel import parse_xlsx
from .word import ParsedRequirement, ParseError, file_hash, parse_docx, summarize


@dataclass
class ImportResult:
    document_id: int
    kind: str
    name: str
    staged: int = 0
    duplicate_of: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"document_id": self.document_id, "kind": self.kind,
                "name": self.name, "staged": self.staged,
                "duplicate_of": self.duplicate_of, "summary": self.summary,
                "warnings": self.warnings}


@dataclass
class PromoteResult:
    created: list[int] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"created": self.created, "skipped": self.skipped,
                "updated": self.updated,
                "counts": {"created": len(self.created),
                           "skipped": len(self.skipped),
                           "updated": len(self.updated)}}


def detect_kind(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return "word"
    if suffix in (".xlsx", ".xlsm"):
        return "excel"
    if suffix == ".doc":
        raise ParseError(
            "Формат .doc (Word 97-2003) не поддерживается: это бинарный "
            "формат, а не XML. Пересохраните файл как .docx.")
    if suffix == ".xls":
        raise ParseError(
            "Формат .xls не поддерживается. Пересохраните как .xlsx.")
    raise ParseError(
        f"Неизвестное расширение {suffix!r}. САПС импортирует .docx и .xlsx.")


def parse_file(path: str | Path) -> tuple[str, list[ParsedRequirement]]:
    kind = detect_kind(path)
    if kind == "word":
        return kind, parse_docx(path)
    return kind, parse_xlsx(path)


def import_file(store: Store, path: str | Path, *, actor: str = "",
                allow_duplicate: bool = False) -> ImportResult:
    """Разобрать файл и положить результат в staging."""
    p = Path(path)
    kind, parsed = parse_file(p)
    digest = file_hash(p)

    existing = store.find_document_by_hash(digest)
    if existing is not None and not allow_duplicate:
        # Не создаём документ: возвращаем указание на прошлый импорт.
        # Оператор либо работает с ним, либо осознанно повторяет импорт.
        return ImportResult(
            document_id=int(existing["id"]), kind=kind, name=p.name,
            staged=0, duplicate_of=int(existing["id"]),
            summary=summarize(parsed),
            warnings=[f"Файл с таким содержимым уже импортирован "
                      f"{existing['imported_at']:%Y-%m-%d %H:%M} как документ "
                      f"#{existing['id']}. Повторный импорт создал бы дубли "
                      f"требований. Используйте --force, если это осознанно."])

    doc_id = store.add_source_document(
        kind, p.name, uri=str(p.resolve()), content_hash=digest,
        imported_by=actor, meta={"summary": summarize(parsed)})
    store.add_staging_records(doc_id, [r.to_staging() for r in parsed])
    store.log(actor or "system", "import", object_type="document",
              object_id=doc_id,
              detail=f"{kind}: {p.name}, записей {len(parsed)}",
              data=summarize(parsed))

    warnings: list[str] = []
    no_id = [r for r in parsed if not r.external_id]
    if no_id:
        warnings.append(
            f"У {len(no_id)} записей не найден идентификатор требования "
            "([REQ-123] и подобные). Им придётся назначить номер вручную "
            "в мастере подготовки данных.")
    if not parsed:
        warnings.append(
            "В документе не распознано ни одного требования. Проверьте, что "
            "требования оформлены абзацами с модальным глаголом или таблицей "
            "с колонками «Идентификатор»/«Требование».")

    return ImportResult(document_id=doc_id, kind=kind, name=p.name,
                        staged=len(parsed), summary=summarize(parsed),
                        warnings=warnings)


def import_records(store: Store, records: Sequence[ParsedRequirement], *,
                   kind: str, name: str, uri: str = "", actor: str = "",
                   meta: dict[str, Any] | None = None) -> ImportResult:
    """Импорт уже разобранных записей (используется коннектором Teamcenter)."""
    doc_id = store.add_source_document(kind, name, uri=uri, imported_by=actor,
                                       meta=meta or {})
    store.add_staging_records(doc_id, [r.to_staging() for r in records])
    store.log(actor or "system", "import", object_type="document",
              object_id=doc_id, detail=f"{kind}: {name}, записей {len(records)}")
    return ImportResult(document_id=doc_id, kind=kind, name=name,
                        staged=len(records),
                        summary=summarize(list(records)))


def promote(store: Store, staging_ids: Sequence[int], *, actor: str = "",
            default_owner: str = "", default_node: str = "",
            on_conflict: str = "skip",
            embedder: Callable[[str], Sequence[float]] | None = None
            ) -> PromoteResult:
    """Перенести записи из staging в production (создать требования).

    on_conflict:
      skip   — если требование с таким external_id уже есть, пропустить
               (по умолчанию: сертификация не прощает молчаливой перезаписи);
      update — обновить текст существующего требования, оставив ревизию.
    """
    if on_conflict not in ("skip", "update"):
        raise StoreError("on_conflict: допустимо skip или update")

    result = PromoteResult()
    for sid in staging_ids:
        rows = store.staging_records()
        record = next((r for r in rows if int(r["id"]) == int(sid)), None)
        if record is None:
            result.skipped.append({"staging_id": sid, "reason": "запись не найдена"})
            continue

        raw = record["raw"] or {}
        external_id = (record["external_id"] or "").strip()
        text = record["raw_text"] or ""
        if not external_id:
            result.skipped.append({
                "staging_id": sid,
                "reason": "нет идентификатора требования — назначьте его "
                          "вручную перед переносом"})
            store.set_staging_status(int(sid), "skipped",
                                     "нет идентификатора")
            continue
        if not text.strip():
            result.skipped.append({"staging_id": sid, "reason": "пустой текст"})
            store.set_staging_status(int(sid), "skipped", "пустой текст")
            continue

        existing = store.get_requirement_by_external(external_id)
        if existing is not None:
            if on_conflict == "skip":
                result.skipped.append({
                    "staging_id": sid, "external_id": external_id,
                    "reason": f"требование уже есть (#{existing['id']})"})
                store.set_staging_status(int(sid), "duplicate",
                                         f"уже есть #{existing['id']}")
                continue
            store.update_requirement(
                int(existing["id"]), text=text,
                reason=f"обновлено из документа #{record['document_id']}",
                actor=actor)
            # Если объект пришёл из Teamcenter, а раньше требование было
            # локальным — фиксируем связь с объектом PDM.
            new_uid = str((raw.get("attributes", {}) or {}).get("tc_uid", ""))
            if new_uid and not (existing.get("tc_uid") or ""):
                store.mark_tc_synced(int(existing["id"]), new_uid)
            if embedder is not None:
                store.set_requirement_embedding(int(existing["id"]),
                                                embedder(text))
            store.set_staging_status(int(sid), "promoted", "обновлено")
            result.updated.append(int(existing["id"]))
            continue

        embedding = embedder(text) if embedder is not None else None
        attributes = raw.get("attributes", {}) or {}
        # UID объекта Teamcenter обязан попасть в ОТДЕЛЬНОЕ поле, а не
        # только в attributes: по нему работает обратная запись (sync.py
        # отказывается писать требование без tc_uid). Пока он лежал
        # исключительно в JSONB, синхронизация с Teamcenter была
        # невозможна в принципе — требование, пришедшее из TC, выглядело
        # как локальное.
        tc_uid = str(attributes.get("tc_uid", "") or "")
        req_id = store.create_requirement(
            external_id, text,
            title=raw.get("title", ""),
            node_code=raw.get("node") or default_node,
            owner=raw.get("owner") or default_owner,
            document_id=int(record["document_id"]),
            staging_id=int(sid),
            tc_uid=tc_uid,
            attributes=attributes,
            embedding=embedding, actor=actor)
        store.set_staging_status(int(sid), "promoted", f"создано #{req_id}")
        result.created.append(req_id)

        # MoC из таблицы атрибутов — если источник его указал, сразу
        # заводим пункт доказательства: это данные из документа, а не
        # догадка агента.
        moc = str(raw.get("attributes", {}).get("moc", "")).strip().upper()
        if moc:
            try:
                store.add_compliance_item(req_id, moc,
                                          note="импортировано из источника")
            except StoreError:
                pass  # неизвестный код MoC — не повод валить импорт

    store.log(actor or "system", "promote", object_type="staging",
              detail=f"создано {len(result.created)}, обновлено "
                     f"{len(result.updated)}, пропущено {len(result.skipped)}")
    return result


def promote_all(store: Store, document_id: int, **kwargs: Any) -> PromoteResult:
    """Перенести все новые записи документа. Явное действие оператора."""
    ids = [int(r["id"]) for r in store.staging_records(document_id, status="new")]
    return promote(store, ids, **kwargs)
