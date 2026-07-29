"""Двусторонняя синхронизация с Teamcenter (ТЗ п.5, Этап 3).

ЗАЩИТНЫЕ УСЛОВИЯ ЗАПИСИ — главное содержимое этого модуля. Технически
записать свойство в Teamcenter просто (см. TeamcenterClient.
set_properties); сложность в том, чтобы этого НЕ произошло случайно.
Поэтому push_requirement проверяет по порядку:

  1. запись включена в конфиге (tc_write_enabled) — иначе отказ;
  2. у требования есть tc_uid — писать «куда-нибудь» нельзя;
  3. статус требования = approved — то есть человек его утвердил;
  4. нет висящих предложений агентов (pending) — иначе в PDM уедет
     формулировка, по которой ещё не принято решение;
  5. действие записывается в audit_log ДО отправки, с полным текстом.

Пятый пункт особенно важен: если запрос уйдёт и оборвётся, у нас
останется запись о попытке. Обратный порядок (сначала запись, потом
журнал) при сбое даёт изменение в PDM без следа в САПС.

dry_run=True — режим по умолчанию для проверки: показывает, что именно
было бы записано, ничего не отправляя.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..config import Config
from ..db.store import Store, StoreError
from .pipeline import ImportResult, import_records
from .teamcenter import TeamcenterClient, TeamcenterError

#: Свойство Teamcenter, в которое пишется очищенная формулировка.
#: Вынесено константой: у заказчика может быть своё поле, и менять его
#: придётся в одном месте.
DEFAULT_TEXT_PROPERTY = "object_desc"


class SyncError(RuntimeError):
    """Ожидаемая ошибка синхронизации."""


@dataclass
class PushPlan:
    """Что будет записано в Teamcenter. Результат проверки перед записью."""
    requirement_id: int
    external_id: str
    tc_uid: str
    properties: dict[str, str]
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"requirement_id": self.requirement_id,
                "external_id": self.external_id, "tc_uid": self.tc_uid,
                "properties": self.properties, "allowed": self.allowed,
                "reasons": self.reasons}


def pull_item(store: Store, client: TeamcenterClient, item_id: str, *,
              actor: str = "") -> ImportResult:
    """Забрать изделие с требованиями из Teamcenter в staging."""
    requirements = client.fetch_requirements(item_id)
    return import_records(
        store, requirements, kind="teamcenter",
        name=f"Teamcenter: {item_id}",
        uri=f"{client.base_url}#{item_id}", actor=actor,
        meta={"item_id": item_id, "objects": len(requirements)})


def plan_push(store: Store, cfg: Config, req_id: int, *,
              text_property: str = DEFAULT_TEXT_PROPERTY) -> PushPlan:
    """Проверить, можно ли записать требование обратно. Ничего не отправляет."""
    req = store.get_requirement(req_id)
    if req is None:
        raise StoreError(f"Требование #{req_id} не найдено")

    reasons: list[str] = []
    if not cfg.tc_write_enabled:
        reasons.append(
            "запись в Teamcenter выключена (SAPS_TC_WRITE=false) — включите "
            "осознанно, это изменение данных в промышленном PDM")
    tc_uid = (req.get("tc_uid") or "").strip()
    if not tc_uid:
        reasons.append(
            "у требования нет tc_uid: оно не пришло из Teamcenter, значит "
            "неизвестно, какой объект обновлять")
    if req["status"] != "approved":
        reasons.append(
            f"статус требования {req['status']!r}, а не 'approved' — в "
            "Teamcenter уходят только утверждённые человеком формулировки")
    pending = store.list_suggestions(req_id=req_id, status="pending")
    if pending:
        reasons.append(
            f"есть необработанные предложения агентов ({len(pending)} шт.): "
            "сначала примите или отклоните их")

    properties = {text_property: req["text"]}
    if req.get("title"):
        properties["object_name"] = req["title"]

    return PushPlan(requirement_id=req_id, external_id=req["external_id"],
                    tc_uid=tc_uid, properties=properties,
                    allowed=not reasons, reasons=reasons)


def push_requirement(store: Store, cfg: Config, client: TeamcenterClient,
                     req_id: int, *, actor: str = "",
                     dry_run: bool = True,
                     text_property: str = DEFAULT_TEXT_PROPERTY
                     ) -> dict[str, Any]:
    """Записать требование в Teamcenter. По умолчанию — только план."""
    plan = plan_push(store, cfg, req_id, text_property=text_property)
    if not plan.allowed:
        return {"written": False, "dry_run": dry_run, "plan": plan.to_dict()}
    if dry_run:
        return {"written": False, "dry_run": True, "plan": plan.to_dict()}

    # Журнал ДО отправки: обрыв связи не должен оставить изменение в PDM
    # без следа в САПС.
    store.log(actor or "system", "tc_write", object_type="requirement",
              object_id=req_id,
              detail=f"запись в Teamcenter uid={plan.tc_uid}",
              data={"properties": plan.properties})
    try:
        response = client.set_properties(plan.tc_uid, plan.properties)
    except TeamcenterError as exc:
        store.log(actor or "system", "tc_write_failed",
                  object_type="requirement", object_id=req_id, detail=str(exc))
        raise SyncError(f"Не удалось записать требование в Teamcenter: {exc}") from exc

    store.mark_tc_synced(req_id)
    return {"written": True, "dry_run": False, "plan": plan.to_dict(),
            "response": response[:1000]}


def push_batch(store: Store, cfg: Config, client: TeamcenterClient,
               req_ids: Sequence[int], *, actor: str = "",
               dry_run: bool = True) -> dict[str, Any]:
    """Пакетная запись. Ошибка на одном требовании не отменяет остальные."""
    written, blocked, failed = [], [], []
    for req_id in req_ids:
        try:
            result = push_requirement(store, cfg, client, int(req_id),
                                      actor=actor, dry_run=dry_run)
        except (SyncError, StoreError) as exc:
            failed.append({"requirement_id": int(req_id), "error": str(exc)})
            continue
        if result["written"]:
            written.append(int(req_id))
        else:
            blocked.append(result["plan"])
    return {"written": written, "blocked": blocked, "failed": failed,
            "dry_run": dry_run}
