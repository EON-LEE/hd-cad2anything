"""Deterministic MD re-layout revisions (R1, R2, H1) with ground-truth change sets.

Storyline:

- R1 (S/S, from R0): 3F brand moved from a low-exposure unit to the unit in front of ESC-A, 1F cosmetics
  brand replaced, 3F merge, 6F split, 2F boundary shift, 4F unit turned into a rest walkway + lounge,
  4F atrium popup, B1 fixture changes, 5F central corridor widened by 1 m.
- R2 (F/W, from R1): 3F category mixing (WOMEN_CONTEMPORARY -> ACCESSORIES/CAFE), 7F merge, 8F restaurant
  replaced, 6F KIDS brand moved, 1F cosmetics fixture changes. The 4F east band is never touched.
- H1 (hypothetical, from R2): swap of two 3F brands and one 3F unit enlarged into back-of-house space.
"""

from __future__ import annotations

import copy
from typing import Any

import shapely
from shapely.geometry import Polygon

from cad2anything.catalog import CATEGORIES
from cad2anything.mockgen.brands import Brand
from cad2anything.mockgen.layout import (
    ESCALATOR_FRONT_POINTS,
    MIN_UNIT_WIDTH_M,
    REVISION_DATES,
    WalkwaySpec,
    amenity_entity,
    escalator_distance,
    finalize_model,
    find_fixture_spot,
    fixture_entity,
    fixtures_of,
    make_unit,
    rect_of,
    rect_poly,
    refresh_unit_fixtures,
    round_rect,
    set_unit_brand,
    set_unit_rect,
    unit_at,
    walkway_entity,
)
from cad2anything.model import (
    ChangeItem,
    ChangeSet,
    ChangeType,
    Entity,
    EntityKind,
    Floor,
    Revision,
    RevisionKind,
    SpatialModel,
)

Rect = tuple[float, float, float, float]


def clone_model(parent: SpatialModel, revision: Revision) -> SpatialModel:
    model = SpatialModel(revision=revision, floors=[Floor(**f.to_dict()) for f in parent.floors])
    for e in parent.entities.values():
        model.add(Entity(e.entity_id, e.kind, e.floor, e.geometry, e.label, copy.deepcopy(e.attributes)))
    return model


def _code(unit: Entity) -> str:
    return unit.attributes["unit_code"]


def _merge_rects(a: Rect, b: Rect, facing: str) -> Rect | None:
    """Union of two consecutive units of the same strip row, else None."""
    if facing in ("N", "S"):
        if a[1] == b[1] and a[3] == b[3] and (a[2] == b[0] or b[2] == a[0]):
            return round_rect((min(a[0], b[0]), a[1], max(a[2], b[2]), a[3]))
    elif a[0] == b[0] and a[2] == b[2] and (a[3] == b[1] or b[3] == a[1]):
        return round_rect((a[0], min(a[1], b[1]), a[2], max(a[3], b[3])))
    return None


def _run_width(rect: Rect, facing: str) -> float:
    return rect[2] - rect[0] if facing in ("N", "S") else rect[3] - rect[1]


