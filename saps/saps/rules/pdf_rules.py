"""Извлечение пунктов авиационных правил из PDF-справочника.

ЗАДАЧА. На входе — PDF вида «Авиационные правила. Часть 25» на сотни
страниц. На выходе — записи справочника: код пункта (25.1309),
заголовок, текст. Дальше их подхватывает обычный загрузчик правил, и
Агент-Классификатор начинает подбирать пункты по смыслу.

КАК РАСПОЗНАЁТСЯ ПУНКТ. По строке-заголовку вида «25.1309 Оборудование,
системы и установки». Признаки: номер в начале строки, точка внутри
номера, дальше текст с заглавной буквы. Всё, что идёт до следующего
такого заголовка, считается телом пункта.

ПОЧЕМУ НЕ LLM. Разметка справочника — механическая работа: номера
пунктов заданы жёстким форматом. Модель здесь добавила бы стоимость,
недетерминированность и риск «улучшить» формулировку нормативного
документа. Текст правил обязан попасть в базу дословно.

ЧТО ОТСЕИВАЕТСЯ И ПОЧЕМУ. Оглавление — главный источник мусора: там те
же номера пунктов, но вместо текста — отточия и номер страницы
(«25.1309 Оборудование, системы .......... 512»). Такие записи
распознаются и выбрасываются: попав в справочник, они дали бы
Классификатору пустые пункты, которые он честно предлагал бы инженеру.

НОМЕР НАБОРА ПРАВИЛ (АП-25) берётся из имени файла, метаданных или
текста первой страницы; если определить не удалось — система просит
указать его явно, а не придумывает.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingest.pdf import PdfDocument, clean_text, read_pdf

#: Заголовок пункта: «25.1309 Оборудование...», «21.A.15 Заявка...»,
#: «25.571(a) Оценка...». Номер обязан содержать точку — иначе под
#: правило попадут нумерованные абзацы вида «15 Общие положения».
CLAUSE_HEADING = re.compile(
    r"^(?P<code>\d{1,3}(?:\.[A-ZА-Я]\.?)?\.\d{1,4}[a-zA-Zа-яА-Я\d.\-]*)"
    r"\s*[.\)]?\s+(?P<title>\S.*)$")

#: Строка оглавления: заголовок, оканчивающийся отточием и/или номером
#: страницы. Это не пункт, а ссылка на него.
TOC_LINE = re.compile(r"(?:\.{4,}|\s{4,}|…)\s*\d{1,4}\s*$")

#: Набор правил в тексте/имени файла: «АП-25», «AP-25», «Часть 25», «CS-25».
RULESET_PATTERNS = [
    re.compile(r"\b(АП[-\s]?\d{1,3}[А-Я]?)\b", re.IGNORECASE),
    re.compile(r"\b(ФАП[-\s]?\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(CS[-\s]?\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(FAR[-\s]?\d{1,3})\b", re.IGNORECASE),
    re.compile(r"[Чч]асть\s+(\d{1,3})"),
]

#: Заголовки разделов — сохраняются как контекст пункта.
SECTION_HEADING = re.compile(
    r"^(Раздел|Подраздел|Глава|Приложение|Дополнение)\s+([A-ZА-Я\d][^.]{0,80})$",
    re.IGNORECASE)

#: Минимальная длина тела пункта. Короче — почти всегда обрывок
#: оглавления или заголовок без содержания.
MIN_BODY_CHARS = 40


@dataclass
class ExtractedClause:
    clause: str
    title: str = ""
    text: str = ""
    section: str = ""
    page: int = 0

    def keywords(self) -> str:
        """Ключевые слова для keyword-фолбэка классификатора.

        Берём значимые слова заголовка и начала текста: они работают,
        пока у пункта нет эмбеддинга (сразу после загрузки).
        """
        source = f"{self.title} {self.text[:400]}"
        words = re.findall(r"[а-яёa-z]{4,}", source.lower())
        seen: list[str] = []
        for w in words:
            if w not in seen and w not in _STOPWORDS:
                seen.append(w)
            if len(seen) >= 20:
                break
        return " ".join(seen)

    def to_dict(self) -> dict[str, Any]:
        return {"clause": self.clause, "title": self.title, "text": self.text,
                "keywords": self.keywords(),
                "meta": {"section": self.section, "page": self.page}}


_STOPWORDS = {
    "должен", "должна", "должно", "должны", "быть", "этот", "того", "если",
    "который", "которые", "которая", "также", "может", "могут", "было",
    "если", "либо", "иные", "иных", "всех", "этих", "либо", "чтобы", "как",
    "при", "для", "или", "the", "and", "must", "shall", "with", "that",
}


@dataclass
class ExtractionResult:
    ruleset: str
    clauses: list[ExtractedClause] = field(default_factory=list)
    source: str = ""
    pages: int = 0
    skipped_toc: int = 0
    skipped_short: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_ruleset_dict(self, title: str = "") -> dict[str, Any]:
        """Формат, который понимает saps.rules.loader."""
        return {
            "ruleset": self.ruleset,
            "title": title or f"Импортировано из {Path(self.source).name}",
            "note": f"Извлечено из PDF: {Path(self.source).name}, "
                    f"страниц {self.pages}, пунктов {len(self.clauses)}",
            "clauses": [c.to_dict() for c in self.clauses],
        }

    def summary(self) -> dict[str, Any]:
        return {"ruleset": self.ruleset, "clauses": len(self.clauses),
                "pages": self.pages, "skipped_toc": self.skipped_toc,
                "skipped_short": self.skipped_short,
                "warnings": self.warnings}


def detect_ruleset(doc: PdfDocument, path: str | Path) -> str:
    """Определить набор правил: имя файла -> метаданные -> первая страница."""
    name = Path(path).stem
    head = "\n".join(p.text for p in doc.pages[:3])
    title = str(doc.meta.get("title", ""))

    for source in (name, title, head):
        for pattern in RULESET_PATTERNS:
            m = pattern.search(source or "")
            if not m:
                continue
            value = m.group(1).strip()
            if pattern.pattern.startswith("[Чч]асть"):
                return f"АП-{value}"
            return re.sub(r"[-\s]+", "-", value.upper())
    return ""


def extract_clauses(text: str) -> tuple[list[ExtractedClause], int, int]:
    """Разобрать очищенный текст на пункты.

    Возвращает (пункты, пропущено_оглавления, пропущено_коротких).
    """
    clauses: list[ExtractedClause] = []
    skipped_toc = 0
    section = ""
    current: ExtractedClause | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current.text = re.sub(r"\s{2,}", " ", " ".join(body)).strip()
        if not current.text and len(current.title) >= MIN_BODY_CHARS:
            # Пункт целиком уместился в одну строку: «25.200 Настоящий
            # пункт содержит…». Весь текст оказался в заголовке, тело
            # пустое — и такой пункт молча выбрасывался фильтром коротких.
            # Молчаливая потеря пункта норматива недопустима: переносим
            # текст в тело, а заголовком оставляем первое предложение.
            current.text = current.title
            head = re.split(r"(?<=[.!?])\s+", current.title, maxsplit=1)[0]
            current.title = head[:120].rstrip(" .,;:")
        clauses.append(current)
        current = None
        body.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        sec = SECTION_HEADING.match(stripped)
        if sec:
            flush()
            section = stripped
            continue

        heading = CLAUSE_HEADING.match(stripped)
        if heading:
            if TOC_LINE.search(stripped):
                # Строка оглавления: «25.1309 Оборудование ..... 512».
                skipped_toc += 1
                continue
            flush()
            title = heading.group("title").strip()
            # Заголовок пункта иногда содержит и начало текста в той же
            # строке; отрезаем по первому предложению, остальное — в тело.
            current = ExtractedClause(clause=heading.group("code").rstrip("."),
                                      title=title, section=section)
            continue

        if current is not None:
            body.append(stripped)

    flush()

    # Пункты без содержательного тела почти всегда — остатки оглавления
    # или заголовки-пустышки. Пустой пункт в справочнике вреден: агент
    # будет честно предлагать его инженеру.
    full = [c for c in clauses if len(c.text) >= MIN_BODY_CHARS]
    skipped_short = len(clauses) - len(full)
    return full, skipped_toc, skipped_short


def extract_from_pdf(path: str | Path, *, ruleset: str = "",
                     engine: str = "") -> ExtractionResult:
    """Полный путь: PDF -> очистка -> пункты справочника."""
    doc = read_pdf(path, engine=engine)
    if doc.looks_scanned():
        raise _scan_error(doc, path)

    text = clean_text(doc)
    clauses, skipped_toc, skipped_short = extract_clauses(text)

    detected = ruleset or detect_ruleset(doc, path)
    warnings: list[str] = []
    if not detected:
        raise ValueError(
            f"Не удалось определить набор правил для {Path(path).name}. "
            "Укажите его явно: --ruleset АП-25 (имя используется как "
            "идентификатор в базе и в отчётах).")
    if not clauses:
        warnings.append(
            "Не найдено ни одного пункта. Проверьте, что в документе "
            "пункты нумерованы в формате «25.1309 Название», и при "
            "необходимости попробуйте другой движок: --engine pypdf.")
    if skipped_toc:
        warnings.append(
            f"Пропущено строк оглавления: {skipped_toc} (это ссылки на "
            "пункты, а не сами пункты).")
    if skipped_short:
        warnings.append(
            f"Пропущено пунктов без содержательного текста: {skipped_short}.")

    return ExtractionResult(ruleset=detected, clauses=clauses,
                            source=str(path), pages=len(doc.pages),
                            skipped_toc=skipped_toc,
                            skipped_short=skipped_short, warnings=warnings)


def _scan_error(doc: PdfDocument, path: str | Path) -> Exception:
    from ..ingest.pdf import PdfError
    return PdfError(
        f"{Path(path).name}: в документе почти нет текстового слоя "
        f"({doc.total_chars} символов на {len(doc.pages)} страниц) — похоже, "
        "это скан. САПС не распознаёт изображения: нужен OCR. Варианты: "
        "прогнать файл через ABBYY FineReader / OCRmyPDF и загрузить "
        "результат, либо взять исходный текстовый PDF у поставщика "
        "документа.")
