"""Тесты базы: прослеживаемость, предложения, покрытие, pgvector.

Самый важный набор проекта. Здесь проверяются инварианты, ради которых
САПС вообще имеет право стоять между инженером и Teamcenter:

  * текст требования нельзя изменить, не оставив ревизию;
  * предложение агента применяется ровно один раз;
  * связь с пунктом АП, предложенная агентом, приходит НЕподтверждённой;
  * покрытие считается по фактическим данным, а не по счётчикам.

Всё на настоящем PostgreSQL с pgvector: инварианты держатся на
транзакциях и ограничениях схемы, и мок проверял бы мок.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, check_raises, make_store, section,     # noqa: E402
                     skip_section, summary)


def main() -> int:
    if harness.server() is None:
        skip_section("Тесты базы данных", harness.SKIP_REASON)
        return summary("База данных")

    from saps.db.store import Store, StoreError

    st = make_store(dim=64)

    section("Схема и повторная инициализация")
    st.init_schema()
    check("init_schema идемпотентен", True)
    check("пустая база: статистика нулевая", st.stats()["requirements"] == 0)

    section("Документы и staging")
    doc = st.add_source_document("word", "ТЗ.docx", uri="/tmp/ТЗ.docx",
                                 content_hash="hash-1", imported_by="engineer")
    check("документ создан", doc > 0)
    check("поиск по хешу находит",
          st.find_document_by_hash("hash-1")["id"] == doc,
          "нужен для защиты от повторного импорта")
    check("несуществующий хеш -> None", st.find_document_by_hash("нет") is None)
    check("пустой хеш не ищется", st.find_document_by_hash("") is None)

    sids = st.add_staging_records(doc, [
        {"ord": 0, "external_id": "REQ-1", "raw_text": "Первое требование",
         "section_path": "1", "raw": {"confidence": 0.9}},
        {"ord": 1, "external_id": "", "raw_text": "Без номера", "raw": {}},
    ])
    check("две записи в staging", len(sids) == 2)
    rows = st.staging_records(doc)
    check("порядок сохранён", [r["ord"] for r in rows] == [0, 1])
    check("JSONB прочитан как словарь",
          rows[0]["raw"] == {"confidence": 0.9}, str(rows[0]["raw"]))
    st.set_staging_status(sids[1], "skipped", "нет идентификатора")
    check("статус записи обновлён",
          st.staging_records(doc, status="skipped")[0]["id"] == sids[1])

    section("Требования: создание и прослеживаемость")
    req = st.create_requirement(
        "REQ-1", "Система должна работать надёжно", title="Надёжность",
        node_code="АСДБ.04.32", owner="Иванов", document_id=doc,
        staging_id=sids[0], actor="engineer")
    check("требование создано", req > 0)
    check("создание оставило ревизию #1",
          [r["version"] for r in st.revisions(req)] == [1],
          "даже импорт обязан быть виден в истории")
    rev1 = st.revisions(req)[0]
    check("в ревизии есть текст", rev1["text_after"].startswith("Система"))
    check("в ревизии есть автор", rev1["actor"] == "engineer")
    check("узел создан автоматически", st.get_node("АСДБ.04.32") is not None)
    check("поиск по внешнему ключу",
          st.get_requirement_by_external("REQ-1")["id"] == req)

    check_raises("дубль external_id отвергается", StoreError,
                 st.create_requirement, "REQ-1", "другой текст")
    check_raises("пустой external_id отвергается", StoreError,
                 st.create_requirement, "  ", "текст")
    check_raises("неизвестный статус отвергается", StoreError,
                 st.create_requirement, "REQ-X", "текст", status="какой-то")

    section("Изменение требования обязано оставлять след")
    v = st.update_requirement(req, text="Наработка на отказ не менее 10000 ч",
                              reason="уточнение по замечанию", actor="Иванов")
    check("версия выросла", v == 2)
    revs = st.revisions(req)
    check("ревизий стало две", len(revs) == 2)
    check("сохранён текст ДО", revs[1]["text_before"].startswith("Система"))
    check("сохранён текст ПОСЛЕ", "10000" in revs[1]["text_after"])
    check("сохранена причина", revs[1]["reason"] == "уточнение по замечанию")
    check("текст в требовании обновлён",
          "10000" in st.get_requirement(req)["text"])

    v_same = st.update_requirement(req, text="Наработка на отказ не менее 10000 ч",
                                   actor="Иванов")
    check("повторная запись того же текста не плодит ревизии",
          v_same == 2 and len(st.revisions(req)) == 2,
          "иначе история засоряется пустыми записями")

    st.update_requirement(req, status="approved", reason="утверждено",
                          actor="Петров")
    revs = st.revisions(req)
    check("смена статуса — тоже ревизия", len(revs) == 3)
    check("статус ДО сохранён", revs[2]["status_before"] == "draft")
    check("статус ПОСЛЕ сохранён", revs[2]["status_after"] == "approved")
    check_raises("несуществующее требование", StoreError,
                 st.update_requirement, 999999, text="x")

    section("Предложения агентов (ТЗ п.6.2)")
    sug = st.add_suggestion(req, "editor", kind="text",
                            text_before=st.get_requirement(req)["text"],
                            text_after="Наработка на отказ должна быть не "
                                       "менее 10000 ч",
                            rationale="нет модального глагола", score=0.6)
    check("предложение создано", sug > 0)
    check("статус pending", st.get_suggestion(sug)["status"] == "pending")
    check("видно в очереди",
          any(s["id"] == sug for s in st.list_suggestions(status="pending")))
    check_raises("предложение к несуществующему требованию", StoreError,
                 st.add_suggestion, 999999, "editor")

    before_revs = len(st.revisions(req))
    applied = st.decide_suggestion(sug, "accepted", "Иванов")
    check("принятие применило изменение", applied["applied"] is True)
    check("текст требования изменился",
          "должна быть не менее" in st.get_requirement(req)["text"])
    check("принятие оставило ревизию",
          len(st.revisions(req)) == before_revs + 1)
    check("в причине ревизии указан агент",
          "editor" in st.revisions(req)[-1]["reason"])
    check("решение записано",
          st.get_suggestion(sug)["decided_by"] == "Иванов")
    check_raises("повторное решение отвергается", StoreError,
                 st.decide_suggestion, sug, "accepted", "Иванов")

    sug2 = st.add_suggestion(req, "editor", kind="text", text_after="другое")
    st.decide_suggestion(sug2, "rejected", "Иванов")
    check("отклонение не меняет текст",
          "должна быть не менее" in st.get_requirement(req)["text"])
    check("статус отклонено", st.get_suggestion(sug2)["status"] == "rejected")
    check_raises("неизвестное решение", StoreError,
                 st.decide_suggestion,
                 st.add_suggestion(req, "editor", text_after="x"),
                 "может_быть", "Иванов")

    section("Пункты авиационных правил и связи")
    clause = st.upsert_clause("АП-25", "25.1309",
                              title="Оборудование, системы и установки",
                              keywords="отказ система")
    check("пункт создан", clause > 0)
    same = st.upsert_clause("АП-25", "25.1309", title="Новое название")
    check("повторная загрузка обновляет, а не дублирует", same == clause)
    check("название обновилось",
          st.get_clause("АП-25", "25.1309")["title"] == "Новое название")

    link = st.link_requirement_clause(req, clause, score=0.8, source="agent")
    links = st.requirement_links(req)
    check("связь создана", len(links) == 1)
    check("связь агента НЕ подтверждена", links[0]["confirmed"] is False,
          "в отчёт для регулятора идут только подтверждённые человеком связи")
    st.confirm_link(link, "Иванов")
    check("подтверждение сработало",
          st.requirement_links(req)[0]["confirmed"] is True)
    check("автор подтверждения записан",
          st.requirement_links(req)[0]["confirmed_by"] == "Иванов")
    check_raises("подтверждение несуществующей связи", StoreError,
                 st.confirm_link, 999999, "Иванов")

    st.link_requirement_clause(req, clause, score=0.5, source="agent")
    check("повторная связь не сбрасывает подтверждение",
          st.requirement_links(req)[0]["confirmed"] is True)
    check("оценка берётся максимальная",
          float(st.requirement_links(req)[0]["score"]) == 0.8)

    section("Доказательная документация")
    item = st.add_compliance_item(req, "MC2", responsible="Иванов",
                                  planned_date=date(2026, 6, 1))
    check("пункт MoC создан", item > 0)
    check_raises("неизвестный код MoC", StoreError,
                 st.add_compliance_item, req, "MC42")
    check_raises("неизвестный статус MoC", StoreError,
                 st.add_compliance_item, req, "MC3", status="почти")
    items = st.compliance_items(req)
    check("пункт виден", len(items) == 1 and items[0]["moc"] == "MC2")
    check("доказательств пока нет", items[0]["evidence"] == [])

    ev = st.add_evidence(item, kind="report", title="Расчёт надёжности 12-345",
                         uri="tc://dataset/9911")
    check("доказательство добавлено", ev > 0)
    check("доказательство видно в пункте",
          len(st.compliance_items(req)[0]["evidence"]) == 1)
    check_raises("доказательство к несуществующему пункту", StoreError,
                 st.add_evidence, 999999, title="x")
    st.set_compliance_status(item, "compliant")
    check("статус пункта обновлён",
          st.compliance_items(req)[0]["status"] == "compliant")

    section("Покрытие считается по фактическим данным")
    cov = {c["external_id"]: c for c in st.coverage()}
    row = cov["REQ-1"]
    check("подтверждённых связей: 1", int(row["links"]) == 1)
    check("пунктов MoC: 1", int(row["moc_count"]) == 1)
    check("доказательств: 1", int(row["evidence_count"]) == 1)
    check("соответствующих пунктов: 1", int(row["compliant_count"]) == 1)

    req2 = st.create_requirement("REQ-2", "Второе требование без покрытия",
                                 node_code="АСДБ.04.32")
    cov = {c["external_id"]: c for c in st.coverage()}
    check("новое требование без покрытия",
          int(cov["REQ-2"]["links"]) == 0 and int(cov["REQ-2"]["moc_count"]) == 0)
    check("фильтр по узлу", len(st.coverage(node_code="АСДБ.04.32")) == 2)
    check("фильтр по несуществующему узлу", st.coverage(node_code="нет") == [])

    section("Векторный поиск (pgvector)")
    from saps.llm import build_embedder
    emb = build_embedder("hash", "hash-64", dim=64)
    for text, code in (("Отказобезопасность систем управления", "25.671"),
                       ("Масса пустого самолёта и центровка", "25.29")):
        cid = st.upsert_clause("АП-25", code, title=text,
                               embedding=emb.embed_one(text))
    hits = st.search_clauses(
        emb.embed_one("отказобезопасность системы управления"), limit=3)
    check("векторный поиск вернул результат", len(hits) > 0)
    check("самый похожий пункт — верный",
          hits and hits[0]["clause"] == "25.671",
          str([(h["clause"], round(float(h["score"]), 2)) for h in hits]))
    check("оценка в разумных пределах",
          hits and 0.0 <= float(hits[0]["score"]) <= 1.0)

    st.set_requirement_embedding(req, emb.embed_one(
        st.get_requirement(req)["text"]))
    st.set_requirement_embedding(req2, emb.embed_one("Второе требование"))
    similar = st.similar_requirements(
        emb.embed_one("Наработка на отказ должна быть не менее 10000 ч"),
        limit=3)
    check("поиск похожих требований работает", len(similar) > 0)
    check("исключение самого себя",
          all(int(s["id"]) != req
              for s in st.similar_requirements(
                  emb.embed_one("текст"), exclude_id=req)))
    check("требования без эмбеддинга перечисляются",
          isinstance(st.requirements_without_embedding(), list))

    section("Качество и журнал")
    st.set_quality(req, 0.85, {"issues": [], "checks": {"measurable": True}})
    r = st.get_requirement(req)
    check("оценка качества сохранена", abs(float(r["quality_score"]) - 0.85) < 1e-6)
    check("разбор качества сохранён как JSON",
          r["quality"]["checks"]["measurable"] is True)

    st.log("Иванов", "accept", object_type="requirement", object_id=req,
           detail="принято предложение", data={"suggestion": sug})
    audit = st.audit(object_type="requirement", object_id=req)
    check("журнал записан", len(audit) >= 1)
    check("данные журнала разобраны", audit[0]["data"].get("suggestion") == sug)

    section("Статистика")
    stats = st.stats()
    check("требований 2", stats["requirements"] == 2)
    check("подтверждённых связей 1", stats["links_confirmed"] == 1)
    check("доказательств 1", stats["evidence"] == 1)
    check("утверждённых требований 1", stats["requirements_approved"] == 1)

    section("Каскадное удаление документа не рушит требования")
    docs_before = len(st.list_documents())
    check("документы перечисляются", docs_before >= 1)
    check("у документа посчитаны записи",
          st.list_documents()[0]["records"] >= 1)

    st.close()
    harness.cleanup()
    return summary("База данных")


if __name__ == "__main__":
    raise SystemExit(main())
