"""Чтение PDF: справочники авиационных правил и ТЗ в виде PDF.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ РАСШИРЕНИЕ WORD-ПАРСЕРА. У .docx есть
точный структурный слой: заголовок помечен стилем, таблица — тегом. В
PDF структуры нет вообще — есть буквы с координатами. Всё, что мы
называем «пунктом 25.1309», приходится восстанавливать по признакам
текста, и делать это надо ЯВНО и проверяемо, а не прятать в общий
парсер.

ЕДИНСТВЕННАЯ ВНЕШНЯЯ ЗАВИСИМОСТЬ ВО ВСЁМ ПРОЕКТЕ. PDF нельзя разобрать
стандартной библиотекой: это бинарный формат со сжатием потоков и
собственными кодировками шрифтов. Поэтому здесь ЛЕНИВЫЙ импорт двух
возможных движков:

  * pymupdf (fitz) — быстрый и лучше держит сложную вёрстку;
  * pypdf — чистый Python, ставится где угодно, но слабее на колонках.

Отсутствие обоих не ломает САПС: импорт Word/Excel и все агенты
работают, а команда с PDF выдаёт понятную инструкцию по установке.

ЧТО ДЕЛАЕТ ОЧИСТКА ТЕКСТА (и почему без неё справочник получается
мусорным):
  * убирает КОЛОНТИТУЛЫ — строки, повторяющиеся на большинстве страниц
    («Авиационные правила Часть 25» на каждой из 400 страниц иначе
    попадёт в текст каждого пункта и испортит эмбеддинги);
  * убирает НОМЕРА СТРАНИЦ («- 15 -», «Стр. 15 из 400»);
  * склеивает ПЕРЕНОСЫ: «неверо-\nятное» -> «невероятное». Без этого
    поиск по слову «невероятное» не найдёт пункт;
  * склеивает строки внутри абзаца, сохраняя границы абзацев.

ГЛАВНОЕ ОГРАНИЧЕНИЕ, О КОТОРОМ НАДО ЗНАТЬ ЧЕСТНО: PDF из сканов (просто
картинки) здесь не читается — нужен OCR, а это отдельная тяжёлая
зависимость и отдельный класс ошибок распознавания. Модуль ОБНАРУЖИВАЕТ
такой случай и говорит об этом прямо, вместо того чтобы вернуть пустой
результат и создать впечатление, что документ пуст.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .word import ParseError

#: Ниже этого числа символов на страницу документ считается сканом.
#: Осмысленная страница правил содержит 1500–3000 знаков; 80 — это
#: обычно только колонтитул и номер страницы, то есть текстового слоя
#: фактически нет.
SCAN_CHARS_PER_PAGE = 80

#: Строка считается колонтитулом, если встречается более чем на этой
#: доле страниц. 0.5 подобрано так, чтобы поймать колонтитул документа
#: (он на каждой странице), но не выбросить фразу, честно повторяющуюся
#: в нескольких пунктах.
HEADER_REPEAT_RATIO = 0.5

#: Номер страницы в разных начертаниях: «15», «- 15 -», «Стр. 15 из 400».
_PAGE_NUMBER = re.compile(
    r"^\s*(?:[-–—]\s*)?(?:стр\.?|страница|page)?\s*\d{1,4}"
    r"(?:\s*(?:из|of|/)\s*\d{1,4})?\s*(?:[-–—])?\s*$", re.IGNORECASE)

#: Перенос слова в конце строки: «неверо-\nятное».
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


class PdfError(ParseError):
    """Ошибка чтения PDF. Наследует ParseError — вызывающий код ловит
    один тип для всех проблем разбора файлов."""


@dataclass
class PdfPage:
    number: int
    text: str = ""

    @property
    def chars(self) -> int:
        return len(self.text.strip())


@dataclass
class PdfDocument:
    path: str
    pages: list[PdfPage] = field(default_factory=list)
    engine: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def total_chars(self) -> int:
        return sum(p.chars for p in self.pages)

    def looks_scanned(self) -> bool:
        if not self.pages:
            return True
        return self.total_chars / len(self.pages) < SCAN_CHARS_PER_PAGE

    def summary(self) -> dict[str, Any]:
        return {"pages": len(self.pages), "chars": self.total_chars,
                "engine": self.engine, "scanned": self.looks_scanned(),
                "title": self.meta.get("title", "")}


# --- движки ---------------------------------------------------------------
def _read_pymupdf(path: Path) -> tuple[list[PdfPage], dict[str, Any]] | None:
    try:
        import fitz                                       # type: ignore
    except ImportError:
        return None
    try:
        doc = fitz.open(str(path))
    except Exception as exc:                              # noqa: BLE001
        raise PdfError(f"{path.name}: не открывается как PDF — {exc}") from exc
    try:
        # sort=True укладывает блоки в порядок чтения. Без него текст из
        # двухколоночной вёрстки перемешивается построчно между колонками.
        pages = [PdfPage(i + 1, page.get_text("text", sort=True) or "")
                 for i, page in enumerate(doc)]
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    finally:
        doc.close()
    return pages, meta


def _read_pypdf(path: Path) -> tuple[list[PdfPage], dict[str, Any]] | None:
    try:
        from pypdf import PdfReader                       # type: ignore
    except ImportError:
        return None
    # pypdf сыплет в лог сотнями предупреждений о внутренностях шрифтов
    # («Skipping broken line…»), которые пользователю ничего не говорят и
    # полностью скрывают полезный вывод команды. Глушим на время чтения:
    # реальные ошибки всё равно приходят исключениями, а не логом.
    import logging
    noisy = logging.getLogger("pypdf")
    prev_level, prev_propagate = noisy.level, noisy.propagate
    noisy.setLevel(logging.ERROR)
    noisy.propagate = False
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Пустой пароль открывает документы «только для печати».
            try:
                reader.decrypt("")
            except Exception:                             # noqa: BLE001
                raise PdfError(
                    f"{path.name}: PDF защищён паролем. Снимите защиту или "
                    "экспортируйте документ без шифрования.")
        pages = [PdfPage(i + 1, (page.extract_text() or ""))
                 for i, page in enumerate(reader.pages)]
        meta = {}
        if reader.metadata:
            meta = {k.lstrip("/").lower(): str(v)
                    for k, v in reader.metadata.items() if v}
    except PdfError:
        raise
    except Exception as exc:                              # noqa: BLE001
        raise PdfError(f"{path.name}: не читается как PDF — {exc}") from exc
    finally:
        noisy.setLevel(prev_level)
        noisy.propagate = prev_propagate
    return pages, meta


def available_engines() -> list[str]:
    out = []
    try:
        import fitz                                       # noqa: F401
        out.append("pymupdf")
    except ImportError:
        pass
    try:
        import pypdf                                      # noqa: F401
        out.append("pypdf")
    except ImportError:
        pass
    return out


def read_pdf(path: str | Path, *, engine: str = "") -> PdfDocument:
    """Прочитать PDF. engine: '' (авто) | 'pymupdf' | 'pypdf'."""
    p = Path(path)
    if not p.exists():
        raise PdfError(f"Файл не найден: {p}")
    if p.suffix.lower() != ".pdf":
        raise PdfError(f"{p.name}: ожидался файл .pdf")

    readers = {"pymupdf": _read_pymupdf, "pypdf": _read_pypdf}
    order = [engine] if engine else ["pymupdf", "pypdf"]
    if engine and engine not in readers:
        raise PdfError(
            f"Неизвестный движок {engine!r}. Доступны: pymupdf, pypdf")

    for name in order:
        result = readers[name](p)
        if result is None:
            continue
        pages, meta = result
        return PdfDocument(str(p), pages, engine=name, meta=meta)

    raise PdfError(
        "Для чтения PDF нужна одна из библиотек (PDF — бинарный формат, "
        "стандартной библиотекой его не разобрать):\n"
        "    pip install pymupdf      # быстрее, лучше держит вёрстку\n"
        "    pip install pypdf        # чистый Python, ставится где угодно\n"
        "Импорт Word/Excel и работа агентов от этого не зависят.")


# --- очистка --------------------------------------------------------------
def find_repeating_lines(pages: list[PdfPage], *,
                         ratio: float = HEADER_REPEAT_RATIO) -> set[str]:
    """Строки-колонтитулы: те, что повторяются на большинстве страниц."""
    if len(pages) < 3:
        # На двух страницах «повторяется» что угодно; выбрасывать строки
        # по такой статистике опаснее, чем оставить колонтитул.
        return set()
    counts: dict[str, int] = {}
    for page in pages:
        seen = set()
        for line in page.text.splitlines():
            norm = re.sub(r"\s+", " ", line).strip()
            if len(norm) < 4 or norm in seen:
                continue
            seen.add(norm)
            counts[norm] = counts.get(norm, 0) + 1
    threshold = max(2, int(len(pages) * ratio))
    return {line for line, n in counts.items() if n >= threshold}


def clean_text(doc: PdfDocument, *, drop_headers: bool = True) -> str:
    """Убрать колонтитулы и номера страниц, склеить переносы и абзацы."""
    headers = find_repeating_lines(doc.pages) if drop_headers else set()
    chunks: list[str] = []
    for page in doc.pages:
        kept: list[str] = []
        for line in page.text.splitlines():
            norm = re.sub(r"\s+", " ", line).strip()
            if not norm:
                kept.append("")
                continue
            if norm in headers:
                continue
            if _PAGE_NUMBER.match(norm):
                continue
            kept.append(norm)
        chunks.append("\n".join(kept))

    text = "\n".join(chunks)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)          # неверо-\nятное
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def paragraphs(text: str) -> list[str]:
    """Разбить очищенный текст на абзацы.

    Границей абзаца считается пустая строка ЛИБО строка, начинающаяся с
    маркера пункта (цифра с точкой, «(a)», «Раздел»). Иначе абзацы
    правил склеиваются в одну простыню, потому что PDF редко оставляет
    пустые строки между ними.
    """
    out: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = " ".join(current).strip()
            if joined:
                out.append(re.sub(r"\s{2,}", " ", joined))
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if _looks_like_new_block(stripped) and current:
            flush()
        current.append(stripped)
    flush()
    return out


_BLOCK_START = re.compile(
    r"^(?:\d{1,3}[.\-]\d{1,4}[A-ZА-Я\d.\-]*\s|\(?[a-zа-я]\)\s|\(\d+\)\s|"
    r"[-–—•]\s|Раздел\s|Приложение\s|Глава\s|Дополнение\s)", re.IGNORECASE)


def _looks_like_new_block(line: str) -> bool:
    return bool(_BLOCK_START.match(line))
