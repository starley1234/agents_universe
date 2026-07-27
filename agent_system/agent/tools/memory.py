"""Инструменты памяти, онтологии и плана.

Дают агенту то, чего не хватало для долгой работы: он может записать
вывод, найти его через час, связать объекты в граф и вести план,
переживающий перезапуск процесса.
"""
from __future__ import annotations

import json

from ..store import Store
from .base import Tool, ToolError


def build(store: Store, run_id_getter) -> list[Tool]:
    """run_id_getter() -> int: текущий прогон (может меняться между задачами)."""

    def rid() -> int:
        return run_id_getter()

    # ---------------------------------------------------------- память
    def remember(text: str, tags: str = "", confidence: float = 1.0) -> str:
        if not text.strip():
            raise ToolError("Пустой факт записывать нечего")
        fid = store.remember(text, tags=tags, confidence=confidence,
                             run_id=rid())
        return f"Записано в память (id={fid}, всего фактов: {store.fact_count()})"

    def recall(query: str = "", limit: int = 10) -> str:
        rows = store.recall(query, limit)
        if not rows:
            return (f"По запросу {query!r} в памяти ничего нет"
                    if query else "Память пуста")
        out = []
        for r in rows:
            tag = f" [{r['tags']}]" if r["tags"] else ""
            conf = "" if r["confidence"] >= 1.0 else f" (уверенность {r['confidence']:.1f})"
            # Номер нужен для revise/forget: без него исправить найденное
            # нечем, и агенту остаётся только дописывать поверх.
            out.append(f"- #{r['id']} {r['text']}{tag}{conf}")
        return "\n".join(out)

    def revise(fact_id: int, text: str = "", tags: str = "",
               confidence: float = -1.0) -> str:
        old = store.revise(fact_id, text,
                           None if not tags else tags,
                           None if confidence < 0 else confidence)
        if old is None:
            raise ToolError(f"Факта #{fact_id} нет. Найди его через recall.")
        now = store.get_fact(fact_id)
        if now is None:
            return (f"Факт #{fact_id} совпал с уже существующим и удалён "
                    f"как дубль.\nБыло: {old['text'][:200]}")
        return (f"Факт #{fact_id} исправлен.\nБыло: {old['text'][:200]}\n"
                f"Стало: {now['text'][:200]}")

    def forget(fact_id: int = 0, query: str = "") -> str:
        try:
            gone = store.forget(fact_id, query)
        except ValueError as exc:
            raise ToolError(
                f"{exc}. Всю память разом стереть нельзя — это защита "
                "от случайной потери всей истории работы.") from exc
        if not gone:
            return ("Ничего не удалено: такого факта нет"
                    if fact_id else f"По запросу {query!r} ничего не найдено")
        head = f"Удалено фактов: {len(gone)}"
        body = "\n".join(f"- #{r['id']} {r['text'][:160]}" for r in gone[:10])
        tail = f"\n… и ещё {len(gone) - 10}" if len(gone) > 10 else ""
        return f"{head}\n{body}{tail}\nОсталось в памяти: {store.fact_count()}"

    # ------------------------------------------------------- онтология
    def link(subject_kind: str, subject: str, predicate: str,
             object_kind: str, object: str) -> str:
        created = store.link((subject_kind, subject), predicate,
                             (object_kind, object), run_id=rid())
        e, r = store.graph_stats()
        return (f"{'Связь создана' if created else 'Связь уже была'}: "
                f"{subject_kind}:{subject} --{predicate}--> "
                f"{object_kind}:{object}\nВ графе: {e} объектов, {r} связей")

    def describe(kind: str, name: str) -> str:
        rows = store.neighbours(kind, name)
        if not rows:
            return f"{kind}:{name} — связей нет (объект может быть не создан)"
        out = [f"{kind}:{name}"]
        for r in rows:
            arrow = "-->" if r["dir"] == "out" else "<--"
            out.append(f"  {arrow} {r['pred']} {r['kind']}:{r['name']}")
        return "\n".join(out)

    def note_entity(kind: str, name: str, props: str = "{}") -> str:
        try:
            data = json.loads(props) if props.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"props должен быть JSON-объектом: {exc}") from exc
        if not isinstance(data, dict):
            raise ToolError("props должен быть объектом, а не списком/строкой")
        eid = store.upsert_entity(kind, name, data, run_id=rid())
        return f"Объект {kind}:{name} сохранён (id={eid})"

    # ------------------------------------------------------------ план
    def plan_add(items: str) -> str:
        titles = [s.strip(" -•\t") for s in items.splitlines() if s.strip()]
        if not titles:
            raise ToolError("Пустой план")
        ids = store.add_tasks(rid(), titles)
        return f"В план добавлено пунктов: {len(ids)}"

    def plan_split(task_id: int, steps: str) -> str:
        """Разбить свой пункт на подшаги.

        Нужно, когда пункт оказался крупнее, чем виделось планировщику.
        Без этого у исполнителя было два выхода: тянуть неподъёмное
        целиком или провалить пункт — оба плохие.
        """
        titles = [x.strip(" -•*\t") for x in steps.splitlines() if x.strip()]
        titles = [t for t in titles if len(t) > 5]
        if len(titles) < 2:
            raise ToolError(
                "Разбивать имеет смысл минимум на 2 подшага, по одному "
                "на строку. Один подшаг — это тот же пункт другими словами.")
        if len(titles) > 6:
            titles = titles[:6]

        rows = {t["id"]: t for t in store.tasks(rid())}
        cur = rows.get(task_id)
        if cur is None:
            raise ToolError(f"Пункта #{task_id} нет в этом прогоне")
        if cur["parent_id"]:
            # Иначе дерево растёт вглубь, а прогон уходит в дробление
            # вместо работы. Два уровня покрывают реальные задачи.
            raise ToolError(
                f"Пункт #{task_id} сам является подшагом. Глубже двух "
                "уровней дробить нельзя — выполняй или закрывай через "
                "plan_fail.")
        if store.children(task_id):
            raise ToolError(f"Пункт #{task_id} уже разбит на подшаги")

        ids = store.add_tasks(rid(), titles, parent=task_id)
        return (f"Пункт #{task_id} разбит на {len(ids)} подшагов:\n"
                + "\n".join(f"  #{i} {t}" for i, t in zip(ids, titles))
                + "\nОни выполняются по очереди; сам пункт закроется, "
                  "когда закончатся подшаги. Работай над первым.")

    def plan_show() -> str:
        rows = store.tasks(rid())
        if not rows:
            return "План пуст. Составьте его через plan_add."
        mark = {"open": "[ ]", "doing": "[~]", "done": "[x]",
                "failed": "[!]", "skipped": "[-]", "blocked": "[?]"}
        out = []
        for t in rows:
            # Подшаги показываем с отступом: иначе непонятно, почему
            # пунктов вдруг стало вдвое больше.
            pad = "    " if t["parent_id"] else ""
            line = (f"{pad}{mark.get(t['status'], '[ ]')} #{t['id']} "
                    f"{t['title']}")
            if t["profile"]:
                line += f" [{t['profile']}]"
            if t["result"]:
                line += f"  → {t['result'][:120]}"
            out.append(line)
        done = sum(1 for t in rows if t["status"] == "done")
        out.append(f"\nВыполнено {done} из {len(rows)}")
        return "\n".join(out)

    def plan_done(task_id: int, result: str = "") -> str:
        # Заблокированный вопросом пункт закрывать нельзя: иначе агент
        # «выполнит» то, чего не сделал, и вопрос потеряется.
        cur = [t for t in store.tasks(rid()) if t["id"] == task_id]
        if cur and cur[0]["status"] == "blocked":
            raise ToolError(
                f"Пункт #{task_id} ждёт ответа человека — закрыть его нельзя. "
                "Возьми следующий пункт плана.")
        store.set_task(task_id, "done", result)
        left = [t for t in store.tasks(rid())
                if t["status"] in ("open", "doing")]
        return (f"Пункт #{task_id} закрыт. Осталось: {len(left)}"
                if left else f"Пункт #{task_id} закрыт. План выполнен полностью.")

    def plan_fail(task_id: int, reason: str) -> str:
        store.set_task(task_id, "failed", reason)
        return f"Пункт #{task_id} помечен как неудачный: {reason[:200]}"

    return [
        Tool("remember",
             "Записать вывод в долговременную память. Пиши сюда то, что "
             "пригодится позже: найденные числа, принятые решения, тупики. "
             "Память переживает перезапуск.",
             {"type": "object",
              "properties": {
                  "text": {"type": "string", "description": "Факт одной фразой"},
                  "tags": {"type": "string", "description": "Метки через запятую"},
                  "confidence": {"type": "number",
                                 "description": "0..1, если вывод неточный"}},
              "required": ["text"]},
             remember),
        Tool("recall",
             "Найти в памяти ранее записанное. Вызывай ПЕРЕД тем, как "
             "исследовать что-то заново — возможно, ответ уже найден.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": []},
             recall),
        Tool("revise",
             "Исправить факт в памяти по его номеру (#N из recall). "
             "Узнал, что записанное неверно или устарело, — правь, а не "
             "дописывай рядом: две противоречивые записи хуже одной.",
             {"type": "object",
              "properties": {
                  "fact_id": {"type": "integer", "description": "Номер из recall"},
                  "text": {"type": "string", "description": "Новый текст"},
                  "tags": {"type": "string"},
                  "confidence": {"type": "number",
                                 "description": "0..1, -1 = не менять"}},
              "required": ["fact_id"]},
             revise),
        Tool("forget",
             "Удалить из памяти неверное или устаревшее: по номеру факта "
             "либо всё найденное по запросу. Стереть память целиком "
             "нельзя. Удаляй осознанно — восстановления нет.",
             {"type": "object",
              "properties": {
                  "fact_id": {"type": "integer"},
                  "query": {"type": "string",
                            "description": "Удалить всё найденное по запросу"}},
              "required": []},
             forget,
             dangerous=True),
        Tool("note_entity",
             "Создать или дополнить объект предметной области "
             "(деталь, файл, человек, гипотеза, метрика).",
             {"type": "object",
              "properties": {
                  "kind": {"type": "string", "description": "Тип: part, file, idea…"},
                  "name": {"type": "string"},
                  "props": {"type": "string", "description": "JSON со свойствами"}},
              "required": ["kind", "name"]},
             note_entity),
        Tool("link",
             "Связать два объекта в графе знаний: субъект-предикат-объект. "
             "Так строится онтология задачи.",
             {"type": "object",
              "properties": {
                  "subject_kind": {"type": "string"},
                  "subject": {"type": "string"},
                  "predicate": {"type": "string",
                                "description": "Например: входит_в, зависит_от"},
                  "object_kind": {"type": "string"},
                  "object": {"type": "string"}},
              "required": ["subject_kind", "subject", "predicate",
                           "object_kind", "object"]},
             link),
        Tool("describe",
             "Показать все связи объекта в графе знаний.",
             {"type": "object",
              "properties": {"kind": {"type": "string"},
                             "name": {"type": "string"}},
              "required": ["kind", "name"]},
             describe),
        Tool("plan_add",
             "Добавить пункты в план (по одному на строку). План хранится "
             "в базе и переживает перезапуск.",
             {"type": "object",
              "properties": {"items": {"type": "string"}},
              "required": ["items"]},
             plan_add),
        Tool("plan_split",
             "Разбить свой пункт плана на 2-6 подшагов, если он оказался "
             "слишком крупным. Подшаги выполняются по очереди, сам пункт "
             "закроется автоматически. Лучше разбить, чем провалить.",
             {"type": "object",
              "properties": {
                  "task_id": {"type": "integer",
                              "description": "Номер разбиваемого пункта"},
                  "steps": {"type": "string",
                            "description": "Подшаги, по одному на строку"}},
              "required": ["task_id", "steps"]},
             plan_split),
        Tool("plan_show",
             "Показать текущий план и прогресс.",
             {"type": "object", "properties": {}, "required": []},
             plan_show),
        Tool("plan_done",
             "Закрыть пункт плана с кратким результатом.",
             {"type": "object",
              "properties": {"task_id": {"type": "integer"},
                             "result": {"type": "string"}},
              "required": ["task_id"]},
             plan_done),
        Tool("plan_fail",
             "Пометить пункт плана как неудачный с причиной. Честно "
             "фиксируй тупики — это экономит время потом.",
             {"type": "object",
              "properties": {"task_id": {"type": "integer"},
                             "reason": {"type": "string"}},
              "required": ["task_id", "reason"]},
             plan_fail),
    ]
