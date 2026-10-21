"""Turn a layout alternative into a hypothetical revision (``RevisionKind.HYPOTHETICAL``) and a change set.

Geometry, fixtures, zones and relations are copied unchanged from the base revision; only the brand
attributes of the affected units change. The change set lists one MOVE item per relocated brand and a
REASSIGN item when a brand enters from outside the layout (``from_unit`` None) or leaves it
(``to_unit`` None), mirroring the ground-truth conventions of ``cad2anything.mockgen``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any

from cad2anything.analytics.prep import category_name
from cad2anything.layout.whatif import ESTIMATE_LABEL, format_krw_delta
from cad2anything.model import (
    ChangeItem,
    ChangeSet,
    ChangeType,
    Entity,
    Revision,
    RevisionKind,
    SpatialModel,
)

HYPOTHETICAL_NOTE = "가상 개정: 형상·집기·존·관계는 기준 개정과 같고 매장의 브랜드 속성만 바뀌었습니다."


def _moves_of(alternative: Any) -> list[dict[str, Any]]:
    raw = (
        alternative.get("moves", [])
        if isinstance(alternative, Mapping)
        else getattr(alternative, "moves", [])
    )
    moves = []
    for move in raw:
        data = asdict(move) if is_dataclass(move) else dict(move)
        if not data.get("brand_id"):
            raise ValueError("every move needs a brand_id")
        moves.append(data)
    return moves


def _alt_id(alternative: Any) -> str:
    value = (
        alternative.get("alt_id")
        if isinstance(alternative, Mapping)
        else getattr(alternative, "alt_id", None)
    )
    return str(value) if value else "ALT-1"


def _set_brand(unit: Entity, brand: dict[str, Any] | None) -> None:
    attrs = unit.attributes
    if brand is None:
        attrs.update({"brand_id": None, "brand_name": None, "vacant": True})
        return
    attrs.update({"brand_id": brand["brand_id"], "brand_name": brand.get("brand_name"), "vacant": False})
    if brand.get("category"):
        attrs["category"] = brand["category"]
        attrs["category_name"] = category_name(brand["category"])


def to_hypothetical_revision(
    base_model: SpatialModel, alternative: Any, rev_id: str | None = None
) -> tuple[SpatialModel, ChangeSet]:
    """Copy ``base_model`` as hypothetical revision ``rev_id`` (default ``H-<n>`` from ``ALT-<n>``) with
    the alternative's brand moves applied, plus the ChangeSet from the base revision to it."""
    moves = _moves_of(alternative)
    alt_id = _alt_id(alternative)
    if rev_id is None:
        match = re.search(r"(\d+)$", alt_id)
        rev_id = f"H-{match.group(1) if match else 1}"
    model = copy.deepcopy(base_model)
    base_rev = base_model.revision.rev_id
    model.revision = Revision(
        rev_id=rev_id,
        kind=RevisionKind.HYPOTHETICAL,
        parent_rev_id=base_rev,
        effective_date=None,
        source=f"cad2anything.layout {alt_id}",
        notes=f"레이아웃 대안 {alt_id} 기반 가상 개정({ESTIMATE_LABEL} 기반, 실제 도면 아님)",
    )
    units = {str(u.attributes.get("unit_code") or u.entity_id): u for u in model.units()}
    base_brand = {
        code: {
            "brand_id": unit.attributes.get("brand_id"),
            "brand_name": unit.attributes.get("brand_name"),
            "category": unit.attributes.get("category"),
        }
        for code, unit in units.items()
    }
    occupant: dict[str, dict[str, Any] | None] = {
        code: (dict(info) if info["brand_id"] else None) for code, info in base_brand.items()
    }
    seen: set[str] = set()
    for move in moves:
        brand_id = str(move["brand_id"])
        if brand_id in seen:
            raise ValueError(f"brand {brand_id} moves more than once")
        seen.add(brand_id)
        for key in ("from_unit", "to_unit"):
            code = move.get(key)
            if code is not None and code not in units:
                raise ValueError(f"unknown unit {code} in move of {brand_id}")
        source = move.get("from_unit")
        if source is not None and base_brand[source]["brand_id"] != brand_id:
            raise ValueError(f"{brand_id} is not in {source} in revision {base_rev}")
        origin = base_brand[source] if source is not None else {}
        move["brand_name"] = move.get("brand_name") or origin.get("brand_name")
        move["category"] = move.get("category") or origin.get("category")
    for move in moves:
        if move.get("from_unit") is not None:
            occupant[move["from_unit"]] = None
    for move in moves:
        if move.get("to_unit") is not None:
            occupant[move["to_unit"]] = {k: move.get(k) for k in ("brand_id", "brand_name", "category")}
    for code, unit in units.items():
        if (occupant[code] or {}).get("brand_id") != base_brand[code]["brand_id"]:
            _set_brand(unit, occupant[code])
    items = [_change_item(move, units, base_brand, alt_id) for move in moves]
    model.issues.append({"severity": "info", "message": HYPOTHETICAL_NOTE, "alternative": alt_id})
    return model, ChangeSet(rev_from=base_rev, rev_to=rev_id, items=items)


