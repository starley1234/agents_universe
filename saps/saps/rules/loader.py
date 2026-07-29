"""Справочник авиационных правил: загрузка пунктов в базу.

ФОРМАТ — JSON-файл на набор правил (saps/rules/data/*.json):

  {"ruleset": "АП-25",
   "title": "Нормы лётной годности самолётов транспортной категории",
   "clauses": [
     {"clause": "25.1309", "title": "Оборудование, системы и установки",
      "text": "...", "keywords": "отказ система оборудование"}
   ]}

ПОЧЕМУ ТОЛЬКО ВЫДЕРЖКА, А НЕ ПОЛНЫЙ ТЕКСТ ПРАВИЛ. В поставку включён
демонстрационный справочник: коды разделов и пунктов с краткими
названиями. Полные тексты АП-21/АП-25 — документы Росавиации со своим
режимом распространения, и включать их в репозиторий было бы неверно.
Организация загружает свою актуальную редакцию командой
`saps rules load <файл>`; структура файла та же.

Это ограничение честно описано в README: качество работы Агента-
Классификатора напрямую зависит от полноты загруженного справочника, и
на демо-выдержке оно заведомо ниже, чем на реальном тексте правил.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db.store import Store


class RulesError(RuntimeError):
    """Ошибка загрузки справочника правил."""


def data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def list_builtin() -> list[str]:
    d = data_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def read_ruleset(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise RulesError(f"Файл справочника не найден: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RulesError(f"{p}: не разбирается как JSON — {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError(f"{p}: ожидался объект JSON верхнего уровня")
    ruleset = str(data.get("ruleset", "")).strip()
    if not ruleset:
        raise RulesError(f"{p}: не задано поле 'ruleset' (например, «АП-25»)")
    clauses = data.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise RulesError(f"{p}: 'clauses' должен быть непустым списком пунктов")
    for i, c in enumerate(clauses):
        if not isinstance(c, dict):
            raise RulesError(f"{p}: пункт #{i} должен быть объектом")
        if not str(c.get("clause", "")).strip():
            raise RulesError(f"{p}: у пункта #{i} нет поля 'clause'")
    return data


def load_ruleset(store: Store, path: str | Path, *,
                 embedder: Any = None) -> dict[str, Any]:
    """Загрузить справочник в базу. Повторная загрузка обновляет пункты."""
    data = read_ruleset(path)
    ruleset = str(data["ruleset"]).strip()
    loaded = 0
    for clause in data["clauses"]:
        code = str(clause["clause"]).strip()
        title = str(clause.get("title", "")).strip()
        text = str(clause.get("text", "")).strip()
        keywords = str(clause.get("keywords", "")).strip()
        embedding = None
        if embedder is not None:
            embedding = embedder.embed_one(
                " ".join(filter(None, [code, title, text, keywords])))
        store.upsert_clause(ruleset, code, title=title, text=text,
                            keywords=keywords, embedding=embedding,
                            meta={"source": Path(path).name})
        loaded += 1
    store.log("system", "rules_load", detail=f"{ruleset}: {loaded} пунктов",
              data={"ruleset": ruleset, "file": str(path)})
    return {"ruleset": ruleset, "loaded": loaded,
            "title": data.get("title", "")}


def load_builtin(store: Store, name: str = "", *, embedder: Any = None
                 ) -> list[dict[str, Any]]:
    """Загрузить встроенные демонстрационные справочники."""
    names = [name] if name else list_builtin()
    if not names:
        raise RulesError(
            f"Встроенных справочников нет в {data_dir()}. Загрузите свой файл: "
            "saps rules load <путь>")
    out = []
    for n in names:
        path = data_dir() / f"{n}.json"
        if not path.exists():
            raise RulesError(
                f"Встроенный справочник {n!r} не найден. Доступны: "
                f"{', '.join(list_builtin()) or '—'}")
        out.append(load_ruleset(store, path, embedder=embedder))
    return out
