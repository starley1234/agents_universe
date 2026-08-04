"""LLM Synthetic Testing Mode — Digital Twin Enterprise Generator."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models import (
    Baseline,
    MDMObject,
    ObjectCode,
    ObjectLink,
    ObjectProperty,
    ObjectState,
    ObjectXref,
    OrgUnit,
    Type,
)
from src.db.repository import MDMRepository
from src.db.init_db import ensure_holding_exists


class SyntheticEnterpriseGenerator:
    """Synthesizes a complete fictional enterprise Digital Twin from natural language description."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MDMRepository(session)

    def _select_domain_profile(
        self, description: str, enterprise_code: Optional[str] = None
    ) -> Dict[str, Any]:
        """Select or generate structured enterprise profile matching description keywords."""
        desc_lower = description.lower()

        # 1. Aviation / UAV
        if any(w in desc_lower for w in ["бпла", "авиа", "беспилот", "самолет", "титан", "дрон", "летать", "aero"]):
            code = enterprise_code or "TITAN"
            return {
                "enterprise_code": code,
                "enterprise_name": "АО 'Небесный Титан'",
                "domain_title": "Авиастроение и беспилотные системы",
                "departments": [
                    {"id": f"{code}", "name": "АО 'Небесный Титан' (Головной завод)", "parent_id": "HOLDING"},
                    {"id": f"{code}_KB", "name": "Конструкторское бюро БПЛА", "parent_id": code},
                    {"id": f"{code}_ASSM", "name": "Сборочный цех авиатехники", "parent_id": code},
                    {"id": f"{code}_TEST", "name": "Летно-испытательный полигон", "parent_id": code},
                ],
                "types": [
                    {
                        "id": "uav_system",
                        "display_name": "Беспилотная авиационная система",
                        "parent_id": "part",
                        "schema": {"properties": {"mtow_kg": "number", "range_km": "number"}},
                    },
                    {
                        "id": "avionics_module",
                        "display_name": "Блок авионики и радиолокации",
                        "parent_id": "part",
                        "schema": {"properties": {"frequency_ghz": "number", "weight_kg": "number"}},
                    },
                    {
                        "id": "electric_motor",
                        "display_name": "Авиационный электродвигатель",
                        "parent_id": "part",
                        "schema": {"properties": {"power_kw": "number", "efficiency_pct": "number"}},
                    },
                ],
                "objects": [
                    {
                        "master_code": f"UAV-{code}-X100",
                        "name": f"БПЛА '{code}-Х100'",
                        "description": "Тяжелый ударно-разведывательный беспилотный комплекс повышенной дальности",
                        "type_id": "uav_system",
                        "org_id": f"{code}_KB",
                        "attributes": {
                            "mtow_kg": 450.0,
                            "cruise_speed_kmh": 180.0,
                            "range_km": 600.0,
                            "payload_kg": 120.0,
                        },
                        "source_id": "plm",
                        "children": [
                            {"child_code": f"MOTOR-EM-{code}-50", "qty": 4.0, "designator": "M1, M2, M3, M4"},
                            {"child_code": f"AVIO-RADAR-{code}-200", "qty": 1.0, "designator": "U1"},
                            {"child_code": f"BATT-LIPO-{code}-80V", "qty": 2.0, "designator": "B1, B2"},
                        ],
                    },
                    {
                        "master_code": f"MOTOR-EM-{code}-50",
                        "name": "Электродвигатель ЭМ-50 кВт",
                        "description": "Бесколлекторный авиамотор с высоким крутящим моментом",
                        "type_id": "electric_motor",
                        "org_id": f"{code}_ASSM",
                        "attributes": {
                            "power_kw": 50.0,
                            "weight_kg": 8.5,
                            "efficiency_pct": 94.5,
                            "part_number": "EM-50-KWT",
                        },
                        "source_id": "plm",
                    },
                    {
                        "master_code": f"AVIO-RADAR-{code}-200",
                        "name": "Блок радиолокации РЛС-200",
                        "description": "Малогабаритный радар с активной фазированной решеткой",
                        "type_id": "avionics_module",
                        "org_id": f"{code}_KB",
                        "attributes": {
                            "frequency_ghz": 9.5,
                            "range_m": 5000.0,
                            "weight_kg": 4.2,
                            "part_number": "RAD-200-A",
                        },
                        "source_id": "plm",
                    },
                    {
                        "master_code": f"BATT-LIPO-{code}-80V",
                        "name": "Аккумуляторный модуль 80В/150Ач",
                        "description": "Литий-полимерная батарея с системой подогрева",
                        "type_id": "part",
                        "org_id": f"{code}_ASSM",
                        "attributes": {
                            "voltage_v": 80.0,
                            "capacity_ah": 150.0,
                            "weight_kg": 32.0,
                            "part_number": "LIPO-80-150",
                        },
                        "source_id": "plm",
                    },
                ],
                "duplicate_candidate": {
                    "master_code": f"MOTOR-EM-{code}-50-AI",
                    "name": "Электродвигатель ЭМ-50 кВт (Импорт AI Matcher)",
                    "description": "Дублирующая запись электродвигателя из каталога AI Matcher",
                    "type_id": "electric_motor",
                    "org_id": f"{code}_ASSM",
                    "attributes": {
                        "power_kw": 49.8,
                        "weight_kg": 8.5,
                        "part_number": "EM-50-KWT",
                    },
                    "source_id": "ai",
                    "xref_source": "plm",
                    "xref_remote": f"TC-{code}-MOTOR-500",
                    "primary_code": f"MOTOR-EM-{code}-50",
                },
            }

        # 2. Marine / Shipyard
        elif any(w in desc_lower for w in ["судо", "верфь", "ледокол", "корабль", "морск", "арктик", "ship", "marine"]):
            code = enterprise_code or "ARCTIC"
            return {
                "enterprise_code": code,
                "enterprise_name": "АО 'Арктика-Марин'",
                "domain_title": "Судостроение и морские турбины",
                "departments": [
                    {"id": f"{code}", "name": "АО 'Арктика-Марин' (Верфь)", "parent_id": "HOLDING"},
                    {"id": f"{code}_DESIGN", "name": "КБ Морского машиностроения", "parent_id": code},
                    {"id": f"{code}_SHIPYARD", "name": "Стапельный цех №2", "parent_id": code},
                    {"id": f"{code}_TURBINE", "name": "Завод морских турбин", "parent_id": code},
                ],
                "types": [
                    {
                        "id": "icebreaker_ship",
                        "display_name": "Атомный ледокол",
                        "parent_id": "part",
                        "schema": {"properties": {"length_m": "number", "power_mw": "number"}},
                    },
                    {
                        "id": "marine_turbine",
                        "display_name": "Морская газотурбинная установка",
                        "parent_id": "part",
                        "schema": {"properties": {"power_mw": "number", "weight_kg": "number"}},
                    },
                    {
                        "id": "nav_radar",
                        "display_name": "Радионавигационный комплекс",
                        "parent_id": "part",
                        "schema": {"properties": {"range_nm": "number"}},
                    },
                ],
                "objects": [
                    {
                        "master_code": f"ICE-{code}-2030",
                        "name": f"Атомный ледокол '{code}-2030'",
                        "description": "Ледокол ледового класса Icebreaker8 для круглогодичной навигации",
                        "type_id": "icebreaker_ship",
                        "org_id": f"{code}_DESIGN",
                        "attributes": {
                            "length_m": 173.3,
                            "beam_m": 34.0,
                            "power_mw": 60.0,
                            "ice_breaking_m": 3.0,
                        },
                        "source_id": "plm",
                        "children": [
                            {"child_code": f"TURB-MGT-{code}-25MW", "qty": 2.0, "designator": "TURB-1, TURB-2"},
                            {"child_code": f"NAV-POLAR-{code}-X1", "qty": 1.0, "designator": "NAV-MAIN"},
                        ],
                    },
                    {
                        "master_code": f"TURB-MGT-{code}-25MW",
                        "name": "Морская турбина МГТУ-25 МВт",
                        "description": "Газотурбинный агрегат главного привода",
                        "type_id": "marine_turbine",
                        "org_id": f"{code}_TURBINE",
                        "attributes": {
                            "power_mw": 25.0,
                            "efficiency_pct": 38.5,
                            "weight_kg": 42000.0,
                            "part_number": "MGTU-25MW",
                        },
                        "source_id": "plm",
                    },
                    {
                        "master_code": f"NAV-POLAR-{code}-X1",
                        "name": "Радионавигационный комплекс 'Полярник-1'",
                        "description": "Двухдиапазонный ледовый радар и спутниковый приемник",
                        "type_id": "nav_radar",
                        "org_id": f"{code}_DESIGN",
                        "attributes": {
                            "range_nm": 64.0,
                            "frequency_band": "X/S-band",
                            "part_number": "POLAR-X1",
                        },
                        "source_id": "plm",
                    },
                ],
                "duplicate_candidate": {
                    "master_code": f"TURB-MGT-{code}-25MW-AI",
                    "name": "Морская турбина МГТУ-25 МВт (Импорт AI Matcher)",
                    "description": "Дублирующая запись турбины из AI Matcher",
                    "type_id": "marine_turbine",
                    "org_id": f"{code}_TURBINE",
                    "attributes": {
                        "power_mw": 24.9,
                        "weight_kg": 42000.0,
                        "part_number": "MGTU-25MW",
                    },
                    "source_id": "ai",
                    "xref_source": "plm",
                    "xref_remote": f"TC-{code}-TURB-25",
                    "primary_code": f"TURB-MGT-{code}-25MW",
                },
            }

        # 3. Robotics / Automation
        elif any(w in desc_lower for w in ["робот", "манипулятор", "серво", "автомат", "роботех", "мехатроник", "robot"]):
            code = enterprise_code or "ROBO"
            return {
                "enterprise_code": code,
                "enterprise_name": "ЗАО 'РобоТех-2030'",
                "domain_title": "Промышленная робототехника и сервоприводы",
                "departments": [
                    {"id": f"{code}", "name": "ЗАО 'РобоТех-2030' (Завод)", "parent_id": "HOLDING"},
                    {"id": f"{code}_RND", "name": "Центр робототехники и ИИ", "parent_id": code},
                    {"id": f"{code}_FACTORY", "name": "Цех сборки манипуляторов", "parent_id": code},
                ],
                "types": [
                    {
                        "id": "robot_arm",
                        "display_name": "Промышленный робот-манипулятор",
                        "parent_id": "part",
                        "schema": {"properties": {"payload_kg": "number", "reach_mm": "number"}},
                    },
                    {
                        "id": "servo_drive",
                        "display_name": "Сервопривод высокой точности",
                        "parent_id": "part",
                        "schema": {"properties": {"torque_nm": "number", "weight_kg": "number"}},
                    },
                    {
                        "id": "vision_sensor",
                        "display_name": "Оптический сенсор машинного зрения",
                        "parent_id": "part",
                        "schema": {"properties": {"resolution": "string"}},
                    },
                ],
                "objects": [
                    {
                        "master_code": f"ARM-{code}-6D",
                        "name": f"6-осевой манипулятор '{code}-6Д'",
                        "description": "Универсальный робот для сварки и точной сборки в машиностроении",
                        "type_id": "robot_arm",
                        "org_id": f"{code}_RND",
                        "attributes": {
                            "payload_kg": 25.0,
                            "reach_mm": 1850.0,
                            "repeatability_mm": 0.02,
                        },
                        "source_id": "plm",
                        "children": [
                            {"child_code": f"SERVO-SRV-{code}-100", "qty": 6.0, "designator": "J1, J2, J3, J4, J5, J6"},
                            {"child_code": f"VISION-CAM-{code}-3D", "qty": 1.0, "designator": "CAM-1"},
                        ],
                    },
                    {
                        "master_code": f"SERVO-SRV-{code}-100",
                        "name": "Сервопривод СРВ-100Нм",
                        "description": "Компактный привод с абсолютным энкодером",
                        "type_id": "servo_drive",
                        "org_id": f"{code}_FACTORY",
                        "attributes": {
                            "torque_nm": 100.0,
                            "max_rpm": 3000.0,
                            "weight_kg": 3.2,
                            "part_number": "SRV-100-NM",
                        },
                        "source_id": "plm",
                    },
                    {
                        "master_code": f"VISION-CAM-{code}-3D",
                        "name": "3D-стереокамера 'Окулюс-3Д'",
                        "description": "Сенсор глубины для распознавания объектов",
                        "type_id": "vision_sensor",
                        "org_id": f"{code}_RND",
                        "attributes": {
                            "resolution": "3840x2160 (4K)",
                            "depth_range_m": "0.1-5.0m",
                            "part_number": "CAM-3D-PRO",
                        },
                        "source_id": "plm",
                    },
                ],
                "duplicate_candidate": {
                    "master_code": f"SERVO-SRV-{code}-100-AI",
                    "name": "Сервопривод СРВ-100Нм (Импорт AI Matcher)",
                    "description": "Дублирующая запись сервопривода из AI Matcher",
                    "type_id": "servo_drive",
                    "org_id": f"{code}_FACTORY",
                    "attributes": {
                        "torque_nm": 99.8,
                        "weight_kg": 3.2,
                        "part_number": "SRV-100-NM",
                    },
                    "source_id": "ai",
                    "xref_source": "plm",
                    "xref_remote": f"TC-{code}-SERVO-100",
                    "primary_code": f"SERVO-SRV-{code}-100",
                },
            }

        # 4. Space / Satellites / Generic Fallback
        else:
            code = enterprise_code or "ORBIT"
            title = description[:40] if len(description) > 5 else "Космические системы и спутниковые платформы"
            return {
                "enterprise_code": code,
                "enterprise_name": f"АО 'Космические технологии ({code})'",
                "domain_title": title,
                "departments": [
                    {"id": f"{code}", "name": f"АО '{code}' (Главное управление)", "parent_id": "HOLDING"},
                    {"id": f"{code}_SAT", "name": "КБ Спутниковых платформ", "parent_id": code},
                    {"id": f"{code}_PROP", "name": "Цех ионных двигателей", "parent_id": code},
                ],
                "types": [
                    {
                        "id": "satellite_platform",
                        "display_name": "Спутниковая платформа",
                        "parent_id": "part",
                        "schema": {"properties": {"mass_kg": "number", "power_w": "number"}},
                    },
                    {
                        "id": "ion_thruster",
                        "display_name": "Ионный двигатель коррекции",
                        "parent_id": "part",
                        "schema": {"properties": {"thrust_mn": "number", "isp_s": "number"}},
                    },
                    {
                        "id": "solar_panel",
                        "display_name": "Солнечная панель GaAs",
                        "parent_id": "part",
                        "schema": {"properties": {"area_m2": "number", "efficiency_pct": "number"}},
                    },
                ],
                "objects": [
                    {
                        "master_code": f"SAT-{code}-M1",
                        "name": f"Спутник связи '{code}-М1'",
                        "description": "Малый космический аппарат для низкоорбитальной группировки",
                        "type_id": "satellite_platform",
                        "org_id": f"{code}_SAT",
                        "attributes": {
                            "mass_kg": 125.0,
                            "orbit_alt_km": 550.0,
                            "power_w": 1200.0,
                        },
                        "source_id": "plm",
                        "children": [
                            {"child_code": f"ION-THR-{code}-25MN", "qty": 2.0, "designator": "THR-1, THR-2"},
                            {"child_code": f"SOLAR-PANEL-{code}-600W", "qty": 2.0, "designator": "SOL-1, SOL-2"},
                        ],
                    },
                    {
                        "master_code": f"ION-THR-{code}-25MN",
                        "name": "Ионный двигатель ИД-25 мН",
                        "description": "Электроракетный двигатель на ксеноне",
                        "type_id": "ion_thruster",
                        "org_id": f"{code}_PROP",
                        "attributes": {
                            "thrust_mn": 25.0,
                            "isp_s": 2800.0,
                            "power_w": 500.0,
                            "part_number": "ION-25MN",
                        },
                        "source_id": "plm",
                    },
                    {
                        "master_code": f"SOLAR-PANEL-{code}-600W",
                        "name": "Солнечная батарея СБ-600 Вт",
                        "description": "Раскладываемая панель на основе арсенида галлия",
                        "type_id": "solar_panel",
                        "org_id": f"{code}_SAT",
                        "attributes": {
                            "power_w": 600.0,
                            "area_m2": 2.2,
                            "efficiency_pct": 30.5,
                            "part_number": "SOL-600W",
                        },
                        "source_id": "plm",
                    },
                ],
                "duplicate_candidate": {
                    "master_code": f"ION-THR-{code}-25MN-AI",
                    "name": "Ионный двигатель ИД-25 мН (Импорт AI Matcher)",
                    "description": "Дублирующая запись ионного двигателя из AI Matcher",
                    "type_id": "ion_thruster",
                    "org_id": f"{code}_PROP",
                    "attributes": {
                        "thrust_mn": 24.8,
                        "isp_s": 2800.0,
                        "part_number": "ION-25MN",
                    },
                    "source_id": "ai",
                    "xref_source": "plm",
                    "xref_remote": f"TC-{code}-ION-25",
                    "primary_code": f"ION-THR-{code}-25MN",
                },
            }

    async def synthesize_enterprise(
        self,
        description: str,
        enterprise_code: Optional[str] = None,
        include_duplicates: bool = True,
        actor_id: str = "llm_synthesizer",
    ) -> Dict[str, Any]:
        """Synthesize and insert enterprise OrgUnits, Types, MDMObjects, EBOM links, Baselines, and Duplicates."""
        logger.info(f"SyntheticEnterpriseGenerator: Synthesizing digital twin for '{description}'")

        await ensure_holding_exists(self.session)
        spec = self._select_domain_profile(description, enterprise_code)

        created_org_units = []
        created_types = []
        created_objects = []
        created_bom_links = 0
        created_baselines = 0
        created_duplicates = 0

        # 1. Create Organizational Units
        for dept in spec["departments"]:
            dept_id = dept["id"]
            res_exist = await self.session.execute(select(OrgUnit).where(OrgUnit.id == dept_id))
            if res_exist.scalar_one_or_none() is None:
                parent_id = dept.get("parent_id")
                # compute LTree path
                path = dept_id
                if parent_id and parent_id != "HOLDING":
                    path = f"HOLDING.{dept_id}"
                elif parent_id == "HOLDING":
                    path = f"HOLDING.{dept_id}"

                ou = OrgUnit(id=dept_id, parent_id=parent_id, name=dept["name"], path=path)
                self.session.add(ou)
                created_org_units.append(dept_id)

        # 2. Create Custom Ontology Types
        for typ in spec["types"]:
            type_id = typ["id"]
            res_t = await self.session.execute(select(Type).where(Type.id == type_id))
            if res_t.scalar_one_or_none() is None:
                parent_id = typ.get("parent_id", "part")
                t = Type(
                    id=type_id,
                    parent_id=parent_id,
                    display_name=typ["display_name"],
                    path=f"{parent_id}.{type_id}",
                    schema=typ.get("schema", {}),
                )
                self.session.add(t)
                created_types.append(type_id)

        # Map master_code -> UUID for BOM link binding
        code_to_id: Dict[str, str] = {}

        # 3. Create Master Objects & Baselines
        for obj_spec in spec["objects"]:
            mcode = obj_spec["master_code"]
            res_obj = await self.session.execute(select(MDMObject).where(MDMObject.master_code == mcode))
            existing_obj = res_obj.scalars().first()

            if existing_obj:
                code_to_id[mcode] = existing_obj.id
                continue

            new_id = str(uuid.uuid4())
            code_to_id[mcode] = new_id

            obj = MDMObject(
                id=new_id,
                type_id=obj_spec["type_id"],
                org_id=obj_spec["org_id"],
                org_path=obj_spec["org_id"],
                master_code=mcode,
                state="active",
                is_dirty=False,
            )
            self.session.add(obj)

            # ObjectCode registry (in PostgreSQL, tg_object_set_defaults trigger creates them automatically)
            if settings.is_sqlite:
                reg = ObjectCode(
                    master_code=mcode,
                    type_id=obj_spec["type_id"],
                    org_id=obj_spec["org_id"],
                    object_id=new_id,
                )
                self.session.add(reg)

            # Property
            attrs = obj_spec.get("attributes", {})
            attrs["name"] = obj_spec["name"]
            attrs["description"] = obj_spec["description"]

            prop = ObjectProperty(
                object_id=new_id,
                org_path=obj_spec["org_id"],
                key="attributes",
                value=attrs,
                source_id=obj_spec.get("source_id", "plm"),
                actor_id=actor_id,
                confidence=1.0,
                is_current=True,
            )
            self.session.add(prop)

            # ObjectState
            state = ObjectState(
                object_id=new_id,
                type_id=obj_spec["type_id"],
                org_path=obj_spec["org_id"],
                attributes=attrs,
                display_name=obj_spec["name"],
                display_desc=obj_spec["description"],
            )
            self.session.add(state)
            created_objects.append({"id": new_id, "master_code": mcode, "name": obj_spec["name"]})

            # Create Cryptographic SHA-256 Baseline
            snap_str = json.dumps(attrs, sort_keys=True)
            comp_str = json.dumps({"synthesized_by": actor_id, "domain": spec["domain_title"]}, sort_keys=True)
            raw_to_hash = f"GENESIS|{new_id}|{mcode}|{snap_str}|{comp_str}|{actor_id}"
            sha_hash = hashlib.sha256(raw_to_hash.encode("utf-8")).hexdigest()

            b = Baseline(
                seq=1,
                object_id=new_id,
                org_path=obj_spec["org_id"],
                code=mcode,
                snapshot=attrs,
                snapshot_hash=sha_hash,
                prev_hash="GENESIS",
                compliance_ref={"synthesized_by": actor_id, "domain": spec["domain_title"]},
                actor_id=actor_id,
            )
            self.session.add(b)
            created_baselines += 1

        # 4. Create EBOM / MBOM Links
        for obj_spec in spec["objects"]:
            parent_mcode = obj_spec["master_code"]
            parent_id = code_to_id.get(parent_mcode)
            if not parent_id:
                continue

            for child_ref in obj_spec.get("children", []):
                child_mcode = child_ref["child_code"]
                child_id = code_to_id.get(child_mcode)
                if not child_id:
                    continue

                res_link = await self.session.execute(
                    select(ObjectLink).where(ObjectLink.parent_id == parent_id, ObjectLink.child_id == child_id)
                )
                if res_link.scalar_one_or_none() is None:
                    lk = ObjectLink(
                        parent_id=parent_id,
                        child_id=child_id,
                        link_type="EBOM",
                        qty=float(child_ref.get("qty", 1.0)),
                        designator=child_ref.get("designator", "REF-1"),
                        valid_period="current",
                    )
                    self.session.add(lk)
                    created_bom_links += 1

        # 5. Create Duplicate Candidate for testing deduplication
        if include_duplicates and "duplicate_candidate" in spec:
            dup_spec = spec["duplicate_candidate"]
            dup_mcode = dup_spec["master_code"]
            res_d = await self.session.execute(select(MDMObject).where(MDMObject.master_code == dup_mcode))
            if res_d.scalars().first() is None:
                dup_id = str(uuid.uuid4())
                dup_obj = MDMObject(
                    id=dup_id,
                    type_id=dup_spec["type_id"],
                    org_id=dup_spec["org_id"],
                    org_path=dup_spec["org_id"],
                    master_code=dup_mcode,
                    state="active",
                    is_dirty=True,
                )
                self.session.add(dup_obj)

                if settings.is_sqlite:
                    dup_reg = ObjectCode(
                        master_code=dup_mcode,
                        type_id=dup_spec["type_id"],
                        org_id=dup_spec["org_id"],
                        object_id=dup_id,
                    )
                    self.session.add(dup_reg)

                dup_attrs = dup_spec.get("attributes", {})
                dup_attrs["name"] = dup_spec["name"]
                dup_attrs["description"] = dup_spec["description"]

                dup_prop = ObjectProperty(
                    object_id=dup_id,
                    org_path=dup_spec["org_id"],
                    key="attributes",
                    value=dup_attrs,
                    source_id=dup_spec.get("source_id", "ai"),
                    actor_id=actor_id,
                    confidence=0.75,
                    is_current=True,
                )
                self.session.add(dup_prop)

                dup_state = ObjectState(
                    object_id=dup_id,
                    type_id=dup_spec["type_id"],
                    org_path=dup_spec["org_id"],
                    attributes=dup_attrs,
                    display_name=dup_spec["name"],
                    display_desc=dup_spec["description"],
                )
                self.session.add(dup_state)

                # Link overlapping XREF between primary and duplicate
                primary_mcode = dup_spec["primary_code"]
                primary_id = code_to_id.get(primary_mcode)
                if primary_id:
                    xref_s = dup_spec.get("xref_source", "plm")
                    xref_r = dup_spec.get("xref_remote", f"TC-SYNC-{dup_mcode}")
                    res_x1 = await self.session.execute(
                        select(ObjectXref).where(ObjectXref.source_id == xref_s, ObjectXref.remote_id == xref_r)
                    )
                    if res_x1.scalars().first() is None:
                        x1 = ObjectXref(source_id=xref_s, remote_id=xref_r, object_id=primary_id)
                        x2 = ObjectXref(source_id="ai", remote_id=xref_r, object_id=dup_id)
                        self.session.add_all([x1, x2])

                created_duplicates += 1
                created_objects.append({"id": dup_id, "master_code": dup_mcode, "name": dup_spec["name"]})

        await self.session.commit()

        logger.info(
            f"Enterprise synthesis complete: {len(created_org_units)} orgs, {len(created_types)} types, "
            f"{len(created_objects)} objects, {created_bom_links} BOM links, {created_duplicates} duplicates."
        )

        return {
            "status": "success",
            "enterprise_code": spec["enterprise_code"],
            "enterprise_name": spec["enterprise_name"],
            "domain_title": spec["domain_title"],
            "created_org_units_count": len(created_org_units),
            "created_types_count": len(created_types),
            "created_objects_count": len(created_objects),
            "created_bom_links_count": created_bom_links,
            "created_baselines_count": created_baselines,
            "created_duplicates_count": created_duplicates,
            "objects": created_objects,
        }