def _change_item(
    move: dict[str, Any], units: dict[str, Entity], base_brand: dict[str, dict[str, Any]], alt_id: str
) -> ChangeItem:
    brand_id = str(move["brand_id"])
    name = move.get("brand_name") or brand_id
    category = move.get("category")
    label = f"{name}({category_name(category)})" if category else name
    source, target = move.get("from_unit"), move.get("to_unit")
    delta = move.get("delta")
    effect = f", 기대 매출 {format_krw_delta(float(delta))} {ESTIMATE_LABEL}" if delta is not None else ""
    details = {
        "brand_name": name,
        "category": category,
        "alternative": alt_id,
        "estimated_delta_krw": delta,
        "estimated_delta_low_krw": move.get("delta_low"),
        "estimated_delta_high_krw": move.get("delta_high"),
        "explanation": move.get("explanation"),
    }
    if source is not None and target is not None:
        floor = units[source].floor or ""
        to_floor = units[target].floor or ""
        floor_note = f"({floor}→{to_floor} 층간 이동)" if floor != to_floor else ""
        details.update(
            {
                "area_before_m2": round(units[source].geometry.area, 2),
                "area_after_m2": round(units[target].geometry.area, 2),
                "floor_after": to_floor,
                "vacated_unit_code": source,
                "displaced_brand_id": base_brand[target]["brand_id"],
            }
        )
        return ChangeItem(
            ChangeType.MOVE,
            floor,
            unit_codes_before=[source],
            unit_codes_after=[target],
            brand_ids_before=[brand_id],
            brand_ids_after=[brand_id],
            entity_ids_before=[units[source].entity_id],
            entity_ids_after=[units[target].entity_id],
            details=details,
            summary=f"{floor} {label} 매장을 {source}에서 {target}로 이전{floor_note} (가상 대안 {alt_id}{effect})",
        )
    code = target if target is not None else source
    if code is None:
        raise ValueError(f"move of {brand_id} needs from_unit or to_unit")
    unit = units[code]
    previous = base_brand[code]["brand_id"]
    entering = target is not None
    return ChangeItem(
        ChangeType.REASSIGN,
        unit.floor or "",
        unit_codes_before=[code],
        unit_codes_after=[code],
        brand_ids_before=[previous] if previous else [],
        brand_ids_after=[brand_id] if entering else [],
        entity_ids_before=[unit.entity_id],
        entity_ids_after=[unit.entity_id],
        details=details,
        summary=(
            f"{unit.floor} {code} 브랜드 {'입점' if entering else '퇴점'}: {label} (가상 대안 {alt_id}{effect})"
        ),
    )
