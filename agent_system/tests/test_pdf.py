"""Тесты навыка pdf: классификация страниц и постраничное распознавание.

Требуют pymupdf. Если библиотека не установлена, тесты сами создают
PDF нечем — пропускаем модуль целиком с понятным сообщением, а не падаем
с ImportError посреди прогона. Остальной набор (make test) от pymupdf
не зависит.

Философия та же: тест обязан уметь падать. Рядом с позитивными
проверками — негативные: битый файл, страница вне диапазона, лимит
страниц за вызов, неизвестный doc_type, ответ модели не в том формате.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import Config                                  # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, Usage               # noqa: E402
from agent.tools.base import ToolError, Workspace                 # noqa: E402

PASS, FAIL = 0, 0

try:
    import fitz  # type: ignore
    HAVE_FITZ = True
except ImportError:
    HAVE_FITZ = False


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


# ------------------------------------------------------------- фикстуры
def _make_bom_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    x0, y0, w, h = 50, 50, 480, 220
    cols, rows = 5, 6
    for i in range(cols + 1):
        page.draw_line((x0 + i * w / cols, y0), (x0 + i * w / cols, y0 + h))
    for j in range(rows + 1):
        page.draw_line((x0, y0 + j * h / rows), (x0 + w, y0 + j * h / rows))
    headers = ["Poz", "Designation", "Name", "Qty", "Material"]
    for i, ht in enumerate(headers):
        page.insert_text((x0 + i * w / cols + 5, y0 + 15), ht, fontsize=9)
    data = [["1", "AB-01", "Housing", "1", "Steel"],
            ["2", "AB-02", "Cover", "1", "Steel"],
            ["3", "AB-03", "Screw", "4", "Steel"],
            ["4", "AB-04", "Bearing", "2", "Bronze"],
            ["5", "AB-05", "Gasket", "1", "Rubber"]]
    for r, row in enumerate(data):
        for c, v in enumerate(row):
            page.insert_text((x0 + c * w / cols + 5, y0 + (r + 1) * h / rows + 15),
                             v, fontsize=9)
    doc.save(path)
    doc.close()


def _make_invoice_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "INVOICE No. 2024-118", fontsize=14)
    page.insert_text((50, 70), "Bill To: Acme Corp", fontsize=10)
    x0, y0, w, h = 50, 100, 480, 140
    cols, rows = 4, 4
    for i in range(cols + 1):
        page.draw_line((x0 + i * w / cols, y0), (x0 + i * w / cols, y0 + h))
    for j in range(rows + 1):
        page.draw_line((x0, y0 + j * h / rows), (x0 + w, y0 + j * h / rows))
    for i, ht in enumerate(["Item", "Qty", "Unit Price", "Amount"]):
        page.insert_text((x0 + i * w / cols + 5, y0 + 15), ht, fontsize=9)
    for r, row in enumerate([["Widget", "10", "5.00", "50.00"],
                             ["Gadget", "2", "20.00", "40.00"]]):
        for c, v in enumerate(row):
            page.insert_text((x0 + c * w / cols + 5, y0 + (r + 1) * h / rows + 15),
                             v, fontsize=9)
    page.insert_text((400, 260), "Total due: 90.00", fontsize=11)
    doc.save(path)
    doc.close()


def _make_prose_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    text = ("Глава первая\n\n"
           "Был тихий осенний вечер, когда герой вышел на крыльцо и "
           "посмотрел на дорогу, уходящую вдаль между полей, желтевших "
           "под низким солнцем. Он думал о том, что скоро придётся "
           "уезжать, и от этой мысли на душе становилось тяжело и "
           "одиноко, будто что-то важное оставалось позади навсегда, "
           "и вернуть его будет уже нельзя, как ни старайся потом.")
    page.insert_textbox(fitz.Rect(50, 50, 500, 700), text, fontsize=12)
    doc.save(path)
    doc.close()


def _make_drawing_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.draw_line((50 + i * 5, 50), (50 + i * 5, 300))
    page.insert_text((60, 320), "Масштаб 1:2", fontsize=8)
    page.insert_text((60, 335), "Формат A3", fontsize=8)
    page.insert_text((60, 350), "Чертил Иванов", fontsize=8)
    page.insert_text((60, 365), "Н.контр Петров", fontsize=8)
    doc.save(path)
    doc.close()


def _make_slide_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((60, 80), "Стратегия на 2026 год", fontsize=28)
    for i, b in enumerate(["Рост выручки", "Новый рынок", "Снижение затрат"]):
        page.insert_text((80, 160 + i * 40), "• " + b, fontsize=16)
    doc.save(path)
    doc.close()


def _make_scanned_pdf(path: Path) -> None:
    import io
    from PIL import Image
    doc = fitz.open()
    page = doc.new_page()
    img = Image.new("RGB", (800, 1000), (230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=buf.getvalue())
    doc.save(path)
    doc.close()


def _make_blank_pdf(path: Path) -> None:
    doc = fitz.open()
    doc.new_page()
    doc.save(path)
    doc.close()


def _make_multi_type_pdf(path: Path) -> None:
    """Один файл, три страницы трёх разных типов — как в жизни."""
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_textbox(fitz.Rect(50, 50, 500, 700),
                      "Титульный лист документа. " * 20, fontsize=12)
    p2 = doc.new_page()
    x0, y0, w, h = 50, 50, 480, 150
    for i in range(4):
        p2.draw_line((x0 + i * w / 3, y0), (x0 + i * w / 3, y0 + h))
    for j in range(4):
        p2.draw_line((x0, y0 + j * h / 3), (x0 + w, y0 + j * h / 3))
    for i, ht in enumerate(["Поз.", "Обозначение", "Наименование"]):
        p2.insert_text((x0 + i * w / 3 + 5, y0 + 15), ht, fontsize=9)
    p3 = doc.new_page()
    for i in range(40):
        p3.draw_line((50 + i * 5, 50), (50 + i * 5, 300))
    p3.insert_text((60, 320), "Масштаб 1:1", fontsize=8)
    doc.save(path)
    doc.close()


# --------------------------------------------------------- фальшивая LLM
class FakeVisionLLM(BaseLLM):
    """Модель, отдающая канонический ответ по запрошенному типу.

    Записывает КАЖДЫЙ вызов (в т.ч. картинку), чтобы тесты могли
    проверить, что странице отправили именно ту инструкцию, которая
    привязана к её типу.
    """

    def __init__(self, script: dict[str, LLMReply] | None = None) -> None:
        super().__init__("fake-vision")
        self.calls: list[dict] = []
        self.script = script or {}
        self.next_text = "не настроено"

    def chat(self, messages, tools=None):
        user_msg = messages[-1]
        content = user_msg.get("content")
        text_part = ""
        n_images = 0
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    text_part = block["text"]
                elif block.get("type") == "image_url":
                    n_images += 1
        self.calls.append({"prompt": text_part, "images": n_images})
        for key, reply in self.script.items():
            if key in text_part:
                return reply
        return LLMReply(text=self.next_text, usage=Usage(10, 10))


def _import_pdf_pipeline():
    from agent.skills import pdf_pipeline
    return pdf_pipeline


# ================================================================ tests
def test_classify_heuristics() -> None:
    section("Классификация страниц (шаг 1, без LLM)")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cases = {
            "bom": (_make_bom_pdf, "bom_table"),
            "invoice": (_make_invoice_pdf, "invoice_financial"),
            "prose": (_make_prose_pdf, "prose_text"),
            "drawing": (_make_drawing_pdf, "technical_drawing"),
            "slide": (_make_slide_pdf, "presentation_slide"),
            "scanned": (_make_scanned_pdf, "scanned_image"),
            "blank": (_make_blank_pdf, "blank_page"),
        }
        for name, (maker, expected) in cases.items():
            p = td / f"{name}.pdf"
            maker(p)
            doc = fitz.open(p)
            sig = pdf_pipeline.classify_page(doc[0], 1)
            check(f"{name} -> {expected}", sig.doc_type == expected,
                  f"получено {sig.doc_type} ({sig.reasons})")
            check(f"{name}: увер. в (0,1]", 0 < sig.confidence <= 1.0)
            doc.close()

        # НЕГАТИВНЫЙ: BOM не должен принять за прозу и наоборот
        p = td / "bom.pdf"
        doc = fitz.open(p)
        sig = pdf_pipeline.classify_page(doc[0], 1)
        check("BOM не спутан с прозой", sig.doc_type != "prose_text")
        doc.close()


def test_multi_page_classification() -> None:
    section("Классификация многостраничного документа (разные типы страниц)")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "multi.pdf"
        _make_multi_type_pdf(p)
        doc = fitz.open(p)
        types = [pdf_pipeline.classify_page(doc[i], i + 1).doc_type
                for i in range(len(doc))]
        check("3 страницы", len(types) == 3, str(types))
        check("стр.1 — проза", types[0] == "prose_text", types[0])
        check("стр.2 — BOM", types[1] == "bom_table", types[1])
        check("стр.3 — чертёж", types[2] == "technical_drawing", types[2])
        doc.close()


def test_parse_pages() -> None:
    section("Разбор диапазона страниц")
    pdf_pipeline = _import_pdf_pipeline()
    check("пусто = все", pdf_pipeline._parse_pages("", 5) == [1, 2, 3, 4, 5])
    check("список", pdf_pipeline._parse_pages("1,3,5", 5) == [1, 3, 5])
    check("диапазон", pdf_pipeline._parse_pages("2-4", 5) == [2, 3, 4])
    check("открытый справа", pdf_pipeline._parse_pages("3-", 5) == [3, 4, 5])
    check("выход за границы обрезается",
          pdf_pipeline._parse_pages("0-100", 5) == [1, 2, 3, 4, 5])
    check("дубликаты схлопываются",
          pdf_pipeline._parse_pages("1,1,2-3,3", 5) == [1, 2, 3])


def test_extract_page_json_and_markdown() -> None:
    section("Распознавание страницы (шаг 2): формат по типу документа")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td / "ws")
        bom_pdf = td / "bom.pdf"
        _make_bom_pdf(bom_pdf)
        # копируем внутрь workspace, т.к. инструменты работают в его границах
        wp = ws.root / "bom.pdf"
        wp.write_bytes(bom_pdf.read_bytes())

        fake = FakeVisionLLM({
            "bom_table": LLMReply(
                text=json.dumps({"type": "bom_table", "page": 1,
                                 "columns": ["Poz", "Name"],
                                 "items": [{"Poz": "1", "Name": "Housing"}]}),
                usage=Usage(50, 20)),
        })
        tools = {t.name: t for t in pdf_pipeline.build(ws, fake, dpi=80)}

        out = tools["pdf_extract_page"].fn(path="bom.pdf", page=1)
        check("тип определён как bom_table", "bom_table" in out, out[:120])
        check("формат json", "формат json" in out, out[:120])
        check("картинка отправлена модели", fake.calls[-1]["images"] == 1)
        check("в промпте номер страницы", "страница: 1" in fake.calls[-1]["prompt"].lower()
              or "Номер страницы: 1" in fake.calls[-1]["prompt"])

        parsed = json.loads(out.split("\n", 1)[1])
        check("JSON распарсился", parsed["type"] == "bom_table")
        check("items на месте", parsed["items"][0]["Poz"] == "1")

        # prose — markdown, инструкция другая (дословная транскрипция)
        prose_pdf = td / "prose.pdf"
        _make_prose_pdf(prose_pdf)
        (ws.root / "prose.pdf").write_bytes(prose_pdf.read_bytes())
        fake.next_text = "# Глава первая\n\nБыл тихий осенний вечер..."
        out2 = tools["pdf_extract_page"].fn(path="prose.pdf", page=1)
        check("тип определён как prose_text", "prose_text" in out2, out2[:120])
        check("формат markdown", "формат markdown" in out2, out2[:120])
        check("промпт для прозы отличается от BOM",
              "дословную" in fake.calls[-1]["prompt"].lower())



def test_extract_page_blank_skips_llm() -> None:
    section("Пустая страница не отправляется модели (экономия)")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        blank = ws.root / "blank.pdf"
        tmp = Path(td) / "b.pdf"
        _make_blank_pdf(tmp)
        blank.write_bytes(tmp.read_bytes())
        fake = FakeVisionLLM()
        tools = {t.name: t for t in pdf_pipeline.build(ws, fake)}
        out = tools["pdf_extract_page"].fn(path="blank.pdf", page=1)
        check("страница помечена как пустая", "пуста" in out)
        check("модель НЕ вызывалась", len(fake.calls) == 0,
              "напрасно потрачен вызов модели на пустую страницу")


def test_save_to_and_batch_extract() -> None:
    section("Сохранение результата и пакетное распознавание (шаг 1+2)")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        multi = ws.root / "multi.pdf"
        tmp = Path(td) / "m.pdf"
        _make_multi_type_pdf(tmp)
        multi.write_bytes(tmp.read_bytes())

        fake = FakeVisionLLM()
        fake.next_text = "результат распознавания"
        tools = {t.name: t for t in pdf_pipeline.build(ws, fake, dpi=72)}

        out = tools["pdf_extract_page"].fn(
            path="multi.pdf", page=1, save_to="out/p1.md")
        check("сохранение отражено в ответе", "out/p1.md" in out.replace("\\", "/"))
        check("файл реально создан", (ws.root / "out" / "p1.md").exists())
        check("в файле только текст, без заголовка",
              (ws.root / "out" / "p1.md").read_text() == "результат распознавания")

        out_batch = tools["pdf_extract"].fn(
            path="multi.pdf", pages="1-3", out_dir="batch")
        check("обработаны все 3 страницы", "распознано 3 стр." in out_batch, out_batch)
        for n in (1, 2, 3):
            check(f"файл страницы {n} создан",
                  (ws.root / "batch" / f"page_{n:04d}.md").exists() or
                  (ws.root / "batch" / f"page_{n:04d}.json").exists())
        check("манифест создан", (ws.root / "batch" / "manifest.md").exists())


def test_negative_cases() -> None:
    section("Негативные проверки")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        bom = ws.root / "bom.pdf"
        tmp = Path(td) / "b.pdf"
        _make_bom_pdf(tmp)
        bom.write_bytes(tmp.read_bytes())

        fake = FakeVisionLLM()
        tools = {t.name: t for t in pdf_pipeline.build(ws, fake, dpi=72,
                                                        max_pages_per_call=2)}

        try:
            tools["pdf_info"].fn(path="nope.pdf")
            check("отказ на отсутствующий файл", False)
        except ToolError:
            check("отказ на отсутствующий файл", True)

        bad = ws.root / "bad.pdf"
        bad.write_text("это не pdf, а текстовый мусор", encoding="utf-8")
        try:
            tools["pdf_info"].fn(path="bad.pdf")
            check("отказ на битый PDF", False)
        except ToolError:
            check("отказ на битый PDF", True)

        try:
            tools["pdf_extract_page"].fn(path="bom.pdf", page=99)
            check("отказ на страницу вне диапазона", False)
        except ToolError:
            check("отказ на страницу вне диапазона", True)

        try:
            tools["pdf_extract_page"].fn(path="bom.pdf", page=1,
                                         doc_type="выдуманный_тип")
            check("отказ на неизвестный doc_type", False)
        except ToolError:
            check("отказ на неизвестный doc_type", True)

        multi = ws.root / "multi.pdf"
        tmp3 = Path(td) / "m3.pdf"
        _make_multi_type_pdf(tmp3)
        multi.write_bytes(tmp3.read_bytes())
        try:
            tools["pdf_extract"].fn(path="multi.pdf", pages="1-3")
            check("отказ при превышении лимита страниц за вызов", False)
        except ToolError as exc:
            check("отказ при превышении лимита страниц за вызов", True)
            check("сообщение объясняет лимит", "лимит" in str(exc) ,str(exc))

        try:
            pdf_pipeline._parse_pages("не число", 5)
            check("отказ на мусор в диапазоне страниц", False)
        except ValueError:
            check("отказ на мусор в диапазоне страниц", True)


def test_invalid_json_from_model_is_flagged() -> None:
    section("Модель вернула не-JSON для табличного типа — помечаем, не роняем")
    pdf_pipeline = _import_pdf_pipeline()
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        bom = ws.root / "bom.pdf"
        tmp = Path(td) / "b.pdf"
        _make_bom_pdf(tmp)
        bom.write_bytes(tmp.read_bytes())

        fake = FakeVisionLLM()
        fake.next_text = "Позиция 1: Housing, позиция 2: Cover"  # не JSON
        tools = {t.name: t for t in pdf_pipeline.build(ws, fake, dpi=72)}
        out = tools["pdf_extract_page"].fn(path="bom.pdf", page=1)
        check("предупреждение о невалидном JSON", "ПРЕДУПРЕЖДЕНИЕ" in out, out[:200])
        check("сырой текст модели сохранён", "Housing" in out)


def test_build_agent_with_pdf_skill() -> None:
    section("Сборка агента с навыком pdf")
    from agent.build import build_agent
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="llava", workspace=td,
                    skills=["files", "pdf"])
        agent = build_agent(cfg)
        names = agent.tools.names()
        for t in ("pdf_info", "pdf_classify", "pdf_extract_page", "pdf_extract"):
            check(f"инструмент {t} зарегистрирован", t in names)

        # отдельная vision-модель конфигурируется независимо от основной
        cfg2 = Config(provider="ollama", model="qwen2.5-coder", workspace=td,
                     skills=["pdf"], vision_provider="ollama",
                     vision_model="llava")
        provider, model, base_url, key = cfg2.resolve_vision()
        check("vision-модель переопределяется отдельно",
              model == "llava" and provider == "ollama")

        cfg3 = Config(provider="openai", model="gpt-4o-mini", workspace=td,
                     skills=["pdf"])
        provider3, model3, _, _ = cfg3.resolve_vision()
        check("без vision_* используется основная модель",
              model3 == "gpt-4o-mini" and provider3 == "openai")


def main() -> int:
    if not HAVE_FITZ:
        print("pymupdf не установлен — тесты навыка pdf пропущены "
              "(pip install pymupdf)")
        return 0
    test_classify_heuristics()
    test_multi_page_classification()
    test_parse_pages()
    test_extract_page_json_and_markdown()
    test_extract_page_blank_skips_llm()
    test_save_to_and_batch_extract()
    test_negative_cases()
    test_invalid_json_from_model_is_flagged()
    test_build_agent_with_pdf_skill()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