class _Editor:
    """Applies edits to a cloned model and records ground-truth change items."""

    def __init__(
        self, parent: SpatialModel, revision: Revision, brands: list[Brand], seed: int, used: set[str]
    ) -> None:
        self.parent = parent
        self.model = clone_model(parent, revision)
        self.brands = {b.brand_id: b for b in brands}
        self.brand_list = brands
        self.seed = seed
        self.used = used
        self.items: list[ChangeItem] = []
        self.touched: set[str] = set()

    # -- queries -------------------------------------------------------------------------------
    def units(self, floor: str, category: str | None = None, occupied: bool = True) -> list[Entity]:
        """Units on ``floor`` not yet edited in this revision (optionally by category / occupancy)."""
        return [
            u
            for u in self.model.units(floor)
            if (category is None or u.attributes["category"] == category)
            and (not occupied or not u.attributes["vacant"])
            and _code(u) not in self.touched
        ]

    def brand_of(self, unit: Entity) -> Brand | None:
        brand_id = unit.attributes["brand_id"]
        return self.brands[brand_id] if brand_id else None

    def attractiveness(self, unit: Entity) -> float:
        brand = self.brand_of(unit)
        return brand.attractiveness if brand else 0.0

    def n_fixtures(self, unit: Entity) -> int:
        return len(fixtures_of(self.model, _code(unit)))

    def spare_brand(self, category: str) -> Brand:
        spares = [b for b in self.brand_list if b.category == category and b.brand_id not in self.used]
        if not spares:
            raise ValueError(f"no spare {category} brand left")
        brand = min(spares, key=lambda b: (-b.attractiveness, b.brand_id))
        self.used.add(brand.brand_id)
        return brand

    def adjacent_pairs(self, floor: str, category: str) -> list[tuple[Entity, Entity, Rect]]:
        units = sorted(self.units(floor, category), key=_code)
        pairs = []
        for i, a in enumerate(units):
            for b in units[i + 1 :]:
                same_row = (
                    a.attributes["band"] == b.attributes["band"]
                    and a.attributes["facing"] == b.attributes["facing"]
                )
                merged = _merge_rects(rect_of(a), rect_of(b), a.attributes["facing"]) if same_row else None
                if merged is not None:
                    pairs.append((a, b, merged))
        return pairs

    # -- edits ---------------------------------------------------------------------------------
    def assign(self, unit: Entity, brand: Brand | None) -> None:
        set_unit_brand(unit, brand)
        refresh_unit_fixtures(self.model, unit, self.seed)
        if brand is not None:
            self.used.add(brand.brand_id)
        self.touched.add(_code(unit))

    def reshape(self, unit: Entity, rect: Rect) -> None:
        set_unit_rect(unit, rect)
        refresh_unit_fixtures(self.model, unit, self.seed)
        self.touched.add(_code(unit))

    def remove_unit(self, unit: Entity) -> None:
        for fixture in fixtures_of(self.model, _code(unit)):
            del self.model.entities[fixture.entity_id]
        del self.model.entities[unit.entity_id]
        self.touched.add(_code(unit))

    def add_unit(
        self, floor: str, code: str, rect: Rect, template: dict[str, Any], brand: Brand | None
    ) -> Entity:
        unit = make_unit(floor, code, rect, template["facing"], template["band"], template["category"], brand)
        self.model.add(unit)
        refresh_unit_fixtures(self.model, unit, self.seed)
        if brand is not None:
            self.used.add(brand.brand_id)
        self.touched.add(code)
        return unit

    def walkway(self, floor: str, segment: str) -> Entity:
        for w in self.model.by_kind(EntityKind.WALKWAY, floor):
            if w.attributes.get("segment") == segment:
                return w
        raise KeyError(f"{floor} walkway segment {segment}")

    def add_walkway(self, floor: str, spec: WalkwaySpec) -> Entity:
        numbers = [int(w.entity_id.rsplit("-", 1)[1]) for w in self.model.by_kind(EntityKind.WALKWAY, floor)]
        walkway = walkway_entity(floor, max(numbers) + 1, spec)
        self.model.add(walkway)
        return walkway

    def add_amenity(self, floor: str, amenity_type: str, geometry: Polygon) -> Entity:
        existing = [
            a
            for a in self.model.by_kind(EntityKind.AMENITY, floor)
            if a.attributes["amenity_type"] == amenity_type
        ]
        amenity = amenity_entity(floor, amenity_type, len(existing) + 1, geometry)
        self.model.add(amenity)
        return amenity

    def record(self, change_type: ChangeType, floor: str, summary: str, **fields: Any) -> None:
        self.items.append(ChangeItem(change_type, floor, summary=summary, **fields))

    def finish(self) -> tuple[SpatialModel, ChangeSet]:
        finalize_model(self.model)
        return self.model, ChangeSet(self.parent.revision.rev_id, self.model.revision.rev_id, self.items)

    # -- composite operations ------------------------------------------------------------------
    def move_brand(self, origin: Entity, dest: Entity, reason: str) -> None:
        brand = self.brand_of(origin)
        assert brand is not None and dest.attributes["vacant"]
        n_origin = self.n_fixtures(origin)
        self.assign(dest, brand)
        self.assign(origin, None)
        floor = origin.floor or ""
        self.record(
            ChangeType.MOVE,
            floor,
            f"{floor} {brand.brand_name}({CATEGORIES[brand.category].name_ko}) 매장을 {_code(origin)}에서 "
            f"{_code(dest)}로 이전 ({reason})",
            unit_codes_before=[_code(origin)],
            unit_codes_after=[_code(dest)],
            brand_ids_before=[brand.brand_id],
            brand_ids_after=[brand.brand_id],
            entity_ids_before=[origin.entity_id],
            entity_ids_after=[dest.entity_id],
            details={
                "brand_name": brand.brand_name,
                "category": brand.category,
                "area_before_m2": round(origin.geometry.area, 2),
                "area_after_m2": round(dest.geometry.area, 2),
                "distance_to_escalator_before_m": escalator_distance(origin.geometry),
                "distance_to_escalator_after_m": escalator_distance(dest.geometry),
                "fixture_count_before": n_origin,
                "fixture_count_after": self.n_fixtures(dest),
                "vacated_unit_code": _code(origin),
                "reason": reason,
            },
        )

    def swap_brands(self, a: Entity, b: Entity, reason: str) -> None:
        brand_a, brand_b = self.brand_of(a), self.brand_of(b)
        assert brand_a is not None and brand_b is not None
        n_a, n_b = self.n_fixtures(a), self.n_fixtures(b)
        self.assign(a, brand_b)
        self.assign(b, brand_a)
        floor = a.floor or ""
        for brand, src, dst, n_src in ((brand_a, a, b, n_a), (brand_b, b, a, n_b)):
            self.record(
                ChangeType.MOVE,
                floor,
                f"{floor} {brand.brand_name} 매장을 {_code(src)}에서 {_code(dst)}로 이전 (맞교환, {reason})",
                unit_codes_before=[_code(src)],
                unit_codes_after=[_code(dst)],
                brand_ids_before=[brand.brand_id],
                brand_ids_after=[brand.brand_id],
                entity_ids_before=[src.entity_id],
                entity_ids_after=[dst.entity_id],
                details={
                    "brand_name": brand.brand_name,
                    "category": brand.category,
                    "swap_with_unit_code": _code(dst),
                    "area_before_m2": round(src.geometry.area, 2),
                    "area_after_m2": round(dst.geometry.area, 2),
                    "distance_to_escalator_before_m": escalator_distance(src.geometry),
                    "distance_to_escalator_after_m": escalator_distance(dst.geometry),
                    "fixture_count_before": n_src,
                    "fixture_count_after": self.n_fixtures(dst),
                    "reason": reason,
                },
            )

    def reassign(self, unit: Entity, brand: Brand, reason: str) -> None:
        old = self.brand_of(unit)
        old_category = unit.attributes["category"]
        n_before = self.n_fixtures(unit)
        self.assign(unit, brand)
        floor = unit.floor or ""
        old_name = old.brand_name if old else "(공실)"
        category_note = (
            f", {CATEGORIES[old_category].name_ko}→{CATEGORIES[brand.category].name_ko}"
            if old_category != brand.category
            else ""
        )
        self.record(
            ChangeType.REASSIGN,
            floor,
            f"{floor} {_code(unit)} 브랜드 교체: {old_name} → {brand.brand_name}{category_note} ({reason})",
            unit_codes_before=[_code(unit)],
            unit_codes_after=[_code(unit)],
            brand_ids_before=[old.brand_id] if old else [],
            brand_ids_after=[brand.brand_id],
            entity_ids_before=[unit.entity_id],
            entity_ids_after=[unit.entity_id],
            details={
                "brand_name_before": old.brand_name if old else None,
                "brand_name_after": brand.brand_name,
                "category_before": old_category,
                "category_after": brand.category,
                "fixture_count_before": n_before,
                "fixture_count_after": self.n_fixtures(unit),
                "reason": reason,
            },
        )

    def merge(self, a: Entity, b: Entity, merged_rect: Rect, reason: str) -> Entity:
        brand_a, brand_b = self.brand_of(a), self.brand_of(b)
        keep = max((x for x in (brand_a, brand_b) if x), key=lambda x: (x.attractiveness, x.brand_id))
        departed = [x.brand_id for x in (brand_a, brand_b) if x and x.brand_id != keep.brand_id]
        n_before = self.n_fixtures(a) + self.n_fixtures(b)
        template = dict(a.attributes)
        floor = a.floor or ""
        self.remove_unit(a)
        self.remove_unit(b)
        merged = self.add_unit(floor, f"{_code(a)}M", merged_rect, template, keep)
        self.record(
            ChangeType.MERGE,
            floor,
            f"{floor} {_code(a)}·{_code(b)} 병합 → {_code(merged)} ({keep.brand_name} 확장, {reason})",
            unit_codes_before=[_code(a), _code(b)],
            unit_codes_after=[_code(merged)],
            brand_ids_before=[x.brand_id for x in (brand_a, brand_b) if x],
            brand_ids_after=[keep.brand_id],
            entity_ids_before=[a.entity_id, b.entity_id],
            entity_ids_after=[merged.entity_id],
            details={
                "area_before_m2": [round(a.geometry.area, 2), round(b.geometry.area, 2)],
                "area_after_m2": round(merged.geometry.area, 2),
                "kept_brand_id": keep.brand_id,
                "departed_brand_ids": departed,
                "fixture_count_before": n_before,
                "fixture_count_after": self.n_fixtures(merged),
                "reason": reason,
            },
        )
        return merged

    def resize(self, unit: Entity, rect: Rect, reason: str, summary: str, **extra: Any) -> None:
        area_before, n_before = round(unit.geometry.area, 2), self.n_fixtures(unit)
        self.reshape(unit, rect)
        brand_ids = [unit.attributes["brand_id"]] if unit.attributes["brand_id"] else []
        self.record(
            ChangeType.RESIZE,
            unit.floor or "",
            summary,
            unit_codes_before=[_code(unit)],
            unit_codes_after=[_code(unit)],
            brand_ids_before=brand_ids,
            brand_ids_after=brand_ids,
            entity_ids_before=[unit.entity_id],
            entity_ids_after=[unit.entity_id],
            details={
                "area_before_m2": area_before,
                "area_after_m2": round(unit.geometry.area, 2),
                "fixture_count_before": n_before,
                "fixture_count_after": self.n_fixtures(unit),
                "reason": reason,
                **extra,
            },
        )

    def change_fixtures(self, unit: Entity, add_type: str | None, remove: bool) -> bool:
        """Add one fixture of ``add_type`` and/or remove the last non-counter fixture."""
        fixtures = sorted(fixtures_of(self.model, _code(unit)), key=lambda f: f.entity_id)
        removed: list[Entity] = []
        added: list[Entity] = []
        if remove:
            candidates = [f for f in fixtures if f.attributes["fixture_type"] != "COUNTER"]
            if len(fixtures) < 2 or not candidates:
                return False
            removed.append(candidates[-1])
        if add_type is not None:
            keep = [f for f in fixtures if f not in removed]
            spot = find_fixture_spot(
                rect_of(unit), unit.attributes["facing"], add_type, [rect_of(f) for f in keep]
            )
            if spot is None:
                return False
            index = (
                max(int(f.attributes["fixture_id"].rsplit("-F", 1)[1]) for f in fixtures) + 1
                if fixtures
                else 1
            )
            added.append(fixture_entity(unit.floor or "", _code(unit), index, spot))
        for fixture in removed:
            del self.model.entities[fixture.entity_id]
        for fixture in added:
            self.model.add(fixture)
        self.touched.add(_code(unit))
        brand = self.brand_of(unit)
        floor = unit.floor or ""
        parts = [f"{f.attributes['fixture_name']} {f.attributes['fixture_id']} 추가" for f in added]
        parts += [f"{f.attributes['fixture_name']} {f.attributes['fixture_id']} 철거" for f in removed]
        brand_ids = [brand.brand_id] if brand else []
        self.record(
            ChangeType.FIXTURE_CHANGE,
            floor,
            f"{floor} {_code(unit)}({brand.brand_name if brand else '공실'}) 집기 변경: {', '.join(parts)}",
            unit_codes_before=[_code(unit)],
            unit_codes_after=[_code(unit)],
            brand_ids_before=brand_ids,
            brand_ids_after=brand_ids,
            entity_ids_before=[f.entity_id for f in removed],
            entity_ids_after=[f.entity_id for f in added],
            details={
                "fixture_count_before": len(fixtures),
                "fixture_count_after": len(fixtures) - len(removed) + len(added),
                "added_fixture_ids": [f.attributes["fixture_id"] for f in added],
                "removed_fixture_ids": [f.attributes["fixture_id"] for f in removed],
                "added_fixture_types": [f.attributes["fixture_type"] for f in added],
                "removed_fixture_types": [f.attributes["fixture_type"] for f in removed],
            },
        )
        return True


