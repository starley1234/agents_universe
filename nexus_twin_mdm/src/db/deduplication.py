"""MDM Deduplication and Merging Engine for Holding Digital Twin."""
from __future__ import annotations

import datetime
import difflib
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
    ObjectXref,
    Source,
)
from src.db.repository import MDMRepository


class DeduplicationEngine:
    """Enterprise deduplication engine with fuzzy matching, XREF overlap, and transactional merge."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = MDMRepository(session)

    def _string_similarity(self, s1: str, s2: str) -> float:
        """Calculate normalized string similarity (0.0 - 1.0) using SequenceMatcher."""
        if not s1 or not s2:
            return 0.0
        norm1 = " ".join(s1.lower().strip().split())
        norm2 = " ".join(s2.lower().strip().split())
        if norm1 == norm2:
            return 1.0
        return difflib.SequenceMatcher(None, norm1, norm2).ratio()

    async def detect_duplicates(
        self,
        type_id: Optional[str] = None,
        org_id: Optional[str] = None,
        threshold: float = 0.75,
    ) -> List[Dict[str, Any]]:
        """Scan MDM objects to identify duplicate clusters by name, code, XREF, and attributes."""
        # 1. Fetch active objects
        stmt = select(MDMObject, ObjectState).outerjoin(
            ObjectState, MDMObject.id == ObjectState.object_id
        ).where(MDMObject.state != "merged", MDMObject.deleted_at.is_(None))

        if type_id:
            stmt = stmt.where(MDMObject.type_id == type_id)
        if org_id:
            stmt = stmt.where(MDMObject.org_id == org_id)

        res = await self.session.execute(stmt)
        rows = res.all()

        # 2. Fetch all XREFs for these objects
        xrefs_res = await self.session.execute(select(ObjectXref))
        xref_rows = xrefs_res.scalars().all()
        xrefs_by_obj: Dict[str, List[Tuple[str, str]]] = {}
        for x in xref_rows:
            xrefs_by_obj.setdefault(x.object_id, []).append((x.source_id, x.remote_id))

        # 3. Pairwise comparison
        clusters: List[Dict[str, Any]] = []
        visited_pairs = set()

        for i in range(len(rows)):
            obj1, state1 = rows[i]
            name1 = state1.display_name if state1 and state1.display_name else obj1.master_code
            attrs1 = state1.attributes if state1 else {}
            xref1 = set(xrefs_by_obj.get(obj1.id, []))

            for j in range(i + 1, len(rows)):
                obj2, state2 = rows[j]
                # Skip if different types when type isn't generic
                if obj1.type_id != obj2.type_id:
                    continue

                pair_key = tuple(sorted([obj1.id, obj2.id]))
                if pair_key in visited_pairs:
                    continue
                visited_pairs.add(pair_key)

                name2 = state2.display_name if state2 and state2.display_name else obj2.master_code
                attrs2 = state2.attributes if state2 else {}
                xref2 = set(xrefs_by_obj.get(obj2.id, []))

                reasons = []
                max_sim = 0.0

                # Check Name similarity
                sim_name = self._string_similarity(name1, name2)
                if sim_name >= threshold:
                    max_sim = max(max_sim, sim_name)
                    reasons.append(f"Схожесть наименования: {int(sim_name * 100)}%")

                # Check Master Code similarity / prefix
                sim_code = self._string_similarity(obj1.master_code, obj2.master_code)
                if sim_code >= 0.8:
                    max_sim = max(max_sim, sim_code)
                    reasons.append(f"Схожесть кода: {int(sim_code * 100)}%")
                elif obj1.master_code.split("-")[0] == obj2.master_code.split("-")[0] and sim_name >= 0.65:
                    max_sim = max(max_sim, 0.75)
                    reasons.append("Общий префикс кода и схожесть наименования")

                # Check XREF overlap
                overlap_xref = xref1.intersection(xref2)
                if overlap_xref:
                    max_sim = max(max_sim, 0.95)
                    reasons.append(f"Совпадение внешних идентификаторов PLM/1C: {list(overlap_xref)}")

                # Check specific attribute keys (part_number, serial_number, model)
                for key in ["part_number", "serial_number", "model", "articul"]:
                    v1 = attrs1.get(key)
                    v2 = attrs2.get(key)
                    if v1 and v2 and str(v1).strip().lower() == str(v2).strip().lower():
                        max_sim = max(max_sim, 0.90)
                        reasons.append(f"Совпадение атрибута '{key}': {v1}")

                if max_sim >= threshold and reasons:
                    # Determine primary vs duplicate based on trust score / created_at
                    detail1 = await self.repo.get_object_detail(obj1.id)
                    detail2 = await self.repo.get_object_detail(obj2.id)

                    t1_max = max([p.get("confidence", 1.0) * 100 for p in detail1.get("properties", [])], default=50) if detail1 else 50
                    t2_max = max([p.get("confidence", 1.0) * 100 for p in detail2.get("properties", [])], default=50) if detail2 else 50

                    if t1_max >= t2_max:
                        primary, dup = detail1, detail2
                    else:
                        primary, dup = detail2, detail1

                    clusters.append(
                        {
                            "cluster_id": f"cluster-{uuid.uuid4().hex[:8]}",
                            "type_id": obj1.type_id,
                            "similarity": round(max_sim, 2),
                            "reason": "; ".join(reasons),
                            "primary_candidate": {
                                "id": primary["id"],
                                "master_code": primary["master_code"],
                                "display_name": primary["display_name"],
                                "state": primary["state"],
                                "org_path": primary["org_path"],
                            },
                            "duplicates": [
                                {
                                    "id": dup["id"],
                                    "master_code": dup["master_code"],
                                    "display_name": dup["display_name"],
                                    "state": dup["state"],
                                    "similarity": round(max_sim, 2),
                                }
                            ],
                        }
                    )

        return clusters

    async def merge_objects(
        self,
        primary_id: str,
        duplicate_id: str,
        merge_strategy: str = "trust_based",
        actor_id: str = "admin",
    ) -> Dict[str, Any]:
        """Merge duplicate_id into primary_id with trust-based EAV consolidation and EBOM link rebinding."""
        logger.info(
            f"DeduplicationEngine: Merging duplicate {duplicate_id} -> primary {primary_id} (strategy={merge_strategy})"
        )

        if primary_id == duplicate_id:
            raise ValueError("Primary ID and Duplicate ID cannot be the same.")

        res_p = await self.session.execute(select(MDMObject).where(MDMObject.id == primary_id))
        primary_obj = res_p.scalar_one_or_none()
        res_d = await self.session.execute(select(MDMObject).where(MDMObject.id == duplicate_id))
        duplicate_obj = res_d.scalar_one_or_none()

        if not primary_obj or not duplicate_obj:
            raise ValueError("Primary or Duplicate object not found.")

        if duplicate_obj.state == "merged":
            raise ValueError(f"Duplicate object {duplicate_id} is already merged.")

        # 1. Load EAV properties
        res_p_props = await self.session.execute(
            select(ObjectProperty).where(ObjectProperty.object_id == primary_id, ObjectProperty.is_current == True)
        )
        primary_props = res_p_props.scalars().all()
        res_d_props = await self.session.execute(
            select(ObjectProperty).where(ObjectProperty.object_id == duplicate_id, ObjectProperty.is_current == True)
        )
        duplicate_props = res_d_props.scalars().all()

        primary_prop_map = {p.key: p for p in primary_props}
        merged_properties_count = 0

        for dup_prop in duplicate_props:
            should_copy = False
            if dup_prop.key not in primary_prop_map:
                should_copy = True
            elif merge_strategy == "duplicate_wins":
                should_copy = True
            elif merge_strategy == "trust_based":
                # Check source trust scores
                res_t_dup = await self.session.execute(select(Source.trust).where(Source.id == dup_prop.source_id))
                t_dup = res_t_dup.scalar_one_or_none() or 50

                res_t_prim = await self.session.execute(select(Source.trust).where(Source.id == primary_prop_map[dup_prop.key].source_id))
                t_prim = res_t_prim.scalar_one_or_none() or 50

                if t_dup > t_prim:
                    should_copy = True

            if should_copy:
                merged_properties_count += 1
                await self.repo.upsert_property(
                    object_id=primary_id,
                    key=dup_prop.key,
                    value=dup_prop.value,
                    source_id=dup_prop.source_id,
                    uom_code=dup_prop.uom_code,
                    actor_id=actor_id,
                )

        # 2. Rebind EBOM / MBOM links
        links_parent_res = await self.session.execute(select(ObjectLink).where(ObjectLink.parent_id == duplicate_id))
        dup_as_parent = links_parent_res.scalars().all()
        rebound_links = 0
        for lk in dup_as_parent:
            # Check if primary already links to this child
            res_exist = await self.session.execute(
                select(ObjectLink).where(ObjectLink.parent_id == primary_id, ObjectLink.child_id == lk.child_id)
            )
            if res_exist.scalar_one_or_none() is None and lk.child_id != primary_id:
                lk.parent_id = primary_id
                rebound_links += 1
            else:
                await self.session.delete(lk)

        links_child_res = await self.session.execute(select(ObjectLink).where(ObjectLink.child_id == duplicate_id))
        dup_as_child = links_child_res.scalars().all()
        for lk in dup_as_child:
            res_exist = await self.session.execute(
                select(ObjectLink).where(ObjectLink.parent_id == lk.parent_id, ObjectLink.child_id == primary_id)
            )
            if res_exist.scalar_one_or_none() is None and lk.parent_id != primary_id:
                lk.child_id = primary_id
                rebound_links += 1
            else:
                await self.session.delete(lk)

        # 3. Rebind Cross-References (XREF)
        xrefs_res = await self.session.execute(select(ObjectXref).where(ObjectXref.object_id == duplicate_id))
        dup_xrefs = xrefs_res.scalars().all()
        rebound_xrefs = 0
        for xref in dup_xrefs:
            res_exist = await self.session.execute(
                select(ObjectXref).where(
                    ObjectXref.source_id == xref.source_id,
                    ObjectXref.remote_id == xref.remote_id,
                    ObjectXref.object_id == primary_id,
                )
            )
            if res_exist.scalar_one_or_none() is None:
                xref.object_id = primary_id
                rebound_xrefs += 1
            else:
                await self.session.delete(xref)

        # 4. Mark duplicate as merged & soft-deleted
        now_ts = datetime.datetime.now()
        duplicate_obj.state = "merged"
        duplicate_obj.merged_into = primary_id
        duplicate_obj.deleted_at = now_ts
        duplicate_obj.updated_at = now_ts

        # Remove from object_codes registry for duplicate so master_code is freed
        await self.session.execute(
            delete(ObjectCode).where(ObjectCode.object_id == duplicate_id)
        )

        # 5. Create immutable cryptographic Baseline on Primary recording the merge event
        primary_detail = await self.repo.get_object_detail(primary_id)
        snapshot = primary_detail["attributes"] if primary_detail else {}
        compliance_ref = {
            "event": "DEDUPLICATION_MERGE",
            "merged_from_id": duplicate_id,
            "merged_from_code": duplicate_obj.master_code,
            "merge_strategy": merge_strategy,
            "merged_properties_count": merged_properties_count,
            "rebound_links": rebound_links,
            "rebound_xrefs": rebound_xrefs,
            "timestamp": now_ts.isoformat(),
        }

        b = await self.repo.create_baseline(
            object_id=primary_id,
            code=primary_obj.master_code,
            snapshot=snapshot,
            compliance_ref=compliance_ref,
            actor_id=actor_id,
        )

        await self.session.commit()

        logger.info(
            f"Successfully merged duplicate {duplicate_id} into {primary_id}. Created baseline seq=#{b['seq']}."
        )

        return {
            "status": "merged",
            "primary_id": primary_id,
            "duplicate_id": duplicate_id,
            "merged_properties_count": merged_properties_count,
            "rebound_links_count": rebound_links,
            "rebound_xrefs_count": rebound_xrefs,
            "new_baseline": b,
        }
