"""Тесты PDF, автозагрузки и внешней модели эмбеддингов.

PDF-часть работает БЕЗ базы: это разбор файла. Автозагрузка и индексация
— на настоящем PostgreSQL. Внешний сервер эмбеддингов имитируется
локальным HTTP-сервером: проверяется реальный протокол (батчи, ключ,
размерность, диагностика ошибок), а не мок клиента.

Тестовые PDF генерируются здесь же PyMuPDF со шрифтом DejaVu (в нём есть
кириллица — встроенные шрифты PDF её не содержат, и без этого тестовые
документы получались бы из «?????»).
"""
from __future__ import annotations

import json
import sys
import threading
import hashlib
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, check_raises, make_config, make_store,  # noqa: E402
                     section, skip_section, summary)

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

HAVE_PDF = True
PDF_SKIP = ""
try:
    import fitz                                                    # noqa: F401
except ImportError:
    HAVE_PDF = False
    PDF_SKIP = "pymupdf не установлен — pip install pymupdf"
if HAVE_PDF and not Path(FONT).exists():
    HAVE_PDF = False
    PDF_SKIP = f"нет шрифта с кириллицей ({FONT}) для генерации тестовых PDF"


def make_pdf(path: Path, pages: list[str], *, header: str = "",
             footer_from: int = 0) -> Path:
    """Собрать тестовый PDF с колонтитулом и номерами страниц."""
    import fitz
    doc = fitz.open()
    for i, body in enumerate(pages):
        pg = doc.new_page()
        if header:
            pg.insert_text((60, 40), header, fontsize=9, fontfile=FONT,
                           fontname="DJ")
        pg.insert_textbox(fitz.Rect(60, 70, 540, 720), body, fontsize=10,
                          fontfile=FONT, fontname="DJ")
        if footer_from:
            pg.insert_text((290, 780), f"- {footer_from + i} -", fontsize=9,
                           fontfile=FONT, fontname="DJ")
    doc.save(str(path))
    doc.close()
    return path


RULEBOOK_PAGES = [
    """СОДЕРЖАНИЕ

25.1309 Оборудование, системы и установки .......... 512
25.1322 Сигнализация ................................ 515
25.1329 Система автоматического управления .......... 517""",
    """25.1309 Оборудование, системы и установки

(a) Оборудование и системы, необходимые для обеспечения безопасности, должны быть спроектированы так, чтобы выполнять предусмотренные функции в ожидаемых условиях эксплуатации.

(b) Самолётные системы должны быть спроектированы так, чтобы любое отказное состояние, препятствующее безопасному полёту, оценивалось как практически неверо-
ятное.""",
    """25.1322 Сигнализация, предупреждающая и уведомляющая информация

Предупреждающая сигнализация должна быть красного цвета и привлекать внимание экипажа немедленно при возникновении опасной ситуации.""",
    """Раздел G — ЭКСПЛУАТАЦИОННЫЕ ОГРАНИЧЕНИЯ

25.1501 Общие положения

Каждое эксплуатационное ограничение должно быть установлено и внесено в руководство по лётной эксплуатации самолёта.""",
]

TZ_PAGES = [
    """3 Требования к системе управления

[REQ-001] Система управления должна обеспечивать отказобезопасность при единичном отказе любого элемента.

[REQ-002] Наработка на отказ блока управления должна быть не менее 10000 ч.

[REQ-003] Масса блока управления не должна превышать 12 кг.""",
]