def _revision(
    rev_id: str, parent: str, notes: str, seed: int, kind: RevisionKind = RevisionKind.ACTUAL
) -> Revision:
    return Revision(
        rev_id,
        kind,
        parent,
        REVISION_DATES[rev_id],
        source=f"cad2anything.mockgen seed={seed}",
        notes=notes,
    )


def _boundary_shift(ed: _Editor, floor: str, band: str, category: str, shift_m: float) -> None:
    """Shift the shared edge of two neighbors so the more attractive brand grows by ``shift_m``."""
    min_width = max(CATEGORIES[category].unit_area_range_m2[0] / 12.0, MIN_UNIT_WIDTH_M)
    best = None
    for a, b, _ in ed.adjacent_pairs(floor, category):
        if a.attributes["band"] != band:
            continue
        brand_a, brand_b = ed.brand_of(a), ed.brand_of(b)
        assert brand_a is not None and brand_b is not None
        grower, shrinker = (a, b) if brand_a.attractiveness >= brand_b.attractiveness else (b, a)
        facing = shrinker.attributes["facing"]
        if _run_width(rect_of(shrinker), facing) - shift_m < min_width:
            continue
        key = _run_width(rect_of(a), facing) + _run_width(rect_of(b), facing)
        if best is None or key > best[0]:
            best = (key, grower, shrinker)
    if best is None:
        raise RuntimeError(f"no boundary-shift pair on {floor}")
    _, grower, shrinker = best
    g, s = rect_of(grower), rect_of(shrinker)
    if grower.attributes["facing"] in ("N", "S"):
        edge = g[2] if g[2] == s[0] else g[0]
        new_edge = round(edge + shift_m if g[2] == s[0] else edge - shift_m, 3)
        g_new = round_rect((g[0], g[1], new_edge, g[3]) if g[2] == s[0] else (new_edge, g[1], g[2], g[3]))
        s_new = round_rect((new_edge, s[1], s[2], s[3]) if g[2] == s[0] else (s[0], s[1], new_edge, s[3]))
    else:
        edge = g[3] if g[3] == s[1] else g[1]
        new_edge = round(edge + shift_m if g[3] == s[1] else edge - shift_m, 3)
        g_new = round_rect((g[0], g[1], g[2], new_edge) if g[3] == s[1] else (g[0], new_edge, g[2], g[3]))
        s_new = round_rect((s[0], new_edge, s[2], s[3]) if g[3] == s[1] else (s[0], s[1], s[2], new_edge))
    for unit, rect, sign, other in ((grower, g_new, 1, shrinker), (shrinker, s_new, -1, grower)):
        verb = "확장" if sign > 0 else "축소"
        ed.resize(
            unit,
            rect,
            "boundary_shift",
            f"{floor} {_code(unit)}·{_code(other)} 경계 {shift_m:g} m 이동: {_code(unit)} {verb}",
            boundary_shift_m=sign * shift_m,
            neighbor_unit_code=_code(other),
        )


