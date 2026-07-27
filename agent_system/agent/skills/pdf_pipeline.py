"""Навык «pdf»: чтение PDF, определение типа документа/страницы,
постраничное распознавание через vision-модель.

Конвейер ровно из двух шагов, как задумано в задаче:

  ШАГ 1 (pdf_info, pdf_classify) — БЕЗ обращения к модели, дёшево и
  детерминированно. PyMuPDF даёт текстовый слой, векторную графику,
  растровые изображения и таблицы каждой страницы; по набору эвристик
  (доля текста, плотность векторных линий, наличие и содержимое таблицы,
  ключевые слова основной надписи чертежа/счёта и т.п.) странице
  присваивается тип. Классификация ПОСТРАНИЧНАЯ, а не по документу
  целиком: реальный комплект — это, например, титульный лист (проза),
  спецификация (bom_table) и сами чертежи (technical_drawing) в одном
  файле, и шаг 2 обязан обращаться с ними по-разному.

  ШАГ 2 (pdf_extract_page, pdf_extract) — страница рендерится в PNG и
  отправляется vision-модели с ИНСТРУКЦИЕЙ, ПОДОБРАННОЙ ПОД ТИП со шага 1:
    * bom_table / invoice_financial -> строгий построчный JSON;
    * technical_drawing             -> markdown: основная надпись,
                                        технические требования, позиции;
    * prose_text / mixed_report     -> дословный markdown без пересказа;
    * presentation_slide            -> markdown: заголовок + тезисы;
    * scanned_image                 -> markdown, как для прозы, но с
                                        оговоркой о низкой уверенности OCR;
    * blank_page                    -> пустой результат без обращения к
                                        модели вообще (сберегает токены).

Правила классификации и промпты извлечения вынесены в doc_types.py и
ОБЩИЕ с навыком docparse (Word/Excel/текст) — таблица остаётся таблицей,
а проза прозой независимо от того, лежит она в PDF или в .docx.

Пакет pymupdf — единственная внешняя зависимость этого навыка (остальная
система работает на голой stdlib). Импортируется ЛЕНИВО внутри build():
если библиотеки нет, а навык не запрашивался — на остальном агенте это
не сказывается; если запрошен — инструменты вернут понятную инструкцию
по установке, а не трейсбек.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import doc_types as dt
from ..llm.base import BaseLLM, LLMError
from ..tools.base import Tool, ToolError, Workspace

# Обратная совместимость публичного имени: раньше словарь жил здесь.
PAGE_TYPES = dt.DOC_TYPES
_BOM_WORDS = dt.BOM_WORDS
_INVOICE_WORDS = dt.INVOICE_WORDS
_DRAWING_WORDS = dt.DRAWING_WORDS


def _keyword_hits(text: str, words: list[str]) -> int:
    return dt.keyword_hits(text, words)


@dataclass
class PageSignal:
    """Признаки страницы, посчитанные локально (без LLM), и вывод."""

    page: int                     # 1-based
    doc_type: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


def _require_fitz():
    try:
        import fitz  # type: ignore
    except ImportError as exc:
        raise ToolError(
            "Навык pdf требует библиотеку PyMuPDF. Установите: "
            "pip install pymupdf"
        ) from exc
    return fitz


def _page_signals(page: Any) -> dict[str, Any]:
    """Считает признаки страницы PyMuPDF. Всё локально, без сети/LLM."""
    text = page.get_text("text") or ""
    stripped = text.strip()
    words = page.get_text("words") or []
    try:
        tables = page.find_tables().tables
    except Exception:
        tables = []
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    try:
        images = page.get_image_info()
    except Exception:
        images = []
    rect = page.rect
    area = max(1.0, rect.width * rect.height)
    img_area = sum(
        max(0.0, (im["bbox"][2] - im["bbox"][0]) * (im["bbox"][3] - im["bbox"][1]))
        for im in images if im.get("bbox")
    )
    sizes: list[float] = []
    try:
        d = page.get_text("dict")
        for b in d.get("blocks", []):
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    if sp.get("text", "").strip():
                        sizes.append(sp.get("size", 0.0))
    except Exception:
        pass
    max_font = max(sizes) if sizes else 0.0
    median_font = sorted(sizes)[len(sizes) // 2] if sizes else 0.0

    table_text = ""
    if tables:
        try:
            names = tables[0].header.names if tables[0].header else []
            table_text += " ".join(str(n) for n in names)
        except Exception:
            pass

    return {
        "chars": len(stripped),
        "words": len(words),
        "tables": len(tables),
        "vector_paths": len(drawings),
        "image_area_ratio": round(img_area / area, 3),
        "landscape": rect.width > rect.height,
        "max_font": round(max_font, 1),
        "median_font": round(median_font, 1),
        "bom_hits": dt.keyword_hits(text + " " + table_text, dt.BOM_WORDS),
        "invoice_hits": dt.keyword_hits(text + " " + table_text, dt.INVOICE_WORDS),
        "drawing_hits": dt.keyword_hits(text, dt.DRAWING_WORDS),
    }


def classify_page(page: Any, page_no: int) -> PageSignal:
    """Эвристическая классификация одной страницы через общее дерево
    решений doc_types.classify_signals — то же дерево использует docparse
    для листов Excel и разделов Word."""
    s = _page_signals(page)
    doc_type, confidence, reasons = dt.classify_signals(s)
    return PageSignal(page_no, doc_type, confidence, reasons, s)


def _parse_pages(spec: str, total: int) -> list[int]:
    """'1-3,5,9-' -> отсортированный список 1-based номеров без повторов."""
    if not spec.strip():
        return list(range(1, total + 1))
    out: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            lo = int(a) if a.strip() else 1
            hi = int(b) if b.strip() else total
        else:
            lo = hi = int(chunk)
        lo = max(1, lo)
        hi = min(total, hi)
        out.update(range(lo, hi + 1))
    return sorted(out)


# Обратная совместимость публичного имени/структуры промптов.
EXTRACTION_SYSTEM = dt.EXTRACTION_SYSTEM
_PROMPTS = dt.PROMPTS


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json|markdown|md)?\s*\n(.*)\n```$", t, re.S)
    return m.group(1).strip() if m else t


def _looks_like_json(fmt: str) -> bool:
    return fmt in ("bom_table", "invoice_financial")


def _render_page_png(fitz, doc: Any, page_no0: int, dpi: int) -> bytes:
    page = doc[page_no0]
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


# ------------------------------------------------------------------ build
def build(ws: Workspace, vision_llm: BaseLLM, dpi: int = 170,
          max_pages_per_call: int = 25) -> list[Tool]:
    fitz_mod = None  # ленивый импорт при первом реальном вызове
    _default_dpi = [dpi]


    def _fitz():
        nonlocal fitz_mod
        if fitz_mod is None:
            fitz_mod = _require_fitz()
        return fitz_mod

    def _open(path: str):
        fitz = _fitz()
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        try:
            return fitz.open(p)
        except Exception as exc:  # повреждённый/не-PDF файл
            raise ToolError(f"Не удалось открыть {path!r} как PDF: {exc}") from exc

    # ------------------------------------------------------------ pdf_info
    def pdf_info(path: str) -> str:
        doc = _open(path)
        try:
            lines = [f"{ws.relative(ws.resolve(path))}: {len(doc)} стр."]
            for i, page in enumerate(doc, 1):
                text = (page.get_text("text") or "").strip()
                imgs = page.get_images(full=True)
                tabs = 0
                try:
                    tabs = len(page.find_tables().tables)
                except Exception:
                    pass
                lines.append(
                    f"  стр.{i}: {len(text)} симв. текста, {len(imgs)} "
                    f"растр., {tabs} табл., {'альбом' if page.rect.width > page.rect.height else 'портрет'}"
                )
            return "\n".join(lines)
        finally:
            doc.close()

    # -------------------------------------------------------- pdf_classify
    def pdf_classify(path: str, pages: str = "") -> str:
        """ШАГ 1: определить тип каждой страницы локально, без LLM."""
        doc = _open(path)
        try:
            nums = _parse_pages(pages, len(doc))
            if not nums:
                raise ToolError("Пустой диапазон страниц")
            out = [f"{ws.relative(ws.resolve(path))}: классификация "
                   f"{len(nums)} стр. из {len(doc)}"]
            counts: dict[str, int] = {}
            for n in nums:
                sig = classify_page(doc[n - 1], n)
                counts[sig.doc_type] = counts.get(sig.doc_type, 0) + 1
                out.append(
                    f"  стр.{n}: {sig.doc_type} (увер. {sig.confidence:.2f}) — "
                    + "; ".join(sig.reasons)
                )
            summary = ", ".join(f"{k}: {v}" for k, v in
                                sorted(counts.items(), key=lambda kv: -kv[1]))
            out.append(f"Итого по типам: {summary}")
            out.append("Доступные типы: " + ", ".join(
                f"{k} ({v})" for k, v in PAGE_TYPES.items()))
            return "\n".join(out)
        finally:
            doc.close()

    # ---------------------------------------------------- вызов vision-LLM
    def _extract_one(doc, page_no: int, doc_type: str,
                      out_format: str, dpi_: int) -> tuple[str, str]:
        """Возвращает (итоговый_текст, фактический_формат: 'json'|'markdown')."""
        if doc_type == "blank_page":
            return "(страница пуста, распознавание не требуется)", "markdown"

        fitz = _fitz()
        png = _render_page_png(fitz, doc, page_no - 1, dpi_)

        fmt = out_format
        if fmt == "auto":
            fmt = "json" if _looks_like_json(doc_type) else "markdown"

        prompt = _PROMPTS.get(doc_type, _PROMPTS["mixed_report"])
        if fmt == "json" and not _looks_like_json(doc_type):
            prompt += ("\n\nВерни результат СТРОГО как JSON-объект вида "
                      '{"type": "' + doc_type + '", "page": <номер>, '
                      '"content": <markdown как строка>} без обвязки кода.')
        prompt = f"Номер страницы: {page_no}.\n\n{prompt}"

        try:
            reply = vision_llm.vision_chat(EXTRACTION_SYSTEM, prompt,
                                           [(png, "image/png")])
        except LLMError as exc:
            raise ToolError(f"Модель распознавания недоступна: {exc}") from exc

        text = _strip_code_fence(reply.text or "")
        if not text:
            raise ToolError(
                f"Модель вернула пустой ответ для страницы {page_no}. "
                "Проверьте, что выбранная модель поддерживает изображения."
            )

        if fmt == "json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                text = ("ПРЕДУПРЕЖДЕНИЕ: ответ не является валидным JSON, "
                        "возвращён как есть для ручной проверки.\n\n" + text)
        return text, fmt

    # --------------------------------------------------- pdf_extract_page
    def pdf_extract_page(path: str, page: int, doc_type: str = "",
                         out_format: str = "auto",
                         save_to: str = "", dpi: int = 0) -> str:
        """ШАГ 2 для одной страницы: рендер в картинку + распознавание LLM."""
        doc = _open(path)
        try:
            if page < 1 or page > len(doc):
                raise ToolError(f"Страница {page} вне диапазона 1..{len(doc)}")
            dtype = doc_type.strip()
            if not dtype:
                dtype = classify_page(doc[page - 1], page).doc_type
            elif dtype not in PAGE_TYPES:
                raise ToolError(
                    f"Неизвестный doc_type {dtype!r}. Доступны: "
                    f"{', '.join(PAGE_TYPES)}"
                )
            text, fmt = _extract_one(doc, page, dtype, out_format,
                                     dpi or _default_dpi[0])
            header = f"[стр. {page}, тип {dtype}, формат {fmt}]\n"
            result = header + text
            if save_to.strip():
                out_p = ws.resolve(save_to)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(text, encoding="utf-8")
                result += f"\n\nСохранено: {ws.relative(out_p)}"
            return result
        finally:
            doc.close()

    # -------------------------------------------------------- pdf_extract
    def pdf_extract(path: str, pages: str = "", out_dir: str = "",
                    out_format: str = "auto") -> str:
        """ШАГ 1+2 пакетно: классифицирует и распознаёт несколько страниц.

        Ограничен pdf_max_pages_per_call за один вызов — так агент не
        сжигает бюджет на сотни страниц одним запросом и видит прогресс.
        """
        doc = _open(path)
        try:
            nums = _parse_pages(pages, len(doc))
            if not nums:
                raise ToolError("Пустой диапазон страниц")
            if len(nums) > max_pages_per_call:
                raise ToolError(
                    f"Запрошено {len(nums)} страниц — это больше лимита "
                    f"{max_pages_per_call} за один вызов. Разбейте на "
                    "несколько вызовов pdf_extract с разными pages, "
                    "например '1-25', затем '26-50'."
                )
            base = ws.resolve(out_dir) if out_dir.strip() else None
            if base:
                base.mkdir(parents=True, exist_ok=True)

            lines = [f"{ws.relative(ws.resolve(path))}: распознано "
                     f"{len(nums)} стр."]
            saved: list[str] = []
            for n in nums:
                sig = classify_page(doc[n - 1], n)
                try:
                    text, fmt = _extract_one(doc, n, sig.doc_type,
                                             out_format, _default_dpi[0])
                except ToolError as exc:
                    lines.append(f"  стр.{n}: {sig.doc_type} — ОШИБКА: {exc}")
                    continue
                status = f"  стр.{n}: {sig.doc_type} -> {fmt}, {len(text)} симв."
                if base:
                    ext = "json" if fmt == "json" else "md"
                    out_p = base / f"page_{n:04d}.{ext}"
                    out_p.write_text(text, encoding="utf-8")
                    saved.append(ws.relative(out_p))
                    status += f" ({ws.relative(out_p)})"
                lines.append(status)
            if base:
                manifest = base / "manifest.md"
                manifest.write_text(
                    "# Результат распознавания\n\n" + "\n".join(
                        f"- {s}" for s in saved),
                    encoding="utf-8")
                lines.append(f"Манифест: {ws.relative(manifest)}")
            return "\n".join(lines)
        finally:
            doc.close()

    return [
        Tool("pdf_info",
             "Быстрая сводка по PDF без распознавания: число страниц и на "
             "каждой странице — объём текстового слоя, число растровых "
             "картинок и таблиц, ориентация. Смотреть перед тем, как "
             "запускать классификацию/распознавание на большом файле.",
             {"type": "object",
              "properties": {"path": {"type": "string"}},
              "required": ["path"]},
             pdf_info),
        Tool("pdf_classify",
             "ШАГ 1: определить тип каждой страницы PDF локально, без "
             "обращения к модели (таблица BOM/счёт, чертёж, слайд, проза, "
             "скан, пусто, смешанное). Нужен, чтобы решить, как именно "
             "распознавать страницу на шаге 2.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "pages": {"type": "string",
                            "description": "Диапазон вида '1-3,5', "
                                          "пусто = все страницы"}},
              "required": ["path"]},
             pdf_classify),
        Tool("pdf_extract_page",
             "ШАГ 2 для одной страницы: рендерит страницу в изображение и "
             "распознаёт её vision-моделью с учётом типа документа "
             "(bom_table/invoice_financial -> JSON, technical_drawing/"
             "prose_text/presentation_slide/mixed_report -> markdown). "
             "Если doc_type не указан — определяется автоматически, как "
             "в pdf_classify.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "page": {"type": "integer", "description": "Номер страницы, с 1"},
                  "doc_type": {"type": "string",
                               "description": "Переопределить тип: " +
                                             ", ".join(PAGE_TYPES)},
                  "out_format": {"type": "string",
                                 "description": "auto|markdown|json"},
                  "save_to": {"type": "string",
                              "description": "Куда сохранить результат "
                                            "(необязательно)"},
                  "dpi": {"type": "integer",
                          "description": "Разрешение рендера, по умолчанию из конфига"}},
              "required": ["path", "page"]},
             pdf_extract_page),
        Tool("pdf_extract",
             "ШАГ 1+2 пакетно для диапазона страниц: классифицирует и "
             "распознаёт каждую, сохраняет по файлу на страницу "
             "(page_NNNN.md/.json) в out_dir и манифест. Лимит страниц за "
             "вызов — чтобы не сжечь бюджет и не потерять прогресс при сбое; "
             "большой документ обрабатывайте несколькими вызовами.",
             {"type": "object",
              "properties": {
                  "path": {"type": "string"},
                  "pages": {"type": "string",
                            "description": "Диапазон вида '1-25', пусто = все"},
                  "out_dir": {"type": "string",
                              "description": "Папка для page_NNNN.md/.json "
                                            "и manifest.md"},
                  "out_format": {"type": "string",
                                 "description": "auto|markdown|json"}},
              "required": ["path"]},
             pdf_extract),
    ]
