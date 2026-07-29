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

    # Отдельно показываем путь «загрузили PDF — система разобралась сама»:
    # это основной сценарий для инженера, и он должен быть виден в демо, а
    # не только в тестах. PDF-движок опционален, поэтому шаг пропускается
    # с объяснением, если ни pymupdf, ни pypdf не установлены.
    _demo_pdf(st, cfg, work)

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


def _demo_pdf(st, cfg, work) -> None:
    """Показать загрузку справочника из PDF одной командой."""
    from pathlib import Path

    from saps.ingest.pdf import available_engines

    engines = available_engines()
    if not engines:
        print("\n      (шаг с PDF пропущен: не установлен ни pymupdf, ни "
              "pypdf — pip install pymupdf)")
        return
    try:
        import fitz                                     # noqa: F401
    except ImportError:
        # Сгенерировать демонстрационный PDF умеет только pymupdf; с одним
        # pypdf читать можно, а создавать пример нечем.
        print(f"\n      (шаг с PDF пропущен: для генерации примера нужен "
              f"pymupdf; доступно чтение через {', '.join(engines)})")
        return

    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not Path(font).exists():
        print("\n      (шаг с PDF пропущен: нет шрифта с кириллицей для "
              "генерации примера)")
        return

    print("\n      Загрузка справочника ИЗ PDF одной командой:")
    pdf = _make_demo_pdf(Path(work) / "АП-25_выдержка.pdf", font)
    from saps.ingest.autoload import autoload
    result = autoload(st, cfg, pdf, actor="demo")
    for step in result.steps:
        print(f"        {'✓' if step.ok else '✗'} {step.name}: {step.detail}")
    for warn in result.warnings:
        print(f"        ⚠ {warn}")


def _make_demo_pdf(path, font):
    """Собрать PDF, похожий на настоящий справочник: колонтитул,
    оглавление, номера страниц, перенос слова через строку."""
    import fitz
    pages = [
        """СОДЕРЖАНИЕ

25.1309 Оборудование, системы и установки .......... 512
25.1322 Сигнализация ................................ 515""",
        """25.1309 Оборудование, системы и установки

(a) Оборудование и системы, необходимые для обеспечения безопасности, должны быть спроектированы так, чтобы выполнять предусмотренные функции в ожидаемых условиях эксплуатации.

(b) Самолётные системы должны быть спроектированы так, чтобы любое отказное состояние, препятствующее безопасному полёту, оценивалось как практически неверо-
ятное.""",
        """25.1322 Сигнализация, предупреждающая и уведомляющая информация

Предупреждающая сигнализация должна быть красного цвета и привлекать внимание экипажа немедленно.""",
        """Раздел G — ЭКСПЛУАТАЦИОННЫЕ ОГРАНИЧЕНИЯ

25.1501 Общие положения

Каждое эксплуатационное ограничение должно быть установлено и внесено в руководство по лётной эксплуатации.""",
    ]
    doc = fitz.open()
    for i, body in enumerate(pages):
        pg = doc.new_page()
        pg.insert_text((60, 40), "Авиационные правила Часть 25", fontsize=9,
                       fontfile=font, fontname="DJ")
        pg.insert_textbox(fitz.Rect(60, 70, 540, 720), body, fontsize=10,
                          fontfile=font, fontname="DJ")
        pg.insert_text((290, 780), f"- {511 + i} -", fontsize=9,
                       fontfile=font, fontname="DJ")
    doc.save(str(path))
    doc.close()
    return path


if __name__ == "__main__":
    raise SystemExit(main())