def build_r1(
    r0: SpatialModel, brands: list[Brand], seed: int, used: set[str]
) -> tuple[SpatialModel, ChangeSet]:
    ed = _Editor(r0, _revision("R1", "R0", "S/S MD 개편", seed), brands, seed, used)

    # 3F: low-exposure WOMEN_CONTEMPORARY brand moves in front of ESC-A (vacant in R0).
    dest = unit_at(ed.model, "3F", ESCALATOR_FRONT_POINTS["ESC-A"])
    assert dest is not None and dest.attributes["vacant"]
    candidates = [u for u in ed.units("3F", "WOMEN_CONTEMPORARY") if u is not dest]
    origin = max(candidates, key=lambda u: (escalator_distance(u.geometry), _code(u)))
    ed.move_brand(origin, dest, "저노출 위치에서 ESC-A 에스컬레이터 정면으로 이동")

    # 1F: weakest island cosmetics counter brand replaced.
    cosmetics = [u for u in ed.units("1F", "COSMETICS") if u.attributes["band"] == "C"]
    weakest = min(cosmetics, key=lambda u: (ed.attractiveness(u), _code(u)))
    ed.reassign(weakest, ed.spare_brand("COSMETICS"), "화장품 카운터 브랜드 교체")

    # 3F: two adjacent small units merged.
    a, b, merged_rect = min(
        ed.adjacent_pairs("3F", "WOMEN_CONTEMPORARY"),
        key=lambda p: (p[0].geometry.area + p[1].geometry.area, _code(p[0])),
    )
    ed.merge(a, b, merged_rect, "소형 매장 2개 통합")

    # 6F: largest SPORTS unit on the south band split into A (brand kept) and B (vacant for re-leasing).
    sports = [u for u in ed.units("6F", "SPORTS") if u.attributes["band"] == "S"]
    big = max(sports, key=lambda u: (u.geometry.area, _code(u)))
    _split(ed, big)

    # 2F: boundary shift between two global-fashion neighbors on the south band.
    _boundary_shift(ed, "2F", "S", "GLOBAL_FASHION", 2.4)

    # 4F: unit at the west-cross-corridor corner becomes a rest walkway (front) and a lounge (back).
    target = unit_at(ed.model, "4F", (41.5, 44.0))
    assert target is not None
    _unit_to_walkway_and_lounge(ed, target)

    # 4F: popup kiosk in the atrium between the escalators.
    _add_popup(ed)

    # B1: fixtures added/removed in food counters.
    food = sorted(
        [
            u
            for u in ed.model.units("B1")
            if not u.attributes["vacant"] and u.attributes["category"] in ("FOOD_DELI", "FOOD_FRESH")
        ],
        key=_code,
    )
    _fixture_changes(ed, food, add_type="SHOWCASE", n_add=2, n_remove=2)

    # 5F: west central corridor widened from 6 m to 7 m, shrinking the adjacent island units.
    _widen_corridor(ed, "5F", "CW", (18.0, 36.5, 42.0, 43.5))
    return ed.finish()


