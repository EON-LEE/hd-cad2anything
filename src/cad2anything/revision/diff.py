"""Change detection between two parsed revisions (no shared truth IDs required).

Units are matched per floor by unit code and geometry:

- SPLIT: one before unit covered by two or more after units that each lie (>= 90 %) inside it.
- MERGE: two or more before units lying (>= 90 %) inside one after unit that they cover.
- Same unit: same code (or mutual best IoU >= 0.5 when the code changed); RESIZE when the area changes by more
  than 0.5 m² or the IoU drops below 0.98.
- ADD / REMOVE: after / before units without any counterpart.
- MOVE: a brand leaves one unit and appears in another unit that is not its split/merge successor.
- REASSIGN: same unit, different brand that did not move in from another unit (vacancy counts as no brand).
- FIXTURE_CHANGE: fixture type multiset changed on a unit whose geometry and brand are unchanged.
- WALKWAY_CHANGE: the floor's walkway union changed beyond tolerance; one item per cluster of changed area,
  clustered by the walkway polygons it touches.

Usage::

    uv run python -m cad2anything.revision.diff --before parsed_R0.json --after parsed_R1.json --out changes.json
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad2anything.catalog import FIXTURE_TYPES, FLOOR_PROGRAMS
from cad2anything.model import (
    ChangeItem,
    ChangeSet,
    ChangeType,
    Entity,
    EntityKind,
    RelationType,
    SpatialModel,
)

AREA_TOL_M2 = 0.5
IOU_TOL = 0.98
COVER_RATIO = 0.9
RENAME_IOU = 0.5
WALKWAY_TOL_M2 = 1.0
WALKWAY_PIECE_M2 = 0.5
_TYPE_ORDER = {t: i for i, t in enumerate(ChangeType)}
_CATALOG_ORDER = {p.code: p.ordinal for p in FLOOR_PROGRAMS}


def _normalize(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def brand_key(attributes: Mapping[str, Any]) -> str | None:
    """Brand identity of a unit: normalized brand name, else brand ID; None when vacant."""
    if attributes.get("vacant") is True:
        return None
    name = attributes.get("brand_name")
    if name:
        return "name:" + _normalize(str(name))
    brand_id = attributes.get("brand_id")
    return f"id:{brand_id}" if brand_id else None


@dataclass
class _Unit:
    entity: Entity
    code: str
    floor: str
    brand: str | None
    fixtures: list[Entity]

    @property
    def id(self) -> str:
        return self.entity.entity_id

    @property
    def geom(self) -> BaseGeometry:
        return self.entity.geometry

    @property
    def area(self) -> float:
        return float(self.entity.geometry.area)

    @property
    def brand_name(self) -> str | None:
        return self.entity.attributes.get("brand_name") if self.brand else None

    @property
    def brand_id(self) -> str | None:
        return self.entity.attributes.get("brand_id") if self.brand else None

    def label(self) -> str:
        return f"{self.code}({self.brand_name or '공실'})"

    def type_counts(self) -> Counter[str]:
        return Counter(str(f.attributes.get("fixture_type")) for f in self.fixtures)


def _as_model(model: SpatialModel | Mapping[str, Any]) -> SpatialModel:
    return model if isinstance(model, SpatialModel) else SpatialModel.from_dict(dict(model))


def _collect_units(model: SpatialModel) -> list[_Unit]:
    by_unit: dict[tuple[str | None, str], list[Entity]] = defaultdict(list)
    unit_codes = {u.entity_id: str(u.attributes.get("unit_code") or u.entity_id) for u in model.units()}
    owners = {
        r.to_id: r.from_id
        for r in model.relations
        if r.type == RelationType.CONTAINS and r.from_id in unit_codes
    }
    for fixture in model.by_kind(EntityKind.FIXTURE):
        code = fixture.attributes.get("unit_code") or unit_codes.get(owners.get(fixture.entity_id, ""))
        if code:
            by_unit[(fixture.floor, str(code))].append(fixture)
    return [
        _Unit(
            u,
            unit_codes[u.entity_id],
            u.floor or "",
            brand_key(u.attributes),
            by_unit.get((u.floor, unit_codes[u.entity_id]), []),
        )
        for u in model.units()
    ]


def _iou(inter: float, a: float, b: float) -> float:
    union = a + b - inter
    return inter / union if union > 0 else 0.0


@dataclass
class _FloorMatch:
    pairs: list[tuple[_Unit, _Unit]]
    renamed: list[tuple[_Unit, _Unit]]
    splits: list[tuple[_Unit, list[_Unit]]]
    merges: list[tuple[list[_Unit], _Unit]]
    removed: list[_Unit]
    added: list[_Unit]


def _match_floor(before: Sequence[_Unit], after: Sequence[_Unit]) -> _FloorMatch:
    inter: dict[tuple[int, int], float] = {}
    if before and after:
        tree = STRtree([a.geom for a in after])
        for i, b in enumerate(before):
            for j in tree.query(b.geom):
                area = b.geom.intersection(after[int(j)].geom).area
                if area > 1e-6:
                    inter[(i, int(j))] = area
    by_before: dict[int, list[int]] = defaultdict(list)
    by_after: dict[int, list[int]] = defaultdict(list)
    for i, j in inter:
        by_before[i].append(j)
        by_after[j].append(i)
    used_b: set[int] = set()
    used_a: set[int] = set()
    splits: list[tuple[_Unit, list[_Unit]]] = []
    for i, b in enumerate(before):
        children = [j for j in by_before[i] if inter[(i, j)] >= COVER_RATIO * after[j].area]
        if len(children) >= 2 and sum(inter[(i, j)] for j in children) >= COVER_RATIO * b.area:
            splits.append((b, sorted((after[j] for j in children), key=lambda u: u.code)))
            used_b.add(i)
            used_a.update(children)
    merges: list[tuple[list[_Unit], _Unit]] = []
    for j, a in enumerate(after):
        if j in used_a:
            continue
        parents = [
            i for i in by_after[j] if i not in used_b and inter[(i, j)] >= COVER_RATIO * before[i].area
        ]
        if len(parents) >= 2 and sum(inter[(i, j)] for i in parents) >= COVER_RATIO * a.area:
            merges.append((sorted((before[i] for i in parents), key=lambda u: u.code), a))
            used_b.update(parents)
            used_a.add(j)
    after_by_code = {a.code: j for j, a in enumerate(after) if j not in used_a}
    pairs: list[tuple[_Unit, _Unit]] = []
    for i, b in enumerate(before):
        j = after_by_code.get(b.code)
        if i in used_b or j is None or j in used_a:
            continue
        if inter.get((i, j), 0.0) >= 0.2 * min(b.area, after[j].area):
            pairs.append((b, after[j]))
            used_b.add(i)
            used_a.add(j)
    renamed: list[tuple[_Unit, _Unit]] = []
    candidates = sorted(
        ((_iou(area, before[i].area, after[j].area), i, j) for (i, j), area in inter.items()), reverse=True
    )
    for value, i, j in candidates:
        if value < RENAME_IOU:
            break
        if i in used_b or j in used_a:
            continue
        pairs.append((before[i], after[j]))
        renamed.append((before[i], after[j]))
        used_b.add(i)
        used_a.add(j)
    removed = [b for i, b in enumerate(before) if i not in used_b]
    added = [a for j, a in enumerate(after) if j not in used_a]
    return _FloorMatch(pairs, renamed, splits, merges, removed, added)


def _type_text(counts: Counter[str]) -> str:
    return ", ".join(
        f"{FIXTURE_TYPES[t].name_ko if t in FIXTURE_TYPES else t} {n}" for t, n in sorted(counts.items())
    )


class _Differ:
    def __init__(self, before: SpatialModel, after: SpatialModel) -> None:
        self.before = before
        self.after = after
        self.items: list[ChangeItem] = []
        ordinals = {f.code: f.ordinal for f in [*before.floors, *after.floors]}
        self.floor_order = lambda code: (ordinals.get(code, _CATALOG_ORDER.get(code, 99)), code)
        self.before_units = _collect_units(before)
        self.after_units = _collect_units(after)

    def add(self, change_type: ChangeType, floor: str, summary: str, **fields: Any) -> None:
        self.items.append(ChangeItem(change_type, floor, summary=summary, **fields))

    @staticmethod
    def _brand_ids(units: Sequence[_Unit]) -> list[str]:
        return [str(u.brand_id) for u in units if u.brand_id]

    def _space_overlap(self, model: SpatialModel, floor: str, geom: BaseGeometry) -> dict[str, Any]:
        walkway = sum(w.geometry.intersection(geom).area for w in model.by_kind(EntityKind.WALKWAY, floor))
        amenities = [
            (a.attributes.get("amenity_type"), a.geometry.intersection(geom).area)
            for a in model.by_kind(EntityKind.AMENITY, floor)
        ]
        amenities = [(t, area) for t, area in amenities if area > AREA_TOL_M2]
        return {
            "walkway_m2": round(walkway, 2),
            "amenity_m2": round(sum(area for _, area in amenities), 2),
            "amenity_types": sorted({str(t) for t, _ in amenities}),
        }

    def run(self) -> list[ChangeItem]:
        floors = sorted(
            {u.floor for u in self.before_units} | {u.floor for u in self.after_units}, key=self.floor_order
        )
        matches = {
            floor: _match_floor(
                [u for u in self.before_units if u.floor == floor],
                [u for u in self.after_units if u.floor == floor],
            )
            for floor in floors
        }
        succ: dict[str, list[_Unit]] = defaultdict(list)
        pred: dict[str, list[_Unit]] = defaultdict(list)
        for match in matches.values():
            for b, a in match.pairs:
                succ[b.id].append(a)
                pred[a.id].append(b)
            for b, children in match.splits:
                for a in children:
                    succ[b.id].append(a)
                    pred[a.id].append(b)
            for parents, a in match.merges:
                for b in parents:
                    succ[b.id].append(a)
                    pred[a.id].append(b)
        moves = self._moves(succ, pred)
        move_origins = {b.id for b, _ in moves}
        move_dests = {a.id for _, a in moves}
        for b, a in moves:
            self._move_item(b, a, succ, pred)
        for floor in floors:
            match = matches[floor]
            resized = set()
            for b, a in match.pairs:
                if self._resize(b, a, floor, match):
                    resized.add(a.id)
                self._reassign(b, a, floor, move_origins, move_dests)
                if a.id not in resized and b.brand == a.brand:
                    self._fixture_change(b, a, floor)
            for b, children in match.splits:
                self._split(b, children, floor)
            for parents, a in match.merges:
                self._merge(parents, a, floor)
            for b in match.removed:
                self._remove(b, floor)
            for a in match.added:
                self._add_unit(a, floor)
            self._walkways(floor)
        self.items.sort(
            key=lambda i: (
                self.floor_order(i.floor),
                _TYPE_ORDER[i.change_type],
                i.unit_codes_before,
                i.unit_codes_after,
            )
        )
        return self.items

    # -- brands ---------------------------------------------------------------------------------------
    def _moves(
        self, succ: Mapping[str, list[_Unit]], pred: Mapping[str, list[_Unit]]
    ) -> list[tuple[_Unit, _Unit]]:
        before_by_brand: dict[str, list[_Unit]] = defaultdict(list)
        after_by_brand: dict[str, list[_Unit]] = defaultdict(list)
        for u in self.before_units:
            if u.brand:
                before_by_brand[u.brand].append(u)
        for u in self.after_units:
            if u.brand:
                after_by_brand[u.brand].append(u)
        moves: list[tuple[_Unit, _Unit]] = []
        for brand in sorted(set(before_by_brand) & set(after_by_brand)):
            departures = [
                b for b in before_by_brand[brand] if not any(a.brand == brand for a in succ.get(b.id, []))
            ]
            arrivals = [
                a for a in after_by_brand[brand] if not any(b.brand == brand for b in pred.get(a.id, []))
            ]
            options = sorted(
                ((b, a) for b in departures for a in arrivals),
                key=lambda p: (
                    p[0].floor != p[1].floor,
                    p[0].geom.centroid.distance(p[1].geom.centroid),
                    p[0].id,
                    p[1].id,
                ),
            )
            taken_b: set[str] = set()
            taken_a: set[str] = set()
            for b, a in options:
                if b.id not in taken_b and a.id not in taken_a:
                    moves.append((b, a))
                    taken_b.add(b.id)
                    taken_a.add(a.id)
        return moves

    def _move_item(
        self, b: _Unit, a: _Unit, succ: Mapping[str, list[_Unit]], pred: Mapping[str, list[_Unit]]
    ) -> None:
        origin_now = succ.get(b.id, [])
        dest_before = pred.get(a.id, [])
        notes = []
        if origin_now and all(u.brand is None for u in origin_now):
            notes.append(f"{b.code} 공실 전환")
        previous = [u.brand_name for u in dest_before if u.brand_name]
        if previous:
            notes.append(f"{a.code} 기존 {', '.join(previous)} 자리")
        elif dest_before:
            notes.append(f"{a.code} 기존 공실")
        brand_ids = self._brand_ids([b]) or self._brand_ids([a])
        self.add(
            ChangeType.MOVE,
            b.floor,
            f"{b.floor} {b.brand_name} 매장을 {b.code}에서 {a.code}로 이전"
            + (f" ({'; '.join(notes)})" if notes else ""),
            unit_codes_before=[b.code],
            unit_codes_after=[a.code],
            brand_ids_before=brand_ids,
            brand_ids_after=brand_ids,
            entity_ids_before=[b.id],
            entity_ids_after=[a.id],
            details={
                "brand_name": b.brand_name,
                "floor_before": b.floor,
                "floor_after": a.floor,
                "area_before_m2": round(b.area, 2),
                "area_after_m2": round(a.area, 2),
                "fixture_count_before": len(b.fixtures),
                "fixture_count_after": len(a.fixtures),
                "vacated_unit_code": b.code
                if origin_now and all(u.brand is None for u in origin_now)
                else None,
                "destination_previous_brands": previous,
            },
        )

    def _reassign(self, b: _Unit, a: _Unit, floor: str, origins: set[str], dests: set[str]) -> None:
        if b.brand == a.brand:
            return
        if a.brand is not None and a.id in dests:
            return
        if a.brand is None and (b.brand is None or b.id in origins):
            return
        old, new = b.brand_name or "(공실)", a.brand_name or "(공실)"
        if a.brand is None:
            note = " (퇴점·공실 전환)"
        elif b.brand is None:
            note = " (공실 신규 입점)"
        else:
            note = ""
        category_before, category_after = (
            b.entity.attributes.get("category"),
            a.entity.attributes.get("category"),
        )
        self.add(
            ChangeType.REASSIGN,
            floor,
            f"{floor} {a.code} 브랜드 교체: {old} → {new}{note}",
            unit_codes_before=[b.code],
            unit_codes_after=[a.code],
            brand_ids_before=self._brand_ids([b]),
            brand_ids_after=self._brand_ids([a]),
            entity_ids_before=[b.id],
            entity_ids_after=[a.id],
            details={
                "brand_name_before": b.brand_name,
                "brand_name_after": a.brand_name,
                "category_before": category_before,
                "category_after": category_after,
                "fixture_count_before": len(b.fixtures),
                "fixture_count_after": len(a.fixtures),
            },
        )

    # -- geometry -------------------------------------------------------------------------------------
    def _resize(self, b: _Unit, a: _Unit, floor: str, match: _FloorMatch) -> bool:
        inter = b.geom.intersection(a.geom).area
        value = _iou(inter, b.area, a.area)
        delta = a.area - b.area
        if abs(delta) <= AREA_TOL_M2 and value >= IOU_TOL:
            return False
        changed = b.geom.symmetric_difference(a.geom)
        neighbors = sorted(
            {
                u.code
                for x, y in match.pairs
                for u in (x, y)
                if u.code != a.code and u.geom.intersection(changed).area > AREA_TOL_M2
            }
        )
        walkway = (
            self._space_overlap(self.after, floor, changed)["walkway_m2"]
            + self._space_overlap(self.before, floor, changed)["walkway_m2"]
        )
        context = []
        if neighbors:
            context.append(f"인접 {', '.join(neighbors)}")
        if walkway > AREA_TOL_M2:
            context.append("통로 경계 변경")
        self.add(
            ChangeType.RESIZE,
            floor,
            f"{floor} {a.code} 면적 변경: {b.area:.1f}㎡ → {a.area:.1f}㎡ ({delta:+.1f}㎡"
            + (f", {'; '.join(context)}" if context else "")
            + ")",
            unit_codes_before=[b.code],
            unit_codes_after=[a.code],
            brand_ids_before=self._brand_ids([b]),
            brand_ids_after=self._brand_ids([a]),
            entity_ids_before=[b.id],
            entity_ids_after=[a.id],
            details={
                "area_before_m2": round(b.area, 2),
                "area_after_m2": round(a.area, 2),
                "area_delta_m2": round(delta, 2),
                "iou": round(value, 4),
                "neighbor_unit_codes": neighbors,
                "walkway_overlap_m2": round(walkway, 2),
                "fixture_count_before": len(b.fixtures),
                "fixture_count_after": len(a.fixtures),
            },
        )
        return True

    def _split(self, b: _Unit, children: Sequence[_Unit], floor: str) -> None:
        self.add(
            ChangeType.SPLIT,
            floor,
            f"{floor} {b.label()} 분할 → {', '.join(c.label() for c in children)}",
            unit_codes_before=[b.code],
            unit_codes_after=[c.code for c in children],
            brand_ids_before=self._brand_ids([b]),
            brand_ids_after=self._brand_ids(children),
            entity_ids_before=[b.id],
            entity_ids_after=[c.id for c in children],
            details={
                "area_before_m2": round(b.area, 2),
                "area_after_m2": [round(c.area, 2) for c in children],
                "vacant_unit_codes_after": [c.code for c in children if c.brand is None],
                "fixture_count_before": len(b.fixtures),
                "fixture_count_after": sum(len(c.fixtures) for c in children),
            },
        )

    def _merge(self, parents: Sequence[_Unit], a: _Unit, floor: str) -> None:
        kept = a.brand
        self.add(
            ChangeType.MERGE,
            floor,
            f"{floor} {'·'.join(p.code for p in parents)} 병합 → {a.label()}",
            unit_codes_before=[p.code for p in parents],
            unit_codes_after=[a.code],
            brand_ids_before=self._brand_ids(parents),
            brand_ids_after=self._brand_ids([a]),
            entity_ids_before=[p.id for p in parents],
            entity_ids_after=[a.id],
            details={
                "area_before_m2": [round(p.area, 2) for p in parents],
                "area_after_m2": round(a.area, 2),
                "kept_brand_name": a.brand_name,
                "departed_brand_names": [p.brand_name for p in parents if p.brand and p.brand != kept],
                "fixture_count_before": sum(len(p.fixtures) for p in parents),
                "fixture_count_after": len(a.fixtures),
            },
        )

    def _remove(self, b: _Unit, floor: str) -> None:
        converted = self._space_overlap(self.after, floor, b.geom)
        parts = []
        if converted["walkway_m2"] > AREA_TOL_M2:
            parts.append(f"통로 {converted['walkway_m2']:.1f}㎡")
        if converted["amenity_m2"] > AREA_TOL_M2:
            parts.append(f"편의시설({', '.join(converted['amenity_types'])}) {converted['amenity_m2']:.1f}㎡")
        self.add(
            ChangeType.REMOVE,
            floor,
            f"{floor} 매장 {b.label()} 철거" + (f" → {' + '.join(parts)} 전환" if parts else ""),
            unit_codes_before=[b.code],
            brand_ids_before=self._brand_ids([b]),
            entity_ids_before=[b.id],
            details={
                "area_before_m2": round(b.area, 2),
                "brand_name": b.brand_name,
                "fixture_count_before": len(b.fixtures),
                "converted_to": converted,
            },
        )

    def _add_unit(self, a: _Unit, floor: str) -> None:
        previous = self._space_overlap(self.before, floor, a.geom)
        note = (
            f" (기존 통로 {previous['walkway_m2']:.1f}㎡ 전용)"
            if previous["walkway_m2"] > AREA_TOL_M2
            else ""
        )
        self.add(
            ChangeType.ADD,
            floor,
            f"{floor} 매장 {a.label()} 신설, {a.area:.1f}㎡{note}",
            unit_codes_after=[a.code],
            brand_ids_after=self._brand_ids([a]),
            entity_ids_after=[a.id],
            details={
                "area_after_m2": round(a.area, 2),
                "brand_name": a.brand_name,
                "category": a.entity.attributes.get("category"),
                "fixture_count_after": len(a.fixtures),
                "previous_use": previous,
            },
        )

    def _fixture_change(self, b: _Unit, a: _Unit, floor: str) -> None:
        before_counts, after_counts = b.type_counts(), a.type_counts()
        if before_counts == after_counts:
            return
        added_types = after_counts - before_counts
        removed_types = before_counts - after_counts
        removed_ids, added_ids = self._fixture_delta(b.fixtures, a.fixtures)
        parts = []
        if added_types:
            parts.append(f"추가 {_type_text(added_types)}")
        if removed_types:
            parts.append(f"철거 {_type_text(removed_types)}")
        self.add(
            ChangeType.FIXTURE_CHANGE,
            floor,
            f"{floor} {a.label()} 집기 변경: {len(b.fixtures)}개 → {len(a.fixtures)}개 ({'; '.join(parts)})",
            unit_codes_before=[b.code],
            unit_codes_after=[a.code],
            brand_ids_before=self._brand_ids([b]),
            brand_ids_after=self._brand_ids([a]),
            entity_ids_before=removed_ids,
            entity_ids_after=added_ids,
            details={
                "fixture_count_before": len(b.fixtures),
                "fixture_count_after": len(a.fixtures),
                "added_fixture_types": sorted(added_types.elements()),
                "removed_fixture_types": sorted(removed_types.elements()),
            },
        )

    @staticmethod
    def _fixture_delta(
        before: Sequence[Entity], after: Sequence[Entity], tol: float = 0.3
    ) -> tuple[list[str], list[str]]:
        """Fixture entity IDs only in ``before`` / only in ``after`` (same type within ``tol`` meters)."""
        remaining = list(after)
        removed = []
        for fixture in before:
            center = fixture.geometry.centroid
            match = next(
                (
                    other
                    for other in sorted(remaining, key=lambda o: o.geometry.centroid.distance(center))
                    if other.attributes.get("fixture_type") == fixture.attributes.get("fixture_type")
                    and other.geometry.centroid.distance(center) <= tol
                ),
                None,
            )
            if match is None:
                removed.append(fixture.entity_id)
            else:
                remaining.remove(match)
        return removed, [f.entity_id for f in remaining]

    # -- walkways -------------------------------------------------------------------------------------
    def _walkways(self, floor: str) -> None:
        before = self.before.by_kind(EntityKind.WALKWAY, floor)
        after = self.after.by_kind(EntityKind.WALKWAY, floor)
        union_b = unary_union([w.geometry for w in before]) if before else Polygon()
        union_a = unary_union([w.geometry for w in after]) if after else Polygon()
        added = union_a.difference(union_b)
        removed = union_b.difference(union_a)
        if added.area + removed.area <= WALKWAY_TOL_M2:
            return
        pieces = [(p, 1) for p in getattr(added, "geoms", [added]) if p.area > WALKWAY_PIECE_M2]
        pieces += [(p, -1) for p in getattr(removed, "geoms", [removed]) if p.area > WALKWAY_PIECE_M2]
        keys: list[set[tuple[str, int]]] = []
        for piece, _ in pieces:
            touched = {("b", i) for i, w in enumerate(before) if w.geometry.intersection(piece).area > 1e-3}
            touched |= {("a", j) for j, w in enumerate(after) if w.geometry.intersection(piece).area > 1e-3}
            keys.append(touched)
        parent = list(range(len(pieces)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(pieces)):
            for j in range(i + 1, len(pieces)):
                if keys[i] & keys[j]:
                    parent[find(j)] = find(i)
        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(pieces)):
            clusters[find(i)].append(i)
        for members in sorted(clusters.values(), key=lambda m: _order(pieces[m[0]][0])):
            plus = sum(pieces[i][0].area for i in members if pieces[i][1] > 0)
            minus = sum(pieces[i][0].area for i in members if pieces[i][1] < 0)
            region = unary_union([pieces[i][0] for i in members])
            touched = set().union(*(keys[i] for i in members))
            units = sorted(
                {
                    u.code
                    for u in [*self.before_units, *self.after_units]
                    if u.floor == floor and u.geom.intersection(region).area > AREA_TOL_M2
                }
            )
            self.add(
                ChangeType.WALKWAY_CHANGE,
                floor,
                f"{floor} 보행 통로 변경: +{plus:.1f}㎡ / -{minus:.1f}㎡"
                + (f" (관련 매장 {', '.join(units)})" if units else ""),
                entity_ids_before=sorted({before[i].entity_id for side, i in touched if side == "b"}),
                entity_ids_after=sorted({after[j].entity_id for side, j in touched if side == "a"}),
                details={
                    "added_m2": round(plus, 2),
                    "removed_m2": round(minus, 2),
                    "floor_walkway_area_before_m2": round(union_b.area, 2),
                    "floor_walkway_area_after_m2": round(union_a.area, 2),
                    "related_unit_codes": units,
                    "centroid": [round(region.centroid.x, 2), round(region.centroid.y, 2)],
                },
            )


def _order(geom: BaseGeometry) -> tuple[float, float]:
    point = geom.centroid
    return (round(point.y, 1), round(point.x, 1))


def diff_models(
    before: SpatialModel | Mapping[str, Any], after: SpatialModel | Mapping[str, Any]
) -> ChangeSet:
    """Change set between two (parsed) models of consecutive revisions."""
    before, after = _as_model(before), _as_model(after)
    items = _Differ(before, after).run()
    return ChangeSet(before.revision.rev_id, after.revision.rev_id, items)


# --- evaluation --------------------------------------------------------------------------------------


def _as_changes(changes: ChangeSet | Mapping[str, Any]) -> ChangeSet:
    return changes if isinstance(changes, ChangeSet) else ChangeSet.from_dict(dict(changes))


def items_match(predicted: ChangeItem, truth: ChangeItem) -> bool:
    """Same type and floor and overlapping unit codes (MOVE: both origin and destination overlap).

    Walkway changes carry no unit codes and match on type and floor.
    """
    if predicted.change_type != truth.change_type or predicted.floor != truth.floor:
        return False
    if predicted.change_type == ChangeType.WALKWAY_CHANGE:
        return True
    pb, pa = set(predicted.unit_codes_before), set(predicted.unit_codes_after)
    tb, ta = set(truth.unit_codes_before), set(truth.unit_codes_after)
    if predicted.change_type == ChangeType.MOVE:
        return bool(pb & tb) and bool(pa & ta)
    return bool((pb | pa) & (tb | ta))


def evaluate_diff(
    predicted: ChangeSet | Mapping[str, Any], truth: ChangeSet | Mapping[str, Any]
) -> dict[str, Any]:
    """Per-type precision/recall with tolerant matching (see ``items_match``) plus exact-match counts."""
    predicted, truth = _as_changes(predicted), _as_changes(truth)
    per_type: dict[str, dict[str, Any]] = {}
    totals = Counter()
    missed: list[str] = []
    false_positives: list[str] = []
    for change_type in ChangeType:
        preds = [i for i in predicted.items if i.change_type == change_type]
        truths = [i for i in truth.items if i.change_type == change_type]
        if not preds and not truths:
            continue
        found = [t for t in truths if any(items_match(p, t) for p in preds)]
        correct = [p for p in preds if any(items_match(p, t) for t in truths)]
        exact = sum(
            any(
                p.floor == t.floor
                and sorted(p.unit_codes_before) == sorted(t.unit_codes_before)
                and sorted(p.unit_codes_after) == sorted(t.unit_codes_after)
                for p in preds
            )
            for t in truths
        )
        missed += [t.summary for t in truths if t not in found]
        false_positives += [p.summary for p in preds if p not in correct]
        per_type[str(change_type)] = {
            "predicted": len(preds),
            "truth": len(truths),
            "true_positive_predicted": len(correct),
            "true_positive_truth": len(found),
            "exact_truth_matches": exact,
            "precision": round(len(correct) / len(preds), 4) if preds else None,
            "recall": round(len(found) / len(truths), 4) if truths else None,
        }
        totals.update(
            {"predicted": len(preds), "truth": len(truths), "tp_pred": len(correct), "tp_truth": len(found)}
        )
    return {
        "rev_from": predicted.rev_from,
        "rev_to": predicted.rev_to,
        "overall": {
            "predicted": totals["predicted"],
            "truth": totals["truth"],
            "precision": round(totals["tp_pred"] / totals["predicted"], 4) if totals["predicted"] else None,
            "recall": round(totals["tp_truth"] / totals["truth"], 4) if totals["truth"] else None,
        },
        "per_type": per_type,
        "types_detected": sorted(t for t, row in per_type.items() if row["true_positive_predicted"]),
        "missed": missed,
        "false_positives": false_positives,
    }


# --- CLI ---------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect changes between two parsed revisions.")
    parser.add_argument(
        "--before", type=Path, required=True, help="parsed SpatialModel JSON (earlier revision)"
    )
    parser.add_argument("--after", type=Path, required=True, help="parsed SpatialModel JSON (later revision)")
    parser.add_argument("--out", type=Path, required=True, help="output ChangeSet JSON")
    parser.add_argument("--truth", type=Path, default=None, help="truth ChangeSet JSON to evaluate against")
    args = parser.parse_args(argv)
    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    changes = diff_models(before, after)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(changes.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    summary: dict[str, Any] = {
        "rev_from": changes.rev_from,
        "rev_to": changes.rev_to,
        "out": str(args.out),
        "items": len(changes.items),
        "by_type": dict(Counter(str(i.change_type) for i in changes.items)),
    }
    if args.truth:
        truth = json.loads(args.truth.read_text(encoding="utf-8"))
        summary["evaluation"] = evaluate_diff(changes, truth)
    print(json.dumps(summary, ensure_ascii=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
