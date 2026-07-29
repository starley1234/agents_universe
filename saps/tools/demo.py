"""Сквозная демонстрация САПС на временной базе — без настройки.

Зачем нужна: систему показывают руководству и коллегам до того, как
развёрнут PostgreSQL и настроен Teamcenter. Демо поднимает embedded
PostgreSQL (pgserver), проходит весь путь и печатает, что произошло на
каждом шаге:

    Word-файл -> staging -> требования -> справочник АП -> три агента
    -> предложения -> решение инженера -> протокол соответствия

Это же используется как дымовой тест: если демо прошло, значит рабочий
конвейер цел целиком, а не по частям.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    try:
        import pgserver
    except ImportError:
        print("Для демо нужен pgserver (embedded PostgreSQL):\n"
              "  pip install pgserver\n"
              "Либо укажите свою базу в SAPS_DB_DSN и работайте командами "
              "saps init / saps import.", file=sys.stderr)
        return 2

    from saps.agents import ClassifierAgent, EditorAgent, GapAgent
    from saps.config import Config
    from saps.db.store import Store
    from saps.export.reports import compliance_docx
    from saps.export.writers import write_docx
    from saps.ingest.pipeline import import_file, promote_all
    from saps.llm import build_embedder
    from saps.rules.loader import load_builtin

    work = Path(tempfile.mkdtemp(prefix="saps_demo_"))
    print(f"Рабочий каталог демо: {work}\n")
    print("Поднимаю временный PostgreSQL…")
    srv = pgserver.get_server(str(work / "pg"))

    cfg = Config(db_dsn=srv.get_uri(), embedding_dim=512,
                 workdir=str(work / "out"))
    st = Store(cfg.require_dsn(), schema="saps", dim=cfg.embedding_dim)
    st.init_schema()
    emb = build_embedder(cfg.embedding_provider, cfg.embedding_model,
                         dim=cfg.embedding_dim)

    print("\n[1/6] Готовлю исходный документ (имитация выгрузки из Teamcenter)")
    doc = write_docx(work / "ТЗ_на_изделие.docx", [
        {"type": "heading", "text": "3 Требования к системе управления",
         "level": 1},
        {"type": "paragraph",
         "text": "[REQ-001] Система управления должна обеспечивать "
                 "отказобезопасность при единичном отказе любого элемента."},
        {"type": "paragraph",
         "text": "[REQ-002] Наработка на отказ блока управления должна быть "
                 "не менее 10000 ч."},
        {"type": "paragraph",
         "text": "[REQ-003] Система должна иметь высокую надёжность и "
                 "достаточное быстродействие."},
        {"type": "heading", "text": "4 Требования к конструкции", "level": 1},
        {"type": "table",
         "header": ["Идентификатор", "Требование", "Ответственный", "Узел"],
         "rows": [
             ["REQ-010", "Масса блока не должна превышать 12 кг", "Иванов",
              "АСДБ.04.32"],
             ["REQ-011", "Конструкция должна выдерживать эксплуатационную "
                         "нагрузку 3.5 g", "Петров", "АСДБ.04.32"],
         ]},
    ], title="Техническое задание на изделие")
    print(f"      {doc.name}")

    print("\n[2/6] Импорт: разбор документа -> staging")
    result = import_file(st, doc, actor="demo")
    s = result.summary
    print(f"      распознано {result.staged} требований "
          f"(с номером {s['with_id']}, из таблиц "
          f"{s['origins'].get('table_row', 0)})")

    print("\n[3/6] Перенос в производственный слой")
    pr = promote_all(st, result.document_id, actor="demo",
                     embedder=emb.embed_one)
    print(f"      создано требований: {len(pr.created)}")

    print("\n[4/6] Загрузка справочника авиационных правил")
    for item in load_builtin(st, embedder=emb):
        print(f"      {item['ruleset']}: {item['loaded']} пунктов")

    print("\n[5/6] Работа агентов")
    ed = EditorAgent(cfg, st).run()
    print(f"      Редактор:      {ed.summary()}")
    for f in ed.findings:
        codes = ", ".join(i["code"] for i in f["issues"])
        print(f"        • {f['external_id']} оценка {f['score']}: {codes}")

    cl = ClassifierAgent(cfg, st, emb).run()
    print(f"      Классификатор: {cl.summary()}")
    for f in cl.findings[:3]:
        best = f["candidates"][0] if f["candidates"] else None
        if best:
            print(f"        • {f['external_id']} -> {best['clause']} "
                  f"({best['score']})")

    gap_agent = GapAgent(cfg, st)
    gp = gap_agent.run()
    print(f"      Gap-аналитик:  {gp.summary()}")
    kinds: dict[str, int] = {}
    for f in gp.findings:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    for kind, count in sorted(kinds.items()):
        print(f"        • {kind}: {count}")

    print("\n      Решение инженера по предложениям агентов:")
    pending = st.list_suggestions(status="pending")
    print(f"        в очереди: {len(pending)}")
    accepted = 0
    for sug in pending:
        if sug["kind"] in ("rule_link", "moc"):
            st.decide_suggestion(int(sug["id"]), "accepted", "Иванов")
            accepted += 1
    print(f"        принято инженером: {accepted} "
          f"(агент сам ничего не менял)")

    print("\n[6/6] Индикатор здоровья и протокол соответствия")
    health = gap_agent.health()
    print(f"      готовность: {health['health'] * 100:.0f}% — {health['status']}")
    for key, value in health["factors"].items():
        print(f"        {key:<11} {value * 100:>5.0f}%")
    print("      незакрытые пробелы: " + ", ".join(
        f"{k}={v}" for k, v in health["gaps"].items() if v))

    out = compliance_docx(st, cfg, Path(cfg.workdir) / "Протокол.docx")
    print(f"\n      протокол: {out}")

    print("\nГотово. Демо прошло весь путь: файл -> база -> агенты -> "
          "решение человека -> документ.")
    print("Ничего не было изменено без явного решения инженера.")
    st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