def _split(ed: _Editor, unit: Entity) -> None:
    rect, facing = rect_of(unit), unit.attributes["facing"]
    brand = ed.brand_of(unit)
    assert brand is not None
    n_before = ed.n_fixtures(unit)
    template = dict(unit.attributes)
    floor = unit.floor or ""
    if facing in ("N", "S"):
        cut = round(rect[0] + round((rect[2] - rect[0]) * 0.55, 1), 3)
        rect_a, rect_b = (rect[0], rect[1], cut, rect[3]), (cut, rect[1], rect[2], rect[3])
    else:
        cut = round(rect[1] + round((rect[3] - rect[1]) * 0.55, 1), 3)
        rect_a, rect_b = (rect[0], rect[1], rect[2], cut), (rect[0], cut, rect[2], rect[3])
    ed.remove_unit(unit)
    unit_a = ed.add_unit(floor, f"{_code(unit)}A", round_rect(rect_a), template, brand)
    unit_b = ed.add_unit(floor, f"{_code(unit)}B", round_rect(rect_b), template, None)
    ed.record(
        ChangeType.SPLIT,
        floor,
        f"{floor} 대형 매장 {_code(unit)} 분할 → {_code(unit_a)}({brand.brand_name} 유지) / {_code(unit_b)}(공실, 재임대 예정)",
        unit_codes_before=[_code(unit)],
        unit_codes_after=[_code(unit_a), _code(unit_b)],
        brand_ids_before=[brand.brand_id],
        brand_ids_after=[brand.brand_id],
        entity_ids_before=[unit.entity_id],
        entity_ids_after=[unit_a.entity_id, unit_b.entity_id],
        details={
            "area_before_m2": round(unit.geometry.area, 2),
            "area_after_m2": [round(unit_a.geometry.area, 2), round(unit_b.geometry.area, 2)],
            "vacant_unit_codes_after": [_code(unit_b)],
            "fixture_count_before": n_before,
            "fixture_count_after": ed.n_fixtures(unit_a) + ed.n_fixtures(unit_b),
        },
    )


