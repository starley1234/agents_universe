"""Автозагрузка документа: одна команда — и система всё сделала сама.

ЗАЧЕМ. Обычный путь в САПС состоит из шести шагов: определить тип
документа, разобрать, положить в staging, перенести в production,
посчитать эмбеддинги, запустить агентов. Каждый шаг оправдан и нужен
инженеру по отдельности — но когда человек первый раз приносит PDF
справочника или ТЗ, он хочет одного: «загрузи и разберись сам».

Этот модуль — тонкая обёртка НАД существующими шагами, а не их замена.
Он ничего не делает в обход: та же валидация, та же прослеживаемость,
те же предложения агентов на утверждение человеком. Просто вызывает всё
по порядку и внятно отчитывается, что произошло на каждом шаге.

ЧТО РАСПОЗНАЁТСЯ АВТОМАТИЧЕСКИ:
  * ТИП ФАЙЛА по расширению (.pdf/.docx/.xlsx);
  * НАЗНАЧЕНИЕ документа — справочник авиационных правил или документ с
    требованиями. Различие принципиальное: справочник идёт в таблицу
    rule_clause (это НОРМАТИВ, его не редактируют), требования — в
    staging и дальше в production (это РАБОТА, её проверяет инженер).
    Перепутать их значит либо засорить справочник требованиями КБ, либо
    завести пункты АП как собственные требования и начать их «улучшать»;
  * НАБОР ПРАВИЛ (АП-25) — из имени файла, метаданных или текста.

ЧЕГО АВТОЗАГРУЗКА НЕ ДЕЛАЕТ. Не утверждает требования, не подтверждает
связи с пунктами АП, не проставляет соответствие. Всё это по-прежнему
решение инженера — автоматизирована только механическая часть.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..db.store import Store
from .pdf import PdfError, clean_text, read_pdf
from .pipeline import import_records, promote_all
from .word import ParsedRequirement, ParseError, find_requirement_id

#: Признаки того, что PDF — нормативный справочник, а не ТЗ.
#: Считаются по первым страницам: заголовок документа и оглавление.
RULEBOOK_MARKERS = (
    "авиационные правила", "нормы лётной годности", "нормы летной годности",
    "федеральные авиационные правила", "certification specifications",
    "airworthiness standards", "part 21", "part 25", "часть 21", "часть 25",
)

#: Признаки документа с требованиями разработчика.
REQUIREMENTS_MARKERS = (
    "техническое задание", "тз на", "требования к", "спецификация требований",
    "перечень требований", "requirement", "требование",
)


@dataclass
class AutoloadStep:
    name: str
    ok: bool = True
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"  {mark} {self.name}: {self.detail}"


@dataclass
class AutoloadResult:
    path: str
    kind: str = ""              # rulebook | requirements
    file_type: str = ""         # pdf | word | excel
    steps: list[AutoloadStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    document_id: int | None = None
    ruleset: str = ""
    clauses_loaded: int = 0
    requirements_created: int = 0
    suggestions: int = 0

    def add(self, name: str, detail: str, *, ok: bool = True,
            **data: Any) -> AutoloadStep:
        step = AutoloadStep(name, ok, detail, data)
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "kind": self.kind, "file_type": self.file_type,
            "ok": self.ok,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail,
                       **s.data} for s in self.steps],
            "warnings": self.warnings,
            "document_id": self.document_id, "ruleset": self.ruleset,
            "clauses_loaded": self.clauses_loaded,
            "requirements_created": self.requirements_created,
            "suggestions": self.suggestions,
        }

    def report(self) -> str:
        out = [f"Документ: {Path(self.path).name}",
               f"Тип: {self.file_type or '?'}, назначение: "
               f"{_kind_label(self.kind)}", ""]
        out += [s.line() for s in self.steps]
        if self.warnings:
            out.append("")
            out += [f"  ⚠ {w}" for w in self.warnings]
        return "\n".join(out)


def _kind_label(kind: str) -> str:
    return {"rulebook": "справочник авиационных правил",
            "requirements": "документ с требованиями"}.get(kind, "не определено")


def detect_file_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "word"
    if suffix in (".xlsx", ".xlsm"):
        return "excel"
    if suffix in (".doc", ".xls"):
        raise ParseError(
            f"Формат {suffix} (бинарный, Office 97-2003) не поддерживается. "
            f"Пересохраните файл как {'.docx' if suffix == '.doc' else '.xlsx'}.")
    raise ParseError(
        f"Неизвестное расширение {suffix!r}. САПС загружает .pdf, .docx, .xlsx.")


def detect_kind(text: str, path: str | Path) -> tuple[str, str]:
    """Справочник или требования. Возвращает (тип, обоснование).

    Решение принимается по трём признакам, и каждый объясняется в
    отчёте: инженер должен видеть, почему система решила так, и иметь
    возможность переопределить (--as rulebook|requirements).
    """
    haystack = f"{Path(path).stem} {text[:4000]}".lower()

    rule_hits = [m for m in RULEBOOK_MARKERS if m in haystack]
    req_ids = len(set(re.findall(r"\[?(?:REQ|ТРБ)[-_]\d+", text[:20000],
                                 re.IGNORECASE)))
    # Нумерация пунктов вида 25.1309 — самый сильный признак норматива.
    clause_numbers = len(set(re.findall(r"\b\d{2}\.\d{3,4}\b", text[:20000])))

    if req_ids >= 3:
        return "requirements", (
            f"найдено идентификаторов требований: {req_ids} "
            "([REQ-…]/[ТРБ-…]) — это рабочий документ, а не норматив")
    if clause_numbers >= 3 and rule_hits:
        return "rulebook", (
            f"нумерация пунктов ({clause_numbers} шт. вида 25.1309) и "
            f"признаки норматива: {', '.join(rule_hits[:2])}")
    if clause_numbers >= 5:
        # Пять и больше номеров вида 25.1309 в документе без единого
        # [REQ-…] — это норматив. У ТЗ разработчика нумерация своя.
        return "rulebook", (
            f"нумерация пунктов ({clause_numbers} шт. вида 25.1309) "
            "характерна для нормативного документа")
    if rule_hits:
        return "rulebook", f"признаки норматива: {', '.join(rule_hits[:2])}"
    if any(m in haystack for m in REQUIREMENTS_MARKERS):
        return "requirements", "признаки документа с требованиями"
    return "", ("не удалось определить назначение документа — укажите его "
                "явно: --as rulebook или --as requirements")


def autoload(store: Store, cfg: Config, path: str | Path, *,
             actor: str = "", kind: str = "", ruleset: str = "",
             owner: str = "", node: str = "", engine: str = "",
             run_agents: bool = True, promote: bool = True,
             force: bool = False,
             progress: Callable[[str], None] | None = None) -> AutoloadResult:
    """Загрузить документ и сделать всё остальное самостоятельно."""
    p = Path(path)
    result = AutoloadResult(path=str(p))
    say = progress or (lambda msg: None)

    if not p.exists():
        raise ParseError(f"Файл не найден: {p}")

    # --- 1. тип файла --------------------------------------------------
    result.file_type = detect_file_type(p)
    result.add("Тип файла", f"{result.file_type} ({p.stat().st_size // 1024} КБ)")
    say(f"Тип файла: {result.file_type}")

    # --- 2. назначение документа ----------------------------------------
    probe_text = _probe_text(p, result.file_type, engine=engine)
    if kind:
        result.kind = kind
        result.add("Назначение", f"{_kind_label(kind)} (указано явно)")
    else:
        detected, reason = detect_kind(probe_text, p)
        if not detected:
            result.add("Назначение", reason, ok=False)
            raise ParseError(reason)
        result.kind = detected
        result.add("Назначение", f"{_kind_label(detected)} — {reason}")
    say(f"Назначение: {_kind_label(result.kind)}")

    embedder = _build_embedder(cfg)

    if result.kind == "rulebook":
        _load_rulebook(store, cfg, p, result, embedder, ruleset=ruleset,
                       engine=engine, actor=actor, say=say)
    else:
        _load_requirements(store, cfg, p, result, embedder, actor=actor,
                           owner=owner, node=node, engine=engine,
                           promote=promote, run_agents=run_agents,
                           force=force, say=say)
    return result


# --- ветка «справочник» ----------------------------------------------------
def _load_rulebook(store: Store, cfg: Config, path: Path,
                   result: AutoloadResult, embedder: Any, *, ruleset: str,
                   engine: str, actor: str, say: Callable[[str], None]) -> None:
    from ..rules.loader import load_ruleset_dict
    from ..rules.pdf_rules import extract_from_pdf

    if result.file_type != "pdf":
        raise ParseError(
            "Справочник правил загружается из PDF или из JSON-файла "
            "(saps rules load <файл.json>). Word/Excel для нормативов не "
            "поддерживаются: их структура слишком разнородна, а ошибка в "
            "тексте норматива дороже ручной подготовки.")

    say("Разбираю PDF…")
    extraction = extract_from_pdf(path, ruleset=ruleset, engine=engine)
    result.ruleset = extraction.ruleset
    result.warnings.extend(extraction.warnings)
    result.add("Разбор PDF",
               f"страниц {extraction.pages}, найдено пунктов "
               f"{len(extraction.clauses)}, набор «{extraction.ruleset}»",
               pages=extraction.pages, clauses=len(extraction.clauses))

    if not extraction.clauses:
        result.steps[-1].ok = False
        return

    say(f"Загружаю {len(extraction.clauses)} пунктов и считаю эмбеддинги…")
    loaded = load_ruleset_dict(store, extraction.to_ruleset_dict(),
                               embedder=embedder, source=str(path))
    result.clauses_loaded = loaded["loaded"]
    result.add("Загрузка в базу",
               f"{loaded['loaded']} пунктов набора «{extraction.ruleset}» "
               f"с эмбеддингами ({_embedder_label(cfg)})")
    store.log(actor or "system", "rules_load", detail=f"PDF: {path.name}",
              data={"ruleset": extraction.ruleset,
                    "clauses": loaded["loaded"]})

    # Справочник изменился — старые требования стоит переклассифицировать.
    total = store.stats().get("requirements", 0)
    if total:
        result.warnings.append(
            f"В базе {total} требований. Чтобы подобрать им пункты нового "
            "справочника, запустите: saps agent classifier")


# --- ветка «требования» ----------------------------------------------------
def _load_requirements(store: Store, cfg: Config, path: Path,
                       result: AutoloadResult, embedder: Any, *, actor: str,
                       owner: str, node: str, engine: str, promote: bool,
                       run_agents: bool, force: bool,
                       say: Callable[[str], None]) -> None:
    from .pipeline import import_file

    say("Разбираю документ…")
    if result.file_type == "pdf":
        records = _requirements_from_pdf(path, engine=engine)
        if not records:
            result.add("Разбор PDF",
                       "в документе не найдено требований (ожидались "
                       "идентификаторы вида [REQ-123] или абзацы с «должен»)",
                       ok=False)
            return
        imported = import_records(store, records, kind="pdf", name=path.name,
                                  uri=str(path.resolve()), actor=actor,
                                  meta={"pages": _page_count(path, engine)})
    else:
        imported = import_file(store, path, actor=actor, allow_duplicate=force)

    result.document_id = imported.document_id
    result.warnings.extend(imported.warnings)
    if imported.duplicate_of and not force:
        result.add("Импорт",
                   f"файл уже загружался (документ #{imported.duplicate_of}) — "
                   "повторный импорт создал бы дубли требований", ok=False)
        return

    s = imported.summary
    result.add("Импорт в staging",
               f"распознано {imported.staged} записей "
               f"(с номером {s.get('with_id', 0)}, "
               f"без номера {s.get('without_id', 0)})",
               staged=imported.staged)

    if not promote:
        result.warnings.append(
            "Записи остались в staging. Проверьте их и перенесите: "
            f"saps promote --doc {imported.document_id}")
        return

    say("Переношу в производственный слой…")
    pr = promote_all(store, imported.document_id, actor=actor,
                     default_owner=owner, default_node=node,
                     embedder=embedder.embed_one)
    counts = pr.to_dict()["counts"]
    result.requirements_created = counts["created"]
    result.add("Перенос в базу",
               f"создано {counts['created']}, обновлено {counts['updated']}, "
               f"пропущено {counts['skipped']}", **counts)
    for skip in pr.skipped[:5]:
        result.warnings.append(
            f"{skip.get('external_id', skip['staging_id'])}: {skip['reason']}")

    if not run_agents or not counts["created"]:
        return

    say("Запускаю агентов…")
    _run_agents(store, cfg, result, embedder, owner=owner, node=node)


def _run_agents(store: Store, cfg: Config, result: AutoloadResult,
                embedder: Any, *, owner: str, node: str) -> None:
    from ..agents import ClassifierAgent, EditorAgent, GapAgent

    editor = EditorAgent(cfg, store).run(owner=owner, node_code=node)
    low = [f for f in editor.findings
           if f.get("score", 1) < cfg.quality_min_score]
    result.add("Агент-Редактор",
               f"проверено {editor.processed}, требований ниже порога "
               f"качества: {len(low)}")

    if store.stats().get("clauses", 0) == 0:
        result.add("Агент-Классификатор",
                   "пропущен: справочник авиационных правил пуст — "
                   "загрузите его (saps load <АП-25.pdf>)", ok=False)
    else:
        cls = ClassifierAgent(cfg, store, embedder).run(owner=owner,
                                                        node_code=node)
        result.add("Агент-Классификатор",
                   f"обработано {cls.processed}, предложено привязок "
                   f"к пунктам АП: {len(cls.suggestions)}")

    gap = GapAgent(cfg, store).run(owner=owner, node_code=node)
    kinds: dict[str, int] = {}
    for f in gap.findings:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    result.add("Агент-Gap-аналитик",
               f"найдено пробелов: {len(gap.findings)}"
               + (f" ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})"
                  if kinds else ""))

    result.suggestions = len(store.list_suggestions(status="pending"))
    if result.suggestions:
        result.warnings.append(
            f"Агенты подготовили {result.suggestions} предложений. Они НЕ "
            "применены: посмотрите и утвердите вручную — saps suggestions "
            "(или вкладка «Предложения агентов» в веб-интерфейсе).")


# --- вспомогательное -------------------------------------------------------
def _probe_text(path: Path, file_type: str, *, engine: str = "") -> str:
    """Немного текста для определения назначения документа.

    ВАЖНО: для PDF берётся СЫРОЙ текст, без clean_text. Очистка
    выбрасывает колонтитулы — а именно в колонтитуле обычно и написано
    «Авиационные правила Часть 25», то есть главный признак норматива.
    Определять назначение по очищенному тексту значит отрезать себе
    основную улику (поймано тестом на документе с оглавлением).
    """
    if file_type == "pdf":
        doc = read_pdf(path, engine=engine)
        if doc.looks_scanned():
            from ..rules.pdf_rules import _scan_error
            raise _scan_error(doc, path)
        return doc.text[:20000]
    if file_type == "word":
        from .word import read_blocks
        blocks = read_blocks(path)
        return "\n".join(b.text for b in blocks if b.text)[:20000]
    from .excel import read_workbook
    wb = read_workbook(path)
    return "\n".join(" ".join(row) for rows in wb.values()
                     for row in rows[:50])[:20000]


def _page_count(path: Path, engine: str = "") -> int:
    try:
        return len(read_pdf(path, engine=engine).pages)
    except PdfError:
        return 0


def _requirements_from_pdf(path: Path, *,
                           engine: str = "") -> list[ParsedRequirement]:
    """Вытащить требования из PDF с ТЗ.

    Требованием считается абзац с идентификатором ([REQ-123]) либо с
    модальным глаголом. Тот же принцип, что в Word-парсере: лучше
    показать инженеру лишнее в staging, чем молча потерять требование.
    """
    from .pdf import paragraphs
    from .word import MODAL_WORDS, _confidence, _strip_id

    doc = read_pdf(path, engine=engine)
    if doc.looks_scanned():
        from ..rules.pdf_rules import _scan_error
        raise _scan_error(doc, path)

    text = clean_text(doc)
    out: list[ParsedRequirement] = []
    counter = 0
    section = ""
    for para in paragraphs(text):
        # Короткая строка без модального глагола — скорее заголовок.
        low = para.lower()
        has_modal = any(w in low for w in MODAL_WORDS)
        req_id = find_requirement_id(para)
        if len(para) < 90 and not has_modal and not req_id:
            section = para[:120]
            continue
        if not req_id and not has_modal:
            continue
        counter += 1
        confidence, notes = _confidence(para, bool(req_id), "paragraph")
        out.append(ParsedRequirement(
            external_id=req_id, text=_strip_id(para, req_id),
            section_path=section, origin="pdf", ord=counter,
            confidence=confidence, notes=notes))
    return out


def _build_embedder(cfg: Config):
    from ..llm import build_embedder
    return build_embedder(cfg.embedding_provider, cfg.embedding_model,
                          dim=cfg.embedding_dim,
                          base_url=cfg.embedding_base_url,
                          api_key=cfg.embedding_api_key,
                          timeout=cfg.embedding_timeout,
                          batch=cfg.embedding_batch)


def _embedder_label(cfg: Config) -> str:
    if cfg.uses_external_embeddings():
        return f"{cfg.embedding_provider}:{cfg.embedding_model}"
    return "офлайн-эмбеддер hash"
