"""Seed database with initial dictionaries (НСИ) and demo Digital Twin objects."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings

from src.db.models import (
    Baseline,
    CodeSeries,
    MDMObject,
    ObjectCode,
    ObjectLink,
    ObjectProperty,
    ObjectState,
    ObjectXref,
    OrgUnit,
    Source,
    Type,
    Uom,
    User,
)


async def ensure_holding_exists(session: AsyncSession) -> None:
    """Ensure HOLDING root org unit exists so any parent_id='HOLDING' references always succeed."""
    res = await session.execute(select(OrgUnit).where(OrgUnit.id == "HOLDING"))
    if res.scalar_one_or_none() is None:
        holding = OrgUnit(id="HOLDING", parent_id=None, name="Главный офис", path="HOLDING")
        session.add(holding)
        await session.commit()
        logger.info("Created root OrgUnit 'HOLDING' in database.")


async def seed_initial_data(session: AsyncSession) -> None:
    """Seed HOLDING org unit, standard sources, admin user, types, UOM, and demo objects."""
    # 1. Ensure HOLDING exists
    await ensure_holding_exists(session)

    res_src = await session.execute(select(Source).where(Source.id == "plm"))
    if res_src.scalar_one_or_none() is not None:
        return

    logger.info("Seeding initial NSI dictionaries and demo Digital Twin objects...")

    # Sources
    s_plm = Source(id="plm", name="Teamcenter PLM", kind="plm", trust=90)
    s_ai = Source(id="ai", name="AI Matcher Engine", kind="ai", trust=30)
    s_manual = Source(id="manual", name="Ручной ввод МДМ", kind="manual", trust=75)
    session.add_all([s_plm, s_ai, s_manual])

    # Users
    u_admin = User(
        id="admin",
        name="Системный админ",
        org_id="HOLDING",
        role="admin",
        is_active=True,
    )
    session.add(u_admin)

    # Types
    t_part = Type(
        id="part",
        parent_id=None,
        display_name="Деталь / Сборочная единица",
        path="part",
        schema={"properties": {"weight": "number", "material": "string"}},
    )
    t_engine = Type(
        id="engine",
        parent_id="part",
        display_name="Двигатель авиационный",
        path="part.engine",
        schema={"properties": {"thrust_kn": "number", "rpm_max": "number"}},
    )
    t_cert = Type(
        id="cert_req",
        parent_id=None,
        display_name="Сертификационное требование (АП-25 / MoC)",
        path="cert_req",
        schema={"properties": {"clause": "string", "moc_type": "string"}},
    )
    session.add_all([t_part, t_engine, t_cert])

    # UOM
    uom_pc = Uom(
        code="796",
        base_code=None,
        factor=1.0,
        name="Штука",
        symbol_nat="шт",
        symbol_intl="pc",
        code_nat="ШТ",
        code_intl="PC",
    )
    uom_min = Uom(
        code="355",
        base_code=None,
        factor=1.0,
        name="Минута",
        symbol_nat="мин",
        symbol_intl="min",
        code_nat="МИН",
        code_intl="MIN",
    )
    session.add_all([uom_pc, uom_min])

    # Demo Digital Twin Objects
    obj_id_1 = str(uuid.uuid4())
    obj_id_2 = str(uuid.uuid4())
    obj_id_3 = str(uuid.uuid4())
    obj_id_4 = str(uuid.uuid4())

    obj1 = MDMObject(
        id=obj_id_1,
        type_id="engine",
        org_id="HOLDING",
        org_path="HOLDING",
        master_code="ENG-500-MASTER",
        state="active",
        is_dirty=False,
    )
    obj2 = MDMObject(
        id=obj_id_2,
        type_id="part",
        org_id="HOLDING",
        org_path="HOLDING",
        master_code="TURBO-COMP-01",
        state="active",
        is_dirty=False,
    )
    obj3 = MDMObject(
        id=obj_id_3,
        type_id="cert_req",
        org_id="HOLDING",
        org_path="HOLDING",
        master_code="CERT-AP25-1309",
        state="active",
        is_dirty=False,
    )
    obj4 = MDMObject(
        id=obj_id_4,
        type_id="part",
        org_id="HOLDING",
        org_path="HOLDING",
        master_code="TURBO-COMP-01-DUP",
        state="active",
        is_dirty=True,
    )
    session.add_all([obj1, obj2, obj3, obj4])

    # Object Codes registry (in PostgreSQL, tg_object_set_defaults trigger creates them automatically)
    if settings.is_sqlite:
        session.add_all(
            [
                ObjectCode(
                    master_code="ENG-500-MASTER",
                    type_id="engine",
                    org_id="HOLDING",
                    object_id=obj_id_1,
                ),
                ObjectCode(
                    master_code="TURBO-COMP-01",
                    type_id="part",
                    org_id="HOLDING",
                    object_id=obj_id_2,
                ),
                ObjectCode(
                    master_code="CERT-AP25-1309",
                    type_id="cert_req",
                    org_id="HOLDING",
                    object_id=obj_id_3,
                ),
                ObjectCode(
                    master_code="TURBO-COMP-01-DUP",
                    type_id="part",
                    org_id="HOLDING",
                    object_id=obj_id_4,
                ),
            ]
        )

    # Properties & ObjectState
    props1 = {
        "name": "Авиационный ДВС-500",
        "description": "Турбовальный двигатель для легких самолетов",
        "thrust_kn": 12.5,
        "weight_kg": 185.0,
    }
    props2 = {
        "name": "Турбокомпрессор ТК-25",
        "description": "Узел нагнетателя воздуха повышенного давления",
        "weight_kg": 18.2,
        "part_number": "TK-25-A",
    }
    props3 = {
        "name": "АП-25 §25.1309 Безопасность оборудования",
        "description": "Требования к отказобезопасности бортовых систем",
        "moc_type": "MoC-001 / Анализ отказов FMEA",
    }
    props4 = {
        "name": "Турбокомпрессор ТК-25 (AI Дубликат)",
        "description": "Автоматически импортированная сборка нагнетателя",
        "weight_kg": 18.0,
        "part_number": "TK-25-A",
    }

    p1 = ObjectProperty(
        object_id=obj_id_1,
        org_path="HOLDING",
        key="attributes",
        value=props1,
        source_id="plm",
        actor_id="admin",
        confidence=1.0,
        is_current=True,
    )
    p2 = ObjectProperty(
        object_id=obj_id_2,
        org_path="HOLDING",
        key="attributes",
        value=props2,
        source_id="plm",
        actor_id="admin",
        confidence=0.98,
        is_current=True,
    )
    p3 = ObjectProperty(
        object_id=obj_id_3,
        org_path="HOLDING",
        key="attributes",
        value=props3,
        source_id="manual",
        actor_id="admin",
        confidence=1.0,
        is_current=True,
    )
    p4 = ObjectProperty(
        object_id=obj_id_4,
        org_path="HOLDING",
        key="attributes",
        value=props4,
        source_id="ai",
        actor_id="admin",
        confidence=0.75,
        is_current=True,
    )
    session.add_all([p1, p2, p3, p4])

    state1 = ObjectState(
        object_id=obj_id_1,
        type_id="engine",
        org_path="HOLDING",
        attributes=props1,
        display_name=props1["name"],
        display_desc=props1["description"],
    )
    state2 = ObjectState(
        object_id=obj_id_2,
        type_id="part",
        org_path="HOLDING",
        attributes=props2,
        display_name=props2["name"],
        display_desc=props2["description"],
    )
    state3 = ObjectState(
        object_id=obj_id_3,
        type_id="cert_req",
        org_path="HOLDING",
        attributes=props3,
        display_name=props3["name"],
        display_desc=props3["description"],
    )
    state4 = ObjectState(
        object_id=obj_id_4,
        type_id="part",
        org_path="HOLDING",
        attributes=props4,
        display_name=props4["name"],
        display_desc=props4["description"],
    )
    session.add_all([state1, state2, state3, state4])

    # Shared Cross-Reference (XREF) demonstrating overlap between primary and duplicate
    xref1 = ObjectXref(
        source_id="plm",
        remote_id="TC-2005-COMP",
        object_id=obj_id_2,
    )
    xref2 = ObjectXref(
        source_id="ai",
        remote_id="TC-2005-COMP",
        object_id=obj_id_4,
    )
    session.add_all([xref1, xref2])

    # EBOM / MBOM link: ENG-500-MASTER -> TURBO-COMP-01
    link1 = ObjectLink(
        parent_id=obj_id_1,
        child_id=obj_id_2,
        link_type="EBOM",
        qty=2.0,
        designator="TC-1, TC-2",
        valid_period="current",
    )
    session.add(link1)

    # Baseline with cryptographic sha256 hash chain
    snap1_str = json.dumps(props1, sort_keys=True)
    comp1_str = json.dumps({"cert_ref": "CERT-AP25-1309"}, sort_keys=True)
    raw_to_hash = f"GENESIS|{obj_id_1}|ENG-500-MASTER|{snap1_str}|{comp1_str}|admin"
    sha_hash = hashlib.sha256(raw_to_hash.encode("utf-8")).hexdigest()

    b1 = Baseline(
        seq=1,
        object_id=obj_id_1,
        org_path="HOLDING",
        code="ENG-500-MASTER",
        snapshot=props1,
        snapshot_hash=sha_hash,
        prev_hash="GENESIS",
        compliance_ref={"cert_ref": "CERT-AP25-1309"},
        actor_id="admin",
    )
    session.add(b1)

    await session.commit()
    logger.info("Initial seed data successfully added to MDM holding database.")