def _unit_to_walkway_and_lounge(ed: _Editor, unit: Entity) -> None:
    x0, y0, x1, y1 = rect_of(unit)
    floor = unit.floor or ""
    brand = ed.brand_of(unit)
    split_y = round(y0 + 3.5, 3)
    n_before = ed.n_fixtures(unit)
    ed.remove_unit(unit)
    walkway = ed.add_walkway(
        floor, WalkwaySpec("REST", "rest_area", "휴게 통로", rect_poly(x0, y0, x1, split_y))
    )
    lounge = ed.add_amenity(floor, "LOUNGE", rect_poly(x0, split_y, x1, y1))
    ed.record(
        ChangeType.REMOVE,
        floor,
        f"{floor} {_code(unit)}({brand.brand_name if brand else '공실'}) 매장 철거 → 휴게 통로 + 고객라운지 전환",
        unit_codes_before=[_code(unit)],
        brand_ids_before=[brand.brand_id] if brand else [],
        entity_ids_before=[unit.entity_id],
        details={
            "area_before_m2": round(unit.geometry.area, 2),
            "fixture_count_before": n_before,
            "converted_to": {"walkway": walkway.entity_id, "amenity": lounge.entity_id},
            "walkway_area_m2": round(walkway.geometry.area, 2),
            "lounge_area_m2": round(lounge.geometry.area, 2),
        },
    )
    ed.record(
        ChangeType.WALKWAY_CHANGE,
        floor,
        f"{floor} 철거 매장 {_code(unit)} 전면에 휴게 통로 신설 (+{walkway.geometry.area:.1f}㎡)",
        entity_ids_after=[walkway.entity_id],
        details={
            "reason": "unit_removed",
            "source_unit_code": _code(unit),
            "walkway_area_added_m2": round(walkway.geometry.area, 2),
            "lounge_amenity_id": lounge.entity_id,
        },
    )