# --- фейковый сервер эмбеддингов ------------------------------------------
class FakeEmbeddings(BaseHTTPRequestHandler):
    dim = 768
    requests: list[dict] = []
    fail_code = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode())
        type(self).requests.append({
            "inputs": len(body.get("input", [])),
            "model": body.get("model", ""),
            "auth": self.headers.get("Authorization", ""),
        })
        if type(self).fail_code:
            out = json.dumps({"error": "нет такой модели"}).encode()
            self.send_response(type(self).fail_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        data = []
        for i, text in enumerate(body["input"]):
            seed = int.from_bytes(
                hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")
            vec = [((seed >> (j % 60)) & 0xFF) / 255.0
                   for j in range(type(self).dim)]
            data.append({"index": i, "embedding": vec})
        out = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="saps_pdf_"))

    # ============ ВНЕШНЯЯ МОДЕЛЬ ЭМБЕДДИНГОВ =========================
    section("Внешняя модель эмбеддингов: протокол")
    from saps.llm.embeddings import (EXTERNAL_PROVIDERS, EmbeddingError,
                                     build_embedder, is_external,
                                     probe_embedding_dim)

    check("hash не считается внешним", is_external("hash") is False)
    for name in ("lmstudio", "ollama", "vllm", "openai", "local"):
        check(f"{name} — внешний провайдер", is_external(name) is True)
    check("адрес LM Studio по умолчанию",
          build_embedder("lmstudio", "m", dim=8).base_url
          == "http://localhost:1234/v1")
    check("адрес Ollama по умолчанию (порт другой!)",
          build_embedder("ollama", "m", dim=8).base_url
          == "http://localhost:11434/v1",
          "у Ollama 11434, у LM Studio 1234 — общий адрес ломал бы половину")
    check("адрес OpenAI по умолчанию",
          build_embedder("openai", "m", dim=8).base_url
          == "https://api.openai.com/v1")
    check("явный адрес перебивает умолчание",
          build_embedder("lmstudio", "m", dim=8,
                         base_url="http://10.0.0.5:8000/v1").base_url
          == "http://10.0.0.5:8000/v1")
    check_raises("неизвестный провайдер", EmbeddingError, build_embedder,
                 "выдуманный", "m", dim=8)

    FakeEmbeddings.requests = []
    FakeEmbeddings.fail_code = 0
    FakeEmbeddings.dim = 768
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmbeddings)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/v1"

    dim = probe_embedding_dim("lmstudio", "nomic-embed-text", base_url=url)
    check("размерность определяется автоматически", dim == 768, str(dim))
    check("пробный запрос один", len(FakeEmbeddings.requests) == 1)

    FakeEmbeddings.requests = []
    emb = build_embedder("lmstudio", "nomic-embed-text", dim=768,
                         base_url=url, api_key="sk-secret", batch=32)
    vectors = emb.embed([f"пункт {i}" for i in range(100)])
    check("получены все векторы", len(vectors) == 100)
    check("отправлено пачками, а не по одному",
          len(FakeEmbeddings.requests) == 4,
          f"запросов: {len(FakeEmbeddings.requests)} (ожидали 4 по 32)")
    check("размеры пачек верные",
          [r["inputs"] for r in FakeEmbeddings.requests] == [32, 32, 32, 4])
    check("ключ передан в заголовке",
          FakeEmbeddings.requests[0]["auth"] == "Bearer sk-secret")
    check("имя модели передано",
          FakeEmbeddings.requests[0]["model"] == "nomic-embed-text")
    check("одинаковый текст -> одинаковый вектор",
          emb.embed_one("тест") == emb.embed_one("тест"))

    section("Внешняя модель: диагностика ошибок")
    wrong = build_embedder("lmstudio", "m", dim=256, base_url=url)
    try:
        wrong.embed(["текст"])
        check("несовпадение размерности поймано", False)
    except EmbeddingError as exc:
        msg = str(exc)
        check("несовпадение размерности поймано", True)
        check("названы обе размерности", "768" in msg and "256" in msg)
        check("подсказано решение", "SAPS_EMBEDDING_DIM=768" in msg, msg[:200])

    FakeEmbeddings.fail_code = 400
    try:
        build_embedder("lmstudio", "нет-такой", dim=768,
                       base_url=url).embed(["x"])
        check("HTTP 400 пойман", False)
    except EmbeddingError as exc:
        check("HTTP 400 пойман", True)
        check("подсказка про имя модели", "имя модели" in str(exc), str(exc)[:150])
    FakeEmbeddings.fail_code = 401
    try:
        build_embedder("lmstudio", "m", dim=768, base_url=url).embed(["x"])
    except EmbeddingError as exc:
        check("подсказка про ключ при 401", "ключ" in str(exc), str(exc)[:150])
    FakeEmbeddings.fail_code = 0

    dead = build_embedder("lmstudio", "m", dim=768,
                          base_url="http://127.0.0.1:1/v1")
    try:
        dead.embed(["x"])
        check("недоступный сервер пойман", False)
    except EmbeddingError as exc:
        check("недоступный сервер пойман", True)
        check("в ошибке есть адрес и подсказка",
              "127.0.0.1:1" in str(exc) and "SAPS_EMBEDDING_BASE_URL" in str(exc))

    section("Конфигурация внешней модели")
    from saps.config import Config, ConfigError
    cfg_ext = Config(embedding_provider="lmstudio",
                     embedding_model="nomic-embed-text", embedding_dim=768)
    check("распознана как внешняя", cfg_ext.uses_external_embeddings())
    check("порог классификатора — «семантический»",
          cfg_ext.effective_classify_min() == cfg_ext.classify_min_score,
          "у внешней модели шкала косинуса другая, чем у hash")
    check("hash использует свой порог",
          Config().effective_classify_min() == Config().classify_min_score_hash)
    check_raises("внешняя модель без имени отвергается", ConfigError,
                 Config(embedding_provider="lmstudio",
                        embedding_model="").validate)
    check_raises("batch < 1 отвергается", ConfigError,
                 Config(embedding_batch=0).validate)

    # ============ PDF ================================================
    if not HAVE_PDF:
        httpd.shutdown()
        skip_section("Чтение PDF", PDF_SKIP)
        return summary("PDF и автозагрузка")

    from saps.ingest.pdf import (PdfError, available_engines, clean_text,
                                 find_repeating_lines, paragraphs, read_pdf)

    section("Чтение PDF")
    check("движок доступен", bool(available_engines()), str(available_engines()))
    book = make_pdf(tmp / "ap25.pdf", RULEBOOK_PAGES,
                    header="Авиационные правила Часть 25", footer_from=511)
    doc = read_pdf(book)
    check("страницы прочитаны", len(doc.pages) == 4)
    check("текст извлечён", doc.total_chars > 500)
    check("не принят за скан", doc.looks_scanned() is False)
    check("сводка заполнена", doc.summary()["pages"] == 4)
    check_raises("несуществующий файл", PdfError, read_pdf, tmp / "нет.pdf")
    check_raises("не-PDF по расширению", PdfError, read_pdf, tmp / "a.docx")
    check_raises("неизвестный движок", PdfError, read_pdf, book,
                 engine="выдуманный")
    check("явный движок pymupdf работает",
          read_pdf(book, engine="pymupdf").engine == "pymupdf")

    section("Очистка текста PDF")
    repeats = find_repeating_lines(doc.pages)
    check("колонтитул распознан как повторяющийся",
          "Авиационные правила Часть 25" in repeats, str(repeats))
    text = clean_text(doc)
    check("колонтитул убран", "Авиационные правила Часть 25" not in text)
    check("номера страниц убраны", "- 512 -" not in text)
    check("перенос слова склеен", "невероятное" in text,
          "«неверо-\\nятное» обязано склеиться, иначе поиск не найдёт слово")
    check("дефис не съеден в обычных словах", "Раздел G" in text)
    check("содержимое пунктов на месте", "25.1309" in text and "25.1501" in text)

    paras = paragraphs(text)
    check("абзацы выделены", len(paras) >= 6, str(len(paras)))
    check("пункт (a) отделён от (b)",
          any(p.startswith("(a)") for p in paras)
          and any(p.startswith("(b)") for p in paras))

    section("PDF без текстового слоя (скан)")
    import fitz
    scan = fitz.open()
    for _ in range(3):
        scan.new_page()
    scan.save(str(tmp / "scan.pdf"))
    scan.close()
    scanned = read_pdf(tmp / "scan.pdf")
    check("скан распознан", scanned.looks_scanned() is True)

    section("Извлечение пунктов справочника")
    from saps.rules.pdf_rules import detect_ruleset, extract_clauses, extract_from_pdf

    result = extract_from_pdf(book)
    codes = [c.clause for c in result.clauses]
    check("набор правил определён", result.ruleset == "АП-25", result.ruleset)
    check("найдены все пункты",
          codes == ["25.1309", "25.1322", "25.1501"], str(codes))
    check("оглавление отброшено", result.skipped_toc == 3,
          f"пропущено {result.skipped_toc} (ожидали 3 строки оглавления)")
    check("предупреждение об оглавлении есть",
          any("оглавления" in w for w in result.warnings))

    first = result.clauses[0]
    check("заголовок пункта разобран",
          first.title == "Оборудование, системы и установки", first.title)
    check("тело пункта собрано", "(a)" in first.text and "(b)" in first.text)
    check("колонтитул НЕ протёк в текст пункта",
          "Авиационные правила" not in first.text, first.text[:120])
    check("номер страницы не протёк", "- 512 -" not in first.text)
    last = result.clauses[-1]
    check("раздел сохранён как контекст",
          "ЭКСПЛУАТАЦИОННЫЕ ОГРАНИЧЕНИЯ" in last.section, last.section)
    check("ключевые слова извлечены", len(first.keywords().split()) >= 5)
    check("стоп-слова отброшены", "должен" not in first.keywords().split())

    payload = result.to_ruleset_dict()
    check("формат для загрузчика верный",
          payload["ruleset"] == "АП-25" and len(payload["clauses"]) == 3)
    check("в пункте есть keywords", bool(payload["clauses"][0]["keywords"]))

    section("Определение набора правил")
    check("из имени файла",
          detect_ruleset(read_pdf(make_pdf(tmp / "АП-21.pdf", ["25.100 Текст"])),
                         tmp / "АП-21.pdf") == "АП-21")
    # Текста должно быть достаточно, иначе документ (справедливо) будет
    # признан сканом, и мы проверим не то, что хотели.
    filler = ("Настоящий документ описывает порядок работы и не содержит "
              "нумерованных пунктов нормативного вида. " * 6)
    empty_named = make_pdf(tmp / "документ.pdf", [filler, filler])
    check("не определился -> пусто",
          detect_ruleset(read_pdf(empty_named), empty_named) == "")
    check_raises("без набора правил — понятный отказ", ValueError,
                 extract_from_pdf, empty_named)
    check("явное указание набора работает",
          extract_from_pdf(book, ruleset="CS-25").ruleset == "CS-25")

    clauses, toc, short = extract_clauses(
        "25.100 Заголовок ..................... 42\n"
        "25.200 Настоящий пункт содержит достаточно длинный текст "
        "для того, чтобы пройти порог.")
    check("строка оглавления не стала пунктом", toc == 1)
    check("нормальный пункт извлечён", len(clauses) == 1)

    section("Определение назначения документа")
    from saps.ingest.autoload import detect_file_type, detect_kind
    from saps.ingest.word import ParseError

    kind, reason = detect_kind(read_pdf(book).text, book)
    check("справочник распознан", kind == "rulebook", f"{kind}: {reason}")
    check("причина названа", bool(reason))
    tz = make_pdf(tmp / "tz.pdf", TZ_PAGES)
    kind, reason = detect_kind(read_pdf(tz).text, tz)
    check("ТЗ распознано как требования", kind == "requirements", reason)
    check("причина упоминает идентификаторы", "REQ" in reason or "идентиф" in reason)
    kind, reason = detect_kind("Просто текст ни о чём", tmp / "x.pdf")
    check("непонятный документ -> пусто + подсказка",
          kind == "" and "--as" in reason)
    check("тип файла по расширению",
          (detect_file_type("a.pdf"), detect_file_type("a.docx"),
           detect_file_type("a.xlsx")) == ("pdf", "word", "excel"))
    check_raises(".doc отвергается с подсказкой", ParseError,
                 detect_file_type, "a.doc")

    # ============ АВТОЗАГРУЗКА (нужна база) ==========================
    if harness.server() is None:
        httpd.shutdown()
        skip_section("Автозагрузка в базу", harness.SKIP_REASON)
        return summary("PDF и автозагрузка")

    from saps.ingest.autoload import autoload

    section("Автозагрузка справочника из PDF")
    st = make_store(dim=64)
    cfg = make_config(embedding_dim=64)
    steps: list[str] = []
    res = autoload(st, cfg, book, actor="engineer",
                   progress=lambda m: steps.append(m))
    check("загрузка успешна", res.ok, res.report())
    check("назначение определено", res.kind == "rulebook")
    check("набор правил", res.ruleset == "АП-25")
    check("пункты в базе", res.clauses_loaded == 3)
    check("прогресс сообщался", len(steps) >= 3, str(steps))
    check("в отчёте есть шаги", len(res.steps) >= 4)
    check("пункты реально записаны",
          len(st.list_clauses("АП-25")) == 3)
    cov = st.embedding_coverage()
    check("эмбеддинги посчитаны сразу",
          cov["clauses_indexed"] == 3 and cov["clauses_total"] == 3, str(cov))
    check("действие в журнале",
          any(a["action"] == "rules_load" for a in st.audit()))

    section("Повторная загрузка справочника обновляет, а не дублирует")
    res2 = autoload(st, cfg, book, actor="engineer")
    check("пункты не задвоились", len(st.list_clauses("АП-25")) == 3)
    check("отчёт снова успешен", res2.ok)

    section("Автозагрузка ТЗ из PDF: требования + агенты")
    res3 = autoload(st, cfg, tz, actor="engineer", owner="Иванов",
                    node="АСДБ.04")
    check("назначение — требования", res3.kind == "requirements", res3.report())
    check("требования созданы", res3.requirements_created == 3,
          str(res3.requirements_created))
    req = st.get_requirement_by_external("REQ-001")
    check("требование в базе", req is not None)
    check("ответственный проставлен", req and req["owner"] == "Иванов")
    check("идентификатор убран из текста",
          req and not req["text"].startswith("[REQ-001]"))
    names = [s.name for s in res3.steps]
    check("агенты отработали",
          any("Редактор" in n for n in names)
          and any("Классификатор" in n for n in names)
          and any("Gap" in n for n in names), str(names))
    check("предложения созданы, но НЕ применены", res3.suggestions > 0)
    check("предупреждение про ручное утверждение",
          any("НЕ применены" in w for w in res3.warnings),
          "агент не имеет права менять данные сам")
    check("требования проиндексированы",
          st.embedding_coverage()["requirements_indexed"] == 3)

    section("Автозагрузка: Word и Excel тем же путём")
    from harness import sample_docx, sample_xlsx
    res4 = autoload(st, cfg, sample_docx(tmp / "tz.docx"), actor="engineer")
    check("Word загружен", res4.kind == "requirements" and res4.ok,
          res4.report())
    # В фикстуре 5 требований, но REQ-001..003 уже пришли из PDF выше:
    # существующие пропускаются (on_conflict=skip), создаются только новые.
    check("новые требования созданы, существующие не задвоились",
          res4.requirements_created == 2,
          f"создано {res4.requirements_created}, ожидали 2 (REQ-010, REQ-011)")
    check("REQ-001 не задвоился",
          len([r for r in st.list_requirements(limit=500)
               if r["external_id"] == "REQ-001"]) == 1)
    check("табличные требования доехали",
          st.get_requirement_by_external("REQ-010") is not None)
    res5 = autoload(st, cfg, sample_xlsx(tmp / "reqs.xlsx"), actor="engineer",
                    run_agents=False)
    check("Excel загружен", res5.requirements_created == 2, res5.report())
    check("агенты пропущены по флагу",
          not any("Редактор" in s.name for s in res5.steps))

    section("Автозагрузка: защита и понятные отказы")
    res6 = autoload(st, cfg, sample_docx(tmp / "tz.docx"), actor="engineer")
    check("повторный файл отклонён", not res6.ok)
    check("причина названа",
          any("уже загружался" in s.detail for s in res6.steps))
    check_raises("несуществующий файл", ParseError, autoload, st, cfg,
                 tmp / "нет.pdf")
    check_raises("скан без текстового слоя", PdfError, autoload, st, cfg,
                 tmp / "scan.pdf")
    unknown = make_pdf(tmp / "неясно.pdf", [filler, filler])
    check_raises("непонятное назначение", ParseError, autoload, st, cfg,
                 unknown)
    forced = autoload(st, cfg, unknown, kind="requirements", actor="e")
    check("явное указание --as снимает неопределённость",
          forced.kind == "requirements")

    section("--no-promote оставляет записи в staging")
    st2 = make_store(dim=64)
    res7 = autoload(st2, cfg, tz, actor="engineer", promote=False)
    check("требования НЕ созданы", res7.requirements_created == 0)
    check("записи в staging есть",
          len(st2.staging_records(status="new")) == 3)
    check("подсказано, как перенести",
          any("saps promote" in w for w in res7.warnings))
    st2.close()

    section("Смена модели эмбеддингов: сброс векторов")
    before = st.embedding_coverage()
    check("векторы были", before["clauses_indexed"] > 0)
    cleared = st.clear_embeddings()
    check("векторы сброшены", cleared["clauses"] == 3)
    after = st.embedding_coverage()
    check("покрытие обнулилось",
          after["clauses_indexed"] == 0 and after["requirements_indexed"] == 0,
          "иначе поиск смешивал бы векторы разных моделей — молча и неверно")
    from saps.agents.classifier import index_clauses, index_requirements
    from saps.llm import build_embedder as be
    emb64 = be("hash", "hash-64", dim=64)
    check("переиндексация восстанавливает",
          index_clauses(st, emb64) == 3 and index_requirements(st, emb64) >= 3)

    section("Загрузка справочника через внешнюю модель")
    FakeEmbeddings.dim = 64            # под размерность тестовой схемы
    FakeEmbeddings.requests = []
    st3 = make_store(dim=64)
    cfg_ext = make_config(embedding_dim=64, embedding_provider="lmstudio",
                          embedding_model="nomic-embed-text",
                          embedding_base_url=url, embedding_batch=2)
    res8 = autoload(st3, cfg_ext, book, actor="engineer")
    check("справочник загружен внешней моделью", res8.clauses_loaded == 3,
          res8.report())
    check("эмбеддинги посчитаны сервером",
          st3.embedding_coverage()["clauses_indexed"] == 3)
    check("использованы пачки, а не по одному запросу",
          len(FakeEmbeddings.requests) == 2,
          f"запросов {len(FakeEmbeddings.requests)} при batch=2 на 3 пункта")
    check("в отчёте назван провайдер",
          any("lmstudio" in s.detail for s in res8.steps),
          str([s.detail for s in res8.steps]))
    st3.close()

    st.close()
    httpd.shutdown()
    harness.cleanup()
    return summary("PDF и автозагрузка")


if __name__ == "__main__":
    raise SystemExit(main())
