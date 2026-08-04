"""Async Repository layer for MDM + Certification + Digital Twin CRUD & verification."""
from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models import (
    Baseline,
    MDMObject,
    ObjectCode,
    ObjectLink,
    ObjectProperty,
    ObjectState,
    OrgUnit,
    Source,
    Type,
    Uom,
)


class MDMRepository:
    """Provides data access for Holding MDM, Certification, and Digital Twin."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # --- NSI / Reference Dictionaries ---
    async def get_org_units(self) -> List[Dict[str, Any]]:
        result = await self.session.execute(select(OrgUnit).order_by(OrgUnit.id))
        rows = result.scalars().all()
        return [
            {"id": r.id, "parent_id": r.parent_id, "name": r.name, "path": r.path}
            for r in rows
        ]

    async def get_types(self) -> List[Dict[str, Any]]:
        result = await self.session.execute(select(Type).order_by(Type.id))
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "parent_id": r.parent_id,
                "display_name": r.display_name,
                "path": r.path,
                "schema": r.schema,
            }
            for r in rows
        ]

    async def get_sources(self) -> List[Dict[str, Any]]:
        result = await self.session.execute(select(Source).order_by(Source.trust.desc()))
        rows = result.scalars().all()
        return [
            {"id": r.id, "name": r.name, "kind": r.kind, "trust": r.trust}
            for r in rows
        ]

    async def get_uom(self) -> List[Dict[str, Any]]:
        result = await self.session.execute(select(Uom).order_by(Uom.code))
        rows = result.scalars().all()
        return [
            {
                "code": r.code,
                "name": r.name,
                "symbol_nat": r.symbol_nat,
                "symbol_intl": r.symbol_intl,
                "factor": float(r.factor) if r.factor else 1.0,
            }
            for r in rows
        ]

    # --- Objects & Digital Twin State ---
    async def list_objects(
        self,
        search_query: Optional[str] = None,
        type_id: Optional[str] = None,
        org_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        stmt = select(MDMObject, ObjectState).outerjoin(
            ObjectState, MDMObject.id == ObjectState.object_id
        )

        if type_id:
            stmt = stmt.where(MDMObject.type_id == type_id)
        if org_id:
            stmt = stmt.where(MDMObject.org_id == org_id)

        stmt = stmt.order_by(MDMObject.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        rows = result.all()

        items = []
        for obj, state in rows:
            display_name = (
                state.display_name if state and state.display_name else obj.master_code
            )
            display_desc = (
                state.display_desc if state and state.display_desc else ""
            )
            attributes = state.attributes if state else {}

            if search_query:
                sq = search_query.lower()
                text_blob = f"{obj.master_code} {display_name} {display_desc} {json.dumps(attributes)}".lower()
                if sq not in text_blob:
                    continue

            items.append(
                {
                    "id": obj.id,
                    "type_id": obj.type_id,
                    "org_id": obj.org_id,
                    "org_path": obj.org_path,
                    "master_code": obj.master_code,
                    "state": obj.state,
                    "display_name": display_name,
                    "display_desc": display_desc,
                    "attributes": attributes,
                    "is_dirty": obj.is_dirty,
                    "created_at": obj.created_at.isoformat(),
                    "updated_at": obj.updated_at.isoformat(),
                }
            )
        return items

    async def get_object_detail(self, object_id: str) -> Optional[Dict[str, Any]]:
        stmt = select(MDMObject, ObjectState).outerjoin(
            ObjectState, MDMObject.id == ObjectState.object_id
        ).where(MDMObject.id == object_id)
        res = await self.session.execute(stmt)
        row = res.first()
        if not row:
            return None
        obj, state = row

        # fetch properties
        res_props = await self.session.execute(
            select(ObjectProperty)
            .where(ObjectProperty.object_id == object_id)
            .order_by(ObjectProperty.id.desc())
        )
        props = res_props.scalars().all()

        return {
            "id": obj.id,
            "type_id": obj.type_id,
            "org_id": obj.org_id,
            "org_path": obj.org_path,
            "master_code": obj.master_code,
            "state": obj.state,
            "display_name": state.display_name if state and state.display_name else obj.master_code,
            "display_desc": state.display_desc if state and state.display_desc else "",
            "attributes": state.attributes if state else {},
            "is_dirty": obj.is_dirty,
            "created_at": obj.created_at.isoformat(),
            "updated_at": obj.updated_at.isoformat(),
            "properties": [
                {
                    "id": p.id,
                    "key": p.key,
                    "value": p.value,
                    "source_id": p.source_id,
                    "confidence": p.confidence,
                    "is_current": p.is_current,
                    "valid_period": p.valid_period,
                    "created_at": p.created_at.isoformat(),
                }
                for p in props
            ],
        }

    async def create_object(
        self,
        type_id: str,
        org_id: str,
        master_code: str,
        name: str,
        description: str = "",
        attributes: Optional[Dict[str, Any]] = None,
        source_id: str = "manual",
        actor_id: str = "admin",
    ) -> Dict[str, Any]:
        """Create a new MDM Object and initialize its EAV property & Digital Twin state."""
        new_id = str(uuid.uuid4())
        obj = MDMObject(
            id=new_id,
            type_id=type_id,
            org_id=org_id,
            org_path=org_id,
            master_code=master_code,
            state="active",
            is_dirty=False,
        )
        self.session.add(obj)

        attrs = attributes or {}
        attrs["name"] = name
        attrs["description"] = description

        prop = ObjectProperty(
            object_id=new_id,
            org_path=org_id,
            key="attributes",
            value=attrs,
            source_id=source_id,
            actor_id=actor_id,
            confidence=1.0,
            is_current=True,
        )
        self.session.add(prop)

        state = ObjectState(
            object_id=new_id,
            type_id=type_id,
            org_path=org_id,
            attributes=attrs,
            display_name=name,
            display_desc=description,
        )
        self.session.add(state)

        # Register in object_codes (in PostgreSQL, tg_object_set_defaults trigger creates them automatically)
        if settings.is_sqlite:
            code_reg = ObjectCode(
                master_code=master_code,
                type_id=type_id,
                org_id=org_id,
                object_id=new_id,
            )
            self.session.add(code_reg)

        await self.session.commit()
        return await self.get_object_detail(new_id)  # type: ignore

    # --- Upsert Property with Trust Score & Time-Travel ---
    async def upsert_property(
        self,
        object_id: str,
        key: str,
        value: Dict[str, Any],
        source_id: str,
        uom_code: Optional[str] = None,
        actor_id: str = "admin",
    ) -> str:
        """Upsert EAV property with trust check (0-100) and time-travel archiving of previous value."""
        # check trust of source_id
        res_s = await self.session.execute(
            select(Source.trust).where(Source.id == source_id)
        )
        new_trust = res_s.scalar_one_or_none() or 50

        # get old current property
        res_old = await self.session.execute(
            select(ObjectProperty)
            .where(
                ObjectProperty.object_id == object_id,
                ObjectProperty.key == key,
                ObjectProperty.is_current == True,
            )
            .order_by(ObjectProperty.id.desc())
        )
        old_prop = res_old.scalars().first()

        if old_prop:
            res_old_trust = await self.session.execute(
                select(Source.trust).where(Source.id == old_prop.source_id)
            )
            old_trust = res_old_trust.scalar_one_or_none() or 50
            if new_trust < old_trust:
                logger.warning(
                    f"Rejected property upsert for object {object_id} key={key} due to low trust ({new_trust} < {old_trust})"
                )
                return "rejected_low_trust"

            # Archive old property
            old_prop.is_current = False
            old_prop.valid_period = "archived"

        # Insert new property
        new_prop = ObjectProperty(
            object_id=object_id,
            org_path="HOLDING",
            key=key,
            value=value,
            uom_code=uom_code,
            source_id=source_id,
            actor_id=actor_id,
            confidence=1.0,
            is_current=True,
        )
        self.session.add(new_prop)

        # Refresh ObjectState
        res_state = await self.session.execute(
            select(ObjectState).where(ObjectState.object_id == object_id)
        )
        state_row = res_state.scalars().first()
        if state_row and key == "attributes":
            state_row.attributes = value
            if "name" in value:
                state_row.display_name = value["name"]
            if "description" in value:
                state_row.display_desc = value["description"]
            state_row.updated_at = datetime.datetime.now()

        # Mark object dirty
        await self.session.execute(
            update(MDMObject)
            .where(MDMObject.id == object_id)
            .values(is_dirty=True, updated_at=datetime.datetime.now())
        )

        await self.session.commit()
        return "ok"

    # --- EBOM / MBOM Graph ---
    async def get_bom_tree(self, object_id: str) -> List[Dict[str, Any]]:
        """Retrieve EBOM / MBOM links where parent_id == object_id."""
        stmt = (
            select(ObjectLink, MDMObject, ObjectState)
            .join(MDMObject, ObjectLink.child_id == MDMObject.id)
            .outerjoin(ObjectState, MDMObject.id == ObjectState.object_id)
            .where(ObjectLink.parent_id == object_id)
            .order_by(ObjectLink.id)
        )
        res = await self.session.execute(stmt)
        rows = res.all()

        links = []
        for lk, child_obj, child_state in rows:
            links.append(
                {
                    "id": lk.id,
                    "parent_id": lk.parent_id,
                    "child_id": lk.child_id,
                    "link_type": lk.link_type,
                    "qty": float(lk.qty) if lk.qty else 1.0,
                    "designator": lk.designator,
                    "child_master_code": child_obj.master_code,
                    "child_display_name": child_state.display_name if child_state else child_obj.master_code,
                    "valid_period": lk.valid_period,
                }
            )
        return links

    async def add_bom_link(
        self,
        parent_id: str,
        child_id: str,
        link_type: str = "EBOM",
        qty: float = 1.0,
        designator: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new BOM link between parent and child MDM objects."""
        link = ObjectLink(
            parent_id=parent_id,
            child_id=child_id,
            link_type=link_type,
            qty=qty,
            designator=designator,
            valid_period="current",
        )
        self.session.add(link)
        await self.session.commit()
        return {
            "id": link.id,
            "parent_id": parent_id,
            "child_id": child_id,
            "link_type": link_type,
            "qty": qty,
            "designator": designator,
        }

    # --- Baselines & Hash-Chain Compliance Verification ---
    async def list_baselines(self, object_id: str) -> List[Dict[str, Any]]:
        res = await self.session.execute(
            select(Baseline)
            .where(Baseline.object_id == object_id)
            .order_by(Baseline.seq.asc())
        )
        rows = res.scalars().all()
        return [
            {
                "id": r.id,
                "seq": r.seq,
                "object_id": r.object_id,
                "code": r.code,
                "snapshot": r.snapshot,
                "snapshot_hash": r.snapshot_hash,
                "prev_hash": r.prev_hash,
                "compliance_ref": r.compliance_ref,
                "actor_id": r.actor_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    async def create_baseline(
        self,
        object_id: str,
        code: str,
        snapshot: Dict[str, Any],
        compliance_ref: Dict[str, Any],
        actor_id: str = "admin",
    ) -> Dict[str, Any]:
        """Create an append-only Baseline with cryptographic sha256 hash chaining."""
        res_last = await self.session.execute(
            select(Baseline)
            .where(Baseline.object_id == object_id)
            .order_by(Baseline.seq.desc())
        )
        last_b = res_last.scalars().first()

        seq = (last_b.seq + 1) if last_b else 1
        prev_hash = last_b.snapshot_hash if last_b else "GENESIS"

        snap_str = json.dumps(snapshot, sort_keys=True)
        comp_str = json.dumps(compliance_ref, sort_keys=True)
        raw_to_hash = f"{prev_hash}|{object_id}|{code}|{snap_str}|{comp_str}|{actor_id}"
        sha_hash = hashlib.sha256(raw_to_hash.encode("utf-8")).hexdigest()

        b = Baseline(
            seq=seq,
            object_id=object_id,
            org_path="HOLDING",
            code=code,
            snapshot=snapshot,
            snapshot_hash=sha_hash,
            prev_hash=prev_hash,
            compliance_ref=compliance_ref,
            actor_id=actor_id,
        )
        self.session.add(b)
        await self.session.commit()
        return {
            "id": b.id,
            "seq": b.seq,
            "object_id": b.object_id,
            "code": b.code,
            "snapshot_hash": b.snapshot_hash,
            "prev_hash": b.prev_hash,
            "actor_id": b.actor_id,
        }

    async def verify_baseline_chain(self, object_id: str) -> List[Dict[str, Any]]:
        """Verify hash chain integrity for an object (equivalent to SQL verify_baseline_chain)."""
        res = await self.session.execute(
            select(Baseline)
            .where(Baseline.object_id == object_id)
            .order_by(Baseline.seq.asc())
        )
        rows = res.scalars().all()

        results = []
        prev_hash = None
        first = True
        for r in rows:
            snap_str = json.dumps(r.snapshot, sort_keys=True)
            comp_str = json.dumps(r.compliance_ref, sort_keys=True)
            expected_prev = prev_hash if prev_hash else "GENESIS"
            raw_to_hash = f"{expected_prev}|{r.object_id}|{r.code}|{snap_str}|{comp_str}|{r.actor_id}"
            recalc = hashlib.sha256(raw_to_hash.encode("utf-8")).hexdigest()

            if recalc != r.snapshot_hash:
                status = "HASH_MISMATCH"
            elif first and r.prev_hash != "GENESIS" and r.prev_hash is not None:
                status = "BROKEN_LINK"
            elif not first and r.prev_hash != prev_hash:
                status = "BROKEN_LINK"
            elif r.signature is not None and r.signed_hash != r.snapshot_hash:
                status = "SIGNED_HASH_DRIFT"
            else:
                status = "OK"

            results.append(
                {
                    "seq": r.seq,
                    "baseline_id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "status": status,
                }
            )
            prev_hash = r.snapshot_hash
            first = False

        return results
