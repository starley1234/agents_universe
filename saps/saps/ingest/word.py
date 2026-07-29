"""Парсер Word (.docx) — режим Б слоя импорта (ТЗ п.3.1).

ПОЧЕМУ БЕЗ python-docx. .docx — это zip с XML внутри; всё, что нужно
САПС (иерархия заголовков, таблицы, текст), лежит в word/document.xml и
достаётся стандартными zipfile + ElementTree. Отказ от зависимости
здесь не ради аскезы: система ставится в конструкторском бюро, где
установка пакетов из интернета часто согласуется неделями, а импорт
требований нужен в первый день. Цена — около сотни строк разбора XML,
и они всё равно понадобились бы: python-docx не умеет ни outlineLvl из
прямого форматирования, ни нумерацию списков в том виде, в каком её
используют в ТЗ на изделие.

ЧТО ДЕЛАЕТ ПАРСЕР (ТЗ п.3.1, режим Б):
  1. Распознаёт ИЕРАРХИЮ ЗАГОЛОВКОВ — по стилю (Heading N, Заголовок N)
     и по outlineLvl. Строит section_path вида «3 > 3.1 > 3.1.2»,
     чтобы инженер видел, откуда требование взято.
  2. Извлекает ТАБЛИЦЫ АТРИБУТОВ. Две формы: «ключ | значение»
     построчно (вертикальная) и таблица с шапкой (горизонтальная), где
     каждая строка — отдельное требование.
  3. Идентифицирует НОМЕРА ТРЕБОВАНИЙ — [REQ-123] и близкие формы.

ГЛАВНОЕ ПРАВИЛО: ПАРСЕР НЕ ДОДУМЫВАЕТ. Он не решает, требование перед
ним или пояснение; он извлекает всё, что похоже на требование, и
помечает уверенность. Разделение «требование/не требование» — работа
инженера в мастере подготовки данных, потому что ошибка парсера,
принятая за истину, попадёт в сертификационный базис.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


class ParseError(Exception):
    """Ожидаемая ошибка разбора: не тот файл, битый архив."""


#: Идентификатор требования. Порядок важен: сначала самые явные формы.
#: Скобочная форма [REQ-123] названа в ТЗ прямо; остальные встречаются в
#: реальных выгрузках Teamcenter и в ТЗ, свёрстанных вручную.
REQ_PATTERNS = [
    re.compile(r"\[\s*(?P<id>[A-ZА-Я][A-ZА-Я0-9]{1,15}[-_]\d{1,6}"
               r"(?:[.-]\d{1,4})*)\s*\]"),
    re.compile(r"\b(?P<id>REQ[-_]\d{1,6}(?:[.-]\d{1,4})*)\b", re.IGNORECASE),
    re.compile(r"\b(?P<id>ТРБ[-_]\d{1,6}(?:[.-]\d{1,4})*)\b", re.IGNORECASE),
]

#: Слова, по которым абзац похож на требование. Используются ТОЛЬКО для
#: подсказки уверенности, а не для отбрасывания текста.
MODAL_WORDS = ("должен", "должна", "должно", "должны", "обязан", "обязана",
               "обязано", "необходимо", "требуется", "не допускается",
               "shall", "must")

#: Названия колонок таблицы атрибутов, которые САПС понимает.
#: Ключ — нормализованное имя, значение — поле требования.
ATTR_COLUMNS = {
    "идентификатор": "external_id", "id": "external_id",
    "ид": "external_id", "номер": "external_id", "код": "external_id",
    "обозначение": "external_id", "requirement id": "external_id",
    "требование": "text", "текст": "text", "формулировка": "text",
    "содержание": "text", "text": "text", "description": "text",
    "наименование": "title", "название": "title", "заголовок": "title",
    "title": "title",
    "владелец": "owner", "ответственный": "owner", "исполнитель": "owner",
    "owner": "owner",
    "узел": "node", "изделие": "node", "агрегат": "node", "система": "node",
    "статус": "status", "status": "status",
    "moc": "moc", "мпс": "moc", "метод подтверждения": "moc",
    "пункт ап": "clause", "пункт": "clause", "ап": "clause",
    "clause": "clause", "правило": "clause",
}

_HEADING_STYLE = re.compile(r"^(?:heading|заголовок)\s*(\d+)$", re.IGNORECASE)


@dataclass
class Block:
    """Единица разбора: абзац, заголовок или таблица."""
    kind: str                       # heading | paragraph | table
    text: str = ""
    level: int = 0                  # для heading
    rows: list[list[str]] = field(default_factory=list)   # для table


@dataclass
class ParsedRequirement:
    """Кандидат в требования, извлечённый из документа."""
    external_id: str = ""
    title: str = ""
    text: str = ""
    section_path: str = ""
    owner: str = ""
    node: str = ""
    status: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    #: Откуда взято: paragraph | table_row | table_kv — нужно инженеру,
    #: чтобы понимать, почему разбор выглядит именно так.
    origin: str = "paragraph"
    ord: int = 0
    #: Уверенность парсера [0..1]: есть ли явный идентификатор, модальный
    #: глагол, достаточная длина. НЕ основание для автоматического
    #: принятия — только для сортировки в мастере подготовки данных.
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_staging(self) -> dict[str, Any]:
        return {
            "ord": self.ord,
            "external_id": self.external_id,
            "section_path": self.section_path,
            "raw_text": self.text,
            "raw": {
                "title": self.title, "owner": self.owner, "node": self.node,
                "status": self.status, "attributes": self.attributes,
                "origin": self.origin, "confidence": round(self.confidence, 3),
                "notes": self.notes,
            },
        }


# --- низкий уровень: чтение docx ------------------------------------------
def _text_of(el: ET.Element) -> str:
    """Собрать текст элемента, уважая разрывы строк и табуляции."""
    parts: list[str] = []
    for node in el.iter():
        tag = node.tag.split("}")[-1]
        if tag == "t":
            parts.append(node.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


def _heading_level(par: ET.Element) -> int:
    """Уровень заголовка: 0 — обычный абзац.

    Смотрим и стиль, и outlineLvl: в выгрузках Teamcenter заголовки
    часто оформлены прямым форматированием без стиля Heading, и разбор
    только по стилю теряет всю иерархию документа.
    """
    ppr = par.find(f"{{{W}}}pPr")
    if ppr is None:
        return 0
    style = ppr.find(f"{{{W}}}pStyle")
    if style is not None:
        val = (style.get(f"{{{W}}}val") or "").strip()
        m = _HEADING_STYLE.match(val)
        if m:
            return int(m.group(1))
        # Стили вида Heading1 / Заголовок1 без пробела.
        m = re.match(r"^(?:heading|заголовок)(\d+)$", val, re.IGNORECASE)
        if m:
            return int(m.group(1))
    outline = ppr.find(f"{{{W}}}outlineLvl")
    if outline is not None:
        try:
            return int(outline.get(f"{{{W}}}val", "9")) + 1
        except ValueError:
            return 0
    return 0


def _iter_blocks(body: ET.Element) -> Iterator[Block]:
    for el in body:
        tag = el.tag.split("}")[-1]
        if tag == "p":
            text = _text_of(el)
            if not text:
                continue
            level = _heading_level(el)
            yield Block("heading" if level else "paragraph", text=text,
                        level=level)
        elif tag == "tbl":
            rows: list[list[str]] = []
            for tr in el.findall(f"{{{W}}}tr"):
                cells = [_text_of(tc) for tc in tr.findall(f"{{{W}}}tc")]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if rows:
                yield Block("table", rows=rows)


def read_blocks(path: str | Path) -> list[Block]:
    """Прочитать .docx в последовательность блоков."""
    p = Path(path)
    if not p.exists():
        raise ParseError(f"Файл не найден: {p}")
    try:
        with zipfile.ZipFile(p) as z:
            names = set(z.namelist())
            if "word/document.xml" not in names:
                raise ParseError(
                    f"{p.name}: это не документ Word (.docx). Внутри нет "
                    "word/document.xml. Если файл в формате .doc — "
                    "пересохраните его как .docx.")
            data = z.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ParseError(
            f"{p.name}: не читается как .docx (повреждён или это .doc). "
            "Пересохраните файл из Word в формате .docx.") from exc
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError(f"{p.name}: битый XML внутри документа — {exc}") from exc
    body = root.find(f"{{{W}}}body")
    if body is None:
        raise ParseError(f"{p.name}: в документе нет тела (w:body)")
    return list(_iter_blocks(body))


# --- распознавание -------------------------------------------------------
def find_requirement_id(text: str) -> str:
    """Найти идентификатор требования в тексте. Пусто — не найден."""
    for pattern in REQ_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group("id").upper().replace("_", "-")
    return ""


def _has_modal(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in MODAL_WORDS)


def _confidence(text: str, has_id: bool, origin: str) -> tuple[float, list[str]]:
    """Насколько блок похож на требование. Прозрачно и без магии."""
    notes: list[str] = []
    score = 0.0
    if has_id:
        score += 0.5
    else:
        notes.append("нет явного идентификатора требования")
    if _has_modal(text):
        score += 0.3
    else:
        notes.append("нет модального глагола («должен», «shall»)")
    if len(text) >= 40:
        score += 0.2
    else:
        notes.append("очень короткий текст")
    if origin.startswith("table"):
        # Табличная строка структурирована — это само по себе признак
        # осознанного оформления требования, а не случайного абзаца.
        score = min(1.0, score + 0.1)
    return min(1.0, score), notes


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", cell).strip().lower().rstrip(":*")


def _table_kind(rows: list[list[str]]) -> str:
    """Определить форму таблицы: с шапкой (rows) или «ключ-значение» (kv)."""
    if not rows:
        return "unknown"
    header = [_norm_header(c) for c in rows[0]]
    known = sum(1 for h in header if h in ATTR_COLUMNS)
    if len(rows) > 1 and known >= 2:
        return "rows"
    # Вертикальная таблица атрибутов: два столбца, левый — имена полей.
    if all(len(r) == 2 for r in rows):
        keys = sum(1 for r in rows if _norm_header(r[0]) in ATTR_COLUMNS)
        if keys >= 1:
            return "kv"
    return "unknown"


def _apply_field(req: ParsedRequirement, field_name: str, value: str) -> None:
    value = value.strip()
    if not value:
        return
    if field_name == "external_id":
        req.external_id = find_requirement_id(value) or value.strip().upper()
    elif field_name == "text":
        req.text = value
    elif field_name == "title":
        req.title = value
    elif field_name == "owner":
        req.owner = value
    elif field_name == "node":
        req.node = value
    elif field_name == "status":
        req.status = value
    else:
        req.attributes[field_name] = value


def parse_blocks(blocks: list[Block]) -> list[ParsedRequirement]:
    """Превратить блоки документа в кандидатов-требования."""
    out: list[ParsedRequirement] = []
    # Стек заголовков: индекс = уровень-1. Нужен для section_path.
    heading_stack: list[str] = []
    counter = 0

    for block in blocks:
        if block.kind == "heading":
            level = max(1, block.level)
            del heading_stack[level - 1:]
            while len(heading_stack) < level - 1:
                heading_stack.append("")
            heading_stack.append(block.text)
            continue

        section_path = " > ".join(h for h in heading_stack if h)

        if block.kind == "paragraph":
            text = block.text
            req_id = find_requirement_id(text)
            # Абзац без идентификатора и без модального глагола — почти
            # наверняка пояснение. Пропускаем, но только при отсутствии
            # ОБОИХ признаков: иначе легко потерять требование, у которого
            # номер вынесен в отдельную колонку/заголовок.
            if not req_id and not _has_modal(text):
                continue
            confidence, notes = _confidence(text, bool(req_id), "paragraph")
            counter += 1
            out.append(ParsedRequirement(
                external_id=req_id,
                text=_strip_id(text, req_id),
                section_path=section_path, origin="paragraph",
                ord=counter, confidence=confidence, notes=notes,
                title=heading_stack[-1] if heading_stack else ""))
            continue

        # --- таблицы ---
        kind = _table_kind(block.rows)
        if kind == "rows":
            header = [_norm_header(c) for c in block.rows[0]]
            for row in block.rows[1:]:
                req = ParsedRequirement(section_path=section_path,
                                        origin="table_row")
                for i, cell in enumerate(row):
                    if i >= len(header):
                        break
                    field_name = ATTR_COLUMNS.get(header[i])
                    if field_name:
                        _apply_field(req, field_name, cell)
                    elif header[i] and cell.strip():
                        req.attributes[header[i]] = cell.strip()
                if not req.text and not req.external_id:
                    continue
                if not req.external_id:
                    req.external_id = find_requirement_id(req.text)
                counter += 1
                req.ord = counter
                req.confidence, req.notes = _confidence(
                    req.text, bool(req.external_id), "table_row")
                out.append(req)
        elif kind == "kv":
            req = ParsedRequirement(section_path=section_path,
                                    origin="table_kv")
            for row in block.rows:
                key = _norm_header(row[0])
                field_name = ATTR_COLUMNS.get(key)
                if field_name:
                    _apply_field(req, field_name, row[1])
                elif key and row[1].strip():
                    req.attributes[key] = row[1].strip()
            if req.text or req.external_id:
                counter += 1
                req.ord = counter
                req.confidence, req.notes = _confidence(
                    req.text, bool(req.external_id), "table_kv")
                out.append(req)
        else:
            # Незнакомая таблица: сохраняем как есть в атрибуты, если в
            # ней нашёлся идентификатор требования. Терять данные нельзя,
            # но и выдумывать структуру — тоже.
            flat = "\n".join(" | ".join(r) for r in block.rows)
            req_id = find_requirement_id(flat)
            if not req_id:
                continue
            counter += 1
            confidence, notes = _confidence(flat, True, "table")
            notes.append("структура таблицы не распознана — сохранена целиком")
            out.append(ParsedRequirement(
                external_id=req_id, text=flat, section_path=section_path,
                origin="table_raw", ord=counter, confidence=confidence,
                notes=notes, attributes={"rows": block.rows}))
    return out


def _strip_id(text: str, req_id: str) -> str:
    """Убрать [REQ-123] из начала текста — он уже в отдельном поле."""
    if not req_id:
        return text
    cleaned = re.sub(r"^\s*\[\s*" + re.escape(req_id) + r"\s*\]\s*[:.\-—]?\s*",
                     "", text, flags=re.IGNORECASE)
    if cleaned != text:
        return cleaned.strip()
    return re.sub(r"^\s*" + re.escape(req_id) + r"\s*[:.\-—]\s*", "", text,
                  flags=re.IGNORECASE).strip()


def parse_docx(path: str | Path) -> list[ParsedRequirement]:
    """Полный разбор файла: блоки -> кандидаты в требования."""
    return parse_blocks(read_blocks(path))


def file_hash(path: str | Path) -> str:
    """sha256 файла — чтобы распознать повторный импорт того же документа."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def summarize(requirements: list[ParsedRequirement]) -> dict[str, Any]:
    """Сводка разбора для отчёта об импорте."""
    total = len(requirements)
    with_id = sum(1 for r in requirements if r.external_id)
    origins: dict[str, int] = {}
    for r in requirements:
        origins[r.origin] = origins.get(r.origin, 0) + 1
    confident = sum(1 for r in requirements if r.confidence >= 0.7)
    return {"total": total, "with_id": with_id, "without_id": total - with_id,
            "confident": confident, "origins": origins}