def _add_popup(ed: _Editor) -> None:
    floor, code, rect = "4F", "4F-P01", (57.0, 42.0, 63.0, 47.0)
    atrium = ed.walkway(floor, "AM")
    area_before = round(atrium.geometry.area, 2)
    brand = ed.spare_brand("ACCESSORIES")
    template = {"facing": "S", "band": "P", "category": brand.category}
    popup = ed.add_unit(floor, code, rect, template, brand)
    atrium.geometry = shapely.set_precision(atrium.geometry.difference(popup.geometry), 0.001)
    ed.record(
        ChangeType.ADD,
        floor,
        f"{floor} 아트리움(ESC-A·ESC-B 사이)에 팝업 매장 {code}({brand.brand_name}) 신설",
        unit_codes_after=[code],
        brand_ids_after=[brand.brand_id],
        entity_ids_after=[popup.entity_id],
        details={
            "area_after_m2": round(popup.geometry.area, 2),
            "category": brand.category,
            "location": "아트리움 중앙",
            "fixture_count_after": ed.n_fixtures(popup),
        },
    )
    ed.record(
        ChangeType.WALKWAY_CHANGE,
        floor,
        f"{floor} 아트리움 중앙 통로 축소: 팝업 {code} 설치 (-{popup.geometry.area:.1f}㎡)",
        entity_ids_before=[atrium.entity_id],
        entity_ids_after=[atrium.entity_id],
        details={
            "reason": "popup_added",
            "popup_unit_code": code,
            "area_before_m2": area_before,
            "area_after_m2": round(atrium.geometry.area, 2),
        },
    )


def _fixture_changes(ed: _Editor, units: list[Entity], add_type: str, n_add: int, n_remove: int) -> None:
    added = removed = 0
    for unit in units:
        if _code(unit) in ed.touched:
            continue
        if added < n_add and ed.change_fixtures(unit, add_type, remove=False):
            added += 1
        elif removed < n_remove and ed.change_fixtures(unit, None, remove=True):
            removed += 1
        if added >= n_add and removed >= n_remove:
            return
    raise RuntimeError(f"fixture changes incomplete: added={added}, removed={removed}")


def _widen_corridor(ed: _Editor, floor: str, segment: str, new_rect: Rect) -> None:
    walkway = ed.walkway(floor, segment)
    old_rect = rect_of(walkway)
    new_geom = rect_poly(*new_rect)
    affected = [u for u in ed.model.units(floor) if u.geometry.intersection(new_geom).area > 1e-6]
    area_before = round(walkway.geometry.area, 2)
    walkway.geometry = new_geom
    width_before = min(old_rect[2] - old_rect[0], old_rect[3] - old_rect[1])
    width_after = min(new_rect[2] - new_rect[0], new_rect[3] - new_rect[1])
    ed.record(
        ChangeType.WALKWAY_CHANGE,
        floor,
        f"{floor} {walkway.attributes['name_ko']} 폭 {width_before:g} m → {width_after:g} m 확장",
        entity_ids_before=[walkway.entity_id],
        entity_ids_after=[walkway.entity_id],
        details={
            "reason": "corridor_widening",
            "width_before_m": width_before,
            "width_after_m": width_after,
            "area_before_m2": area_before,
            "area_after_m2": round(new_geom.area, 2),
            "affected_unit_codes": sorted(_code(u) for u in affected),
        },
    )
    for unit in sorted(affected, key=_code):
        rect = round_rect(unit.geometry.difference(new_geom).bounds)
        ed.resize(
            unit,
            rect,
            "corridor_widening",
            f"{floor} 통로 확장으로 {_code(unit)} 깊이 축소",
            walkway_entity_id=walkway.entity_id,
        )


def build_r2(
    r1: SpatialModel, brands: list[Brand], seed: int, used: set[str]
) -> tuple[SpatialModel, ChangeSet]:
    ed = _Editor(r1, _revision("R2", "R1", "F/W MD 개편", seed), brands, seed, used)

    # 3F category mixing: weakest plain WOMEN_CONTEMPORARY units become ACCESSORIES / CAFE.
    escalator_front = unit_at(ed.model, "3F", ESCALATOR_FRONT_POINTS["ESC-A"])
    plain = [
        u
        for u in ed.units("3F", "WOMEN_CONTEMPORARY")
        if not _code(u).endswith("M") and u is not escalator_front
    ]
    weakest = sorted(plain, key=lambda u: (ed.attractiveness(u), _code(u)))[:3]
    for unit, category in zip(weakest, ("ACCESSORIES", "ACCESSORIES", "CAFE"), strict=True):
        ed.reassign(unit, ed.spare_brand(category), "카테고리 믹스 조정")

    # 7F: two adjacent LIVING units merged.
    a, b, merged_rect = min(
        ed.adjacent_pairs("7F", "LIVING"),
        key=lambda p: (p[0].geometry.area + p[1].geometry.area, _code(p[0])),
    )
    ed.merge(a, b, merged_rect, "리빙 대형 매장화")

    # 8F: weakest restaurant replaced.
    restaurants = ed.units("8F", "RESTAURANT")
    weakest_rest = min(restaurants, key=lambda u: (ed.attractiveness(u), _code(u)))
    ed.reassign(weakest_rest, ed.spare_brand("RESTAURANT"), "식당 교체")

    # 6F: farthest KIDS brand moves into the unit vacated by the R1 split.
    vacant_b = [u for u in ed.model.units("6F") if u.attributes["vacant"] and _code(u).endswith("B")]
    assert vacant_b, "R1 split unit missing"
    kids = ed.units("6F", "KIDS")
    origin = max(kids, key=lambda u: (escalator_distance(u.geometry), _code(u)))
    ed.move_brand(origin, vacant_b[0], "분할 공실로 아동 브랜드 이전")

    # 1F: cosmetics fixture changes.
    cosmetics = sorted(ed.units("1F", "COSMETICS"), key=_code)
    _fixture_changes(ed, cosmetics, add_type="TABLE", n_add=2, n_remove=1)
    return ed.finish()


def build_h1(
    r2: SpatialModel, brands: list[Brand], seed: int, used: set[str]
) -> tuple[SpatialModel, ChangeSet]:
    revision = _revision(
        "H1", "R2", "R2 기반 what-if: 3F 브랜드 맞교환 + 매장 확장", seed, RevisionKind.HYPOTHETICAL
    )
    revision.source = f"cad2anything.mockgen seed={seed} (what-if)"
    ed = _Editor(r2, revision, brands, seed, used)

    # The west-most north band unit will absorb the unused NW back-of-house slot (single resize).
    north = sorted(
        (u for u in ed.model.units("3F") if u.attributes["band"] == "N"), key=lambda u: rect_of(u)[0]
    )
    corner_unit = north[0]

    # Swap: strongest brand in a low-exposure unit <-> weakest brand in a high-exposure unit.
    wc = [u for u in ed.units("3F", "WOMEN_CONTEMPORARY") if u is not corner_unit]
    distances = sorted(escalator_distance(u.geometry) for u in wc)
    median = distances[len(distances) // 2]
    far = [u for u in wc if escalator_distance(u.geometry) >= median]
    near = [u for u in wc if escalator_distance(u.geometry) < median]
    strong_far = max(far, key=lambda u: (ed.attractiveness(u), _code(u)))
    weak_near = min(near, key=lambda u: (ed.attractiveness(u), _code(u)))
    ed.swap_brands(strong_far, weak_near, "what-if: 고매력 브랜드 전면 배치")

    x0, y0, x1, y1 = rect_of(corner_unit)
    ed.resize(
        corner_unit,
        (12.0, y0, x1, y1),
        "absorb_back_of_house",
        f"3F {_code(corner_unit)} 서측 후방 공간 편입으로 매장 확장 (what-if)",
        gained_area_m2=round((x0 - 12.0) * (y1 - y0), 2),
    )
    return ed.finish()


def build_revisions(
    r0: SpatialModel, brands: list[Brand], seed: int = 42
) -> tuple[dict[str, SpatialModel], dict[str, ChangeSet]]:
    """Return models ``{R0, R1, R2, H1}`` and change sets ``{"R0_R1", "R1_R2", "R2_H1"}``."""
    used = {u.attributes["brand_id"] for u in r0.units() if u.attributes["brand_id"]}
    r1, c01 = build_r1(r0, brands, seed, used)
    r2, c12 = build_r2(r1, brands, seed, used)
    h1, c2h = build_h1(r2, brands, seed, set(used))
    return {"R0": r0, "R1": r1, "R2": r2, "H1": h1}, {"R0_R1": c01, "R1_R2": c12, "R2_H1": c2h}


def change_type_counts(change_set: ChangeSet) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in change_set.items:
        counts[str(item.change_type)] = counts.get(str(item.change_type), 0) + 1
    return counts
