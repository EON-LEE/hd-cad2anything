"""Parse a drawing revision folder into a ``SpatialModel`` and a ``ParseReport``.

Stages: read (units, encodings) -> per-layer geometry reconstruction -> layer classification (manifest ->
heuristics -> optional LLM -> human review queue) -> labels, fixtures and entities -> brand registry ->
relations. All geometry, areas and matching are deterministic; the LLM only helps with ambiguous layers.

Usage::

    uv run python -m cad2anything.cadcore.pipeline --rev-dir data/generated/drawings/R1/messy \\
        --out data/generated/parsed/R1_messy.json [--brands data/generated/business/brands.csv] [--llm]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from cad2anything.cadcore.classify import (
    REVIEW_THRESHOLD,
    LayerClassification,
    LayerClassifier,
    classify_layers,
    compute_layer_features,
)
from cad2anything.cadcore.fixtures import FixtureResolver, build_fixtures, resolve_fixture_type
from cad2anything.cadcore.geometry import (
    LayerGeometry,
    PolygonRecord,
    build_layer_geometry,
    dedupe,
    overlap_pairs,
    subtract_nested,
)
from cad2anything.cadcore.labels import (
    LINK_NAMES_KO,
    UNIT_CODE_RE,
    label_amenities,
    label_openings,
    label_units,
    label_vertical_links,
    label_zones,
)
from cad2anything.cadcore.reader import DrawingSet, FloorDrawing, Issue, Primitive, read_revision
from cad2anything.catalog import AMENITY_TYPES, CATEGORIES, FIXTURE_TYPES, FLOOR_HEIGHT_M, FLOOR_PROGRAMS
from cad2anything.model import (
    Entity,
    EntityKind,
    Floor,
    Relation,
    RelationType,
    Revision,
    RevisionKind,
    SpatialModel,
)

LINK_PREFIXES: dict[str, str] = {"escalator": "ESC", "elevator": "EV", "stair": "ST"}
_KIND_ORDER = [
    EntityKind.FLOOR,
    EntityKind.EXTERIOR,
    EntityKind.WALL,
    EntityKind.WALKWAY,
    EntityKind.VERTICAL_LINK,
    EntityKind.AMENITY,
    EntityKind.OPENING,
    EntityKind.ZONE,
    EntityKind.UNIT,
    EntityKind.FIXTURE,
]
_CATEGORY_BY_NAME = {c.name_ko: code for code, c in CATEGORIES.items()}
_SKIP_SEMANTICS = ("noise", "annotation")
_POLYGON_SEMANTICS = ("exterior", "unit", "zone", "walkway", "escalator", "elevator", "stair", "amenity")


# --- report ------------------------------------------------------------------------------------------


@dataclass
class ParseReport:
    """QA report of one parsed revision: layer table, issues, statistics and the human review queue."""

    rev_id: str
    variant: str | None
    source: str
    layer_table: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    review_queue: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def issue_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(i.code for i in self.issues).items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rev_id": self.rev_id,
            "variant": self.variant,
            "source": self.source,
            "stats": self.stats,
            "timings_s": self.timings,
            "issue_counts": self.issue_counts(),
            "issues": [i.to_dict() for i in self.issues],
            "layer_table": self.layer_table,
            "review_queue": self.review_queue,
        }


# --- brand registry ----------------------------------------------------------------------------------


def normalize_brand(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


class BrandRegistry:
    """Exact brand-name lookup (after whitespace/Unicode normalization); ambiguous names never resolve."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.ambiguous: set[str] = set()
        for row in rows:
            name = str(row.get("brand_name") or "").strip()
            if not name:
                continue
            key = normalize_brand(name)
            if key in self.rows and self.rows[key].get("brand_id") != row.get("brand_id"):
                self.ambiguous.add(key)
            self.rows[key] = dict(row)

    def __len__(self) -> int:
        return len(self.rows)

    def resolve(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        key = normalize_brand(name)
        return None if key in self.ambiguous else self.rows.get(key)


def load_brand_registry(registry: Any) -> BrandRegistry | None:
    """Registry from a list of dicts, a DataFrame, a ``BrandRegistry`` or a CSV path (None passes through)."""
    if registry is None or isinstance(registry, BrandRegistry):
        return registry
    if isinstance(registry, str | Path):
        with open(registry, encoding="utf-8-sig", newline="") as fp:
            return BrandRegistry(list(csv.DictReader(fp)))
    if hasattr(registry, "to_dict") and hasattr(registry, "columns"):
        return BrandRegistry(registry.to_dict("records"))
    return BrandRegistry(list(registry))


# --- helpers -----------------------------------------------------------------------------------------


def _natural_key(text: str) -> list[Any]:
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", text)]


def floor_ordinal(code: str) -> int:
    for program in FLOOR_PROGRAMS:
        if program.code == code:
            return program.ordinal
    if match := re.fullmatch(r"B(\d+)F?", code, re.IGNORECASE):
        return -int(match.group(1))
    if match := re.fullmatch(r"(\d+)F", code, re.IGNORECASE):
        return int(match.group(1)) - 1
    return 0


def _floors(drawings: DrawingSet) -> list[Floor]:
    floors = []
    for level in drawings.levels:
        code = str(level["floor"])
        ordinal = int(level.get("ordinal", floor_ordinal(code)))
        floors.append(
            Floor(
                code,
                ordinal,
                str(level.get("name", "")),
                float(level.get("elevation_m", ordinal * FLOOR_HEIGHT_M)),
                float(level.get("height_m", FLOOR_HEIGHT_M)),
            )
        )
    return floors


def _revision(drawings: DrawingSet, rev_id: str | None) -> Revision:
    manifest = drawings.manifest
    rev = rev_id or str(manifest.get("revision") or drawings.rev_dir.parent.name or drawings.rev_dir.name)
    try:
        kind = RevisionKind(str(manifest.get("kind", "actual")))
    except ValueError:
        kind = RevisionKind.ACTUAL
    variant = manifest.get("variant") or drawings.rev_dir.name
    return Revision(
        rev,
        kind,
        manifest.get("parent_revision"),
        manifest.get("effective_date"),
        source=f"cad2anything.cadcore:{manifest.get('facility', 'FACILITY')}/{rev}/{variant}",
        notes="CAD 도면 파싱 결과",
    )


def _order_key(point: BaseGeometry) -> tuple[float, float]:
    return (round(point.y, 1), round(point.x, 1))


def _unique_id(base: str, used: set[str]) -> str:
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}~{n}"
        n += 1
    used.add(candidate)
    return candidate


def _band(unit_code: str) -> str | None:
    match = re.match(r"^[^-]+-([A-Z])", unit_code)
    return match.group(1) if match else None


class _FloorLayers:
    """Access to one floor's reconstructed geometry by semantic."""

    def __init__(self, floor: str, geoms: Mapping[str, LayerGeometry], classification: LayerClassification):
        self.floor = floor
        self.geoms = geoms
        self.semantic = {layer: classification.semantic_of(floor, layer) for layer in geoms}

    def layers(self, *semantics: str) -> list[str]:
        return [layer for layer, semantic in self.semantic.items() if semantic in semantics]

    def polygons(self, *semantics: str) -> list[PolygonRecord]:
        return [r for layer in self.layers(*semantics) for r in self.geoms[layer].polygons]

    def texts(self, *semantics: str) -> list[Primitive]:
        return [t for layer in self.layers(*semantics) for t in self.geoms[layer].texts]

    def lines(self, *semantics: str) -> list[LineString]:
        return [line for layer in self.layers(*semantics) for line in self.geoms[layer].lines]

    def inserts(self, *semantics: str) -> list[Primitive]:
        return [i for layer in self.layers(*semantics) for i in self.geoms[layer].inserts]

    def all_texts(self) -> list[Primitive]:
        return [t for layer, g in self.geoms.items() if self.semantic[layer] != "noise" for t in g.texts]


@dataclass
class _FloorResult:
    entities: list[Entity] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    handle_map: dict[str, str] = field(default_factory=dict)


# --- per-floor build ---------------------------------------------------------------------------------


def _exterior(fl: _FloorLayers, result: _FloorResult, units: Sequence[Polygon]) -> Entity | None:
    records = fl.polygons("exterior")
    floor = fl.floor
    if records:
        record = max(records, key=lambda r: r.polygon.area)
        geometry: BaseGeometry = record.polygon
        result.handle_map.update({h: f"X-{floor}" for h in record.handles})
    elif units:
        geometry = shapely.envelope(unary_union(list(units)))
        result.issues.append(
            Issue(
                "warning",
                "exterior_missing",
                "건물 외곽 레이어가 없어 매장 범위의 외접 사각형을 사용했다.",
                floor,
            )
        )
    else:
        return None
    x0, y0, x1, y1 = geometry.bounds
    return Entity(
        f"X-{floor}",
        EntityKind.EXTERIOR,
        floor,
        geometry,
        label="건물 외곽",
        attributes={
            "width_m": round(x1 - x0, 3),
            "depth_m": round(y1 - y0, 3),
            "area_m2": round(geometry.area, 2),
        },
    )


def _units(fl: _FloorLayers, result: _FloorResult, used: set[str]) -> list[Entity]:
    floor = fl.floor
    records, duplicates = dedupe(fl.polygons("unit"))
    if duplicates:
        result.issues.append(
            Issue(
                "info",
                "duplicate_removed",
                f"여러 매장 레이어에서 겹치는 매장 경계 {duplicates}개를 제거했다.",
                floor,
            )
        )
    labels, issues = label_units([r.polygon for r in records], fl.texts("unit_label", "unit"), floor)
    result.issues.extend(issues)
    units: list[Entity] = []
    unlabeled: list[PolygonRecord] = []
    for record, label in zip(records, labels, strict=True):
        code = label.unit_code
        if code is None:
            unlabeled.append(record)
            continue
        if not code.startswith(f"{floor}-"):
            result.issues.append(
                Issue(
                    "warning",
                    "unit_code_floor_mismatch",
                    f"{floor} 도면에 다른 층 매장 코드 {code}가 있다.",
                    floor,
                )
            )
        if not UNIT_CODE_RE.match(code):
            result.issues.append(
                Issue("info", "unit_code_nonstandard", f"표준 형식이 아닌 매장 코드: {code}", floor)
            )
        entity_id = _unique_id(f"U-{code}", used)
        if entity_id != f"U-{code}":
            result.issues.append(
                Issue(
                    "error",
                    "duplicate_unit_code",
                    f"매장 코드 {code}가 중복되어 {entity_id}로 저장했다.",
                    floor,
                    [entity_id],
                )
            )
        if label.vacant is None:
            result.issues.append(
                Issue(
                    "warning",
                    "unit_brand_missing",
                    f"{code} 라벨에 브랜드명/공실 표시가 없다.",
                    floor,
                    [entity_id],
                )
            )
        polygon = record.polygon
        units.append(
            Entity(
                entity_id,
                EntityKind.UNIT,
                floor,
                polygon,
                label=code,
                attributes={
                    "unit_code": code,
                    "band": _band(code),
                    "brand_id": None,
                    "brand_name": label.brand_name,
                    "category": None,
                    "category_name": None,
                    "area_m2": round(polygon.area, 2),
                    "frontage_m": 0.0,
                    "vacant": label.vacant,
                    "zone_code": None,
                    "label_method": label.method,
                    "geometry_source": record.source,
                },
            )
        )
        result.handle_map.update({h: entity_id for h in record.handles})
    if unlabeled:
        result.issues.append(
            Issue(
                "warning",
                "unit_polygon_without_label",
                f"매장 코드 라벨이 없는 매장 경계 폴리곤 {len(unlabeled)}개를 제외했다.",
                floor,
                data={"areas_m2": [round(r.polygon.area, 1) for r in unlabeled][:20]},
            )
        )
    for i, j, area in overlap_pairs([u.geometry for u in units]):
        result.issues.append(
            Issue(
                "warning",
                "unit_overlap",
                f"매장 경계가 {area:.1f}㎡ 겹친다: {units[i].label}, {units[j].label}",
                floor,
                [units[i].entity_id, units[j].entity_id],
            )
        )
    return units


def _walkways(fl: _FloorLayers, result: _FloorResult) -> list[Entity]:
    records, _ = dedupe(fl.polygons("walkway"))
    records.sort(key=lambda r: _order_key(r.polygon.centroid))
    walkways = []
    for number, record in enumerate(records, 1):
        entity_id = f"W-{fl.floor}-{number}"
        walkways.append(
            Entity(
                entity_id,
                EntityKind.WALKWAY,
                fl.floor,
                record.polygon,
                label=f"통로 {number}",
                attributes={"area_m2": round(record.polygon.area, 2), "geometry_source": record.source},
            )
        )
        result.handle_map.update({h: entity_id for h in record.handles})
    return walkways


def _vertical_links(fl: _FloorLayers, result: _FloorResult, used: set[str]) -> list[Entity]:
    floor = fl.floor
    records: list[PolygonRecord] = []
    layer_types: list[str | None] = []
    for semantic in ("escalator", "elevator", "stair"):
        for record in fl.polygons(semantic):
            records.append(record)
            layer_types.append(semantic)
    texts = fl.texts("escalator", "elevator", "stair") or fl.all_texts()
    labels, issues = label_vertical_links([r.polygon for r in records], layer_types, texts, floor)
    if any(link_id is None for link_id, _, _ in labels):
        fallback, _ = label_vertical_links([r.polygon for r in records], layer_types, fl.all_texts(), floor)
        labels = [lab if lab[0] else fb for lab, fb in zip(labels, fallback, strict=True)]
    result.issues.extend(issues)
    links = []
    counters: Counter[str] = Counter()
    for record, (link_id, link_type, text) in zip(records, labels, strict=True):
        link_type = link_type or "stair"
        if link_id is None:
            counters[link_type] += 1
            link_id = f"{LINK_PREFIXES[link_type]}-U{counters[link_type]}"
            result.issues.append(
                Issue(
                    "warning", "link_without_label", f"ID 라벨이 없는 수직동선에 {link_id}를 부여했다.", floor
                )
            )
        entity_id = _unique_id(f"V-{floor}-{link_id}", used)
        name = LINK_NAMES_KO.get(link_type, link_type)
        links.append(
            Entity(
                entity_id,
                EntityKind.VERTICAL_LINK,
                floor,
                record.polygon,
                label=f"{link_id} {name}",
                attributes={
                    "link_id": link_id,
                    "link_type": link_type,
                    "name_ko": name,
                    "source_label": text,
                },
            )
        )
        result.handle_map.update({h: entity_id for h in record.handles})
    return links


def _amenities(fl: _FloorLayers, result: _FloorResult, used: set[str]) -> list[Entity]:
    floor = fl.floor
    records, _ = dedupe(fl.polygons("amenity"))
    records.sort(key=lambda r: _order_key(r.polygon.centroid))
    labels = label_amenities([r.polygon for r in records], fl.texts("amenity_label", "amenity"))
    counters: Counter[str] = Counter()
    amenities = []
    for record, (kind, text) in zip(records, labels, strict=True):
        if kind is None:
            kind = "UNKNOWN"
            result.issues.append(
                Issue("warning", "amenity_without_label", "명칭 라벨이 없는 편의시설 폴리곤이 있다.", floor)
            )
        counters[kind] += 1
        entity_id = _unique_id(f"A-{floor}-{kind}-{counters[kind]}", used)
        amenities.append(
            Entity(
                entity_id,
                EntityKind.AMENITY,
                floor,
                record.polygon,
                label=text or kind,
                attributes={
                    "amenity_type": kind,
                    "name_ko": AMENITY_TYPES.get(kind, kind),
                    "area_m2": round(record.polygon.area, 2),
                },
            )
        )
        result.handle_map.update({h: entity_id for h in record.handles})
    return amenities


def _openings(fl: _FloorLayers, result: _FloorResult, used: set[str]) -> list[Entity]:
    floor = fl.floor
    lines = fl.lines("opening") + [LineString(r.polygon.exterior.coords) for r in fl.polygons("opening")]
    labels = label_openings(lines, fl.texts("opening"))
    openings = []
    unnamed = 0
    for line, (opening_id, name, text) in zip(lines, labels, strict=True):
        if opening_id is None:
            unnamed += 1
            opening_id = f"OPEN-{unnamed}"
            result.issues.append(
                Issue(
                    "warning",
                    "opening_without_label",
                    f"ID 라벨이 없는 출입구 선에 {opening_id}를 부여했다.",
                    floor,
                )
            )
        entity_id = _unique_id(f"O-{floor}-{opening_id}", used)
        kind = (
            "entrance"
            if opening_id.startswith("ENT") or any(w in name for w in ("출입", "정문", "입구"))
            else "door"
        )
        openings.append(
            Entity(
                entity_id,
                EntityKind.OPENING,
                floor,
                line,
                label=text or opening_id,
                attributes={
                    "opening_id": opening_id,
                    "opening_type": kind,
                    "name_ko": name,
                    "width_m": round(line.length, 2),
                },
            )
        )
    return openings


def _fixture_inserts(fl: _FloorLayers) -> list[Primitive]:
    inserts = fl.inserts("fixture")
    for layer, g in fl.geoms.items():
        if fl.semantic[layer] in ("fixture", *_SKIP_SEMANTICS):
            continue
        inserts.extend(
            i for i in g.inserts if resolve_fixture_type(str(i.block), i.attributes).fixture_type != "UNKNOWN"
        )
    return inserts


def _fixtures(
    fl: _FloorLayers,
    result: _FloorResult,
    units: Sequence[Entity],
    scale: float,
    resolver: FixtureResolver | None,
    used: set[str],
) -> list[Entity]:
    floor = fl.floor
    records, issues = build_fixtures(
        _fixture_inserts(fl),
        [(str(u.attributes["unit_code"]), u.geometry) for u in units],
        floor,
        scale,
        resolver,
    )
    result.issues.extend(issues)
    fixtures = []
    for record in records:
        entity_id = _unique_id(f"F-{record.fixture_id}", used)
        fixture_type = record.typing.fixture_type
        spec = FIXTURE_TYPES.get(fixture_type)
        fixtures.append(
            Entity(
                entity_id,
                EntityKind.FIXTURE,
                floor,
                record.polygon,
                label=record.fixture_id,
                attributes={
                    "fixture_id": record.fixture_id,
                    "fixture_type": fixture_type,
                    "fixture_name": spec.name_ko if spec else str(record.prim.block),
                    "unit_code": record.unit_code,
                    "width_m": record.width_m,
                    "depth_m": record.depth_m,
                    "rotation_deg": round(record.rotation_deg, 2),
                    "block_name": record.prim.block,
                    "type_method": record.typing.method,
                    "id_generated": record.id_generated,
                },
            )
        )
        result.handle_map[record.prim.handle] = entity_id
    return fixtures


def _resolve_brands(
    units: Sequence[Entity], registry: BrandRegistry | None, floor: str, result: _FloorResult
) -> None:
    if registry is None:
        return
    unresolved = []
    for unit in units:
        attrs = unit.attributes
        row = registry.resolve(attrs.get("brand_name"))
        if row is not None:
            attrs["brand_id"] = row.get("brand_id")
            category = row.get("category")
            if category:
                attrs["category"] = str(category)
                attrs["category_source"] = "brand_registry"
        elif attrs.get("brand_name"):
            unresolved.append(unit)
    if unresolved:
        result.issues.append(
            Issue(
                "warning",
                "brand_unresolved",
                f"브랜드 레지스트리와 이름이 일치하지 않는 매장 {len(unresolved)}개",
                floor,
                [u.entity_id for u in unresolved],
                data={"brand_names": [u.attributes["brand_name"] for u in unresolved][:50]},
            )
        )


def _majority_category(units: Iterable[Entity]) -> str | None:
    counts = Counter(
        u.attributes.get("category") for u in units if u.attributes.get("category_source") == "brand_registry"
    )
    counts.pop(None, None)
    return counts.most_common(1)[0][0] if counts else None


def _zones(fl: _FloorLayers, result: _FloorResult, units: Sequence[Entity], used: set[str]) -> list[Entity]:
    floor = fl.floor
    records, _ = dedupe(fl.polygons("zone"))
    holes = subtract_nested(records)
    parts = [r.polygon for r in records]
    labels, issues = label_zones(parts, fl.texts("zone_label", "zone"), floor)
    result.issues.extend(issues)
    unit_points = [(u, u.geometry.representative_point()) for u in units]
    members: list[list[Entity]] = [[u for u, pt in unit_points if part.covers(pt)] for part in parts]
    zone_parts: dict[str, list[int]] = defaultdict(list)
    names: dict[str, str] = {}
    for index, label in enumerate(labels):
        if label is not None:
            zone_parts[label[0]].append(index)
            names.setdefault(label[0], label[1])
    categories: dict[str, str | None] = {}
    for code, indices in zone_parts.items():
        categories[code] = _CATEGORY_BY_NAME.get(names[code]) or _majority_category(
            u for i in indices for u in members[i]
        )
    inferred: dict[str, int] = Counter()
    unassigned = 0
    for index, label in enumerate(labels):
        if label is not None or not members[index]:
            continue
        category = _majority_category(members[index])
        candidates = [code for code, cat in categories.items() if category and cat == category]
        if len(candidates) == 1:
            zone_parts[candidates[0]].append(index)
            inferred[candidates[0]] += 1
        else:
            unassigned += 1
    if inferred:
        result.issues.append(
            Issue(
                "info",
                "zone_part_inferred",
                f"라벨 없는 존 조각 {sum(inferred.values())}개를 소속 매장의 브랜드 카테고리로 존에 배정했다.",
                floor,
                data=dict(inferred),
            )
        )
    if unassigned:
        result.issues.append(
            Issue(
                "warning",
                "zone_part_unlabeled",
                f"존 라벨이 없고 카테고리로도 배정하지 못한 존 조각 {unassigned}개가 있다.",
                floor,
            )
        )
    if holes:
        result.issues.append(Issue("info", "zone_holes", f"존 폴리곤 안쪽 구멍 {holes}개를 반영했다.", floor))
    zones = []
    for code in sorted(zone_parts, key=_natural_key):
        indices = zone_parts[code]
        geometry = unary_union([parts[i] for i in indices])
        zone_units = [u for i in indices for u in members[i]]
        category = categories.get(code)
        entity_id = _unique_id(f"Z-{code}", used)
        zones.append(
            Entity(
                entity_id,
                EntityKind.ZONE,
                floor,
                geometry,
                label=f"{code} {names[code]}".strip(),
                attributes={
                    "zone_code": code,
                    "zone_name": names[code],
                    "category": category,
                    "category_name": CATEGORIES[category].name_ko if category in CATEGORIES else None,
                    "unit_count": len(zone_units),
                    "area_m2": round(geometry.area, 2),
                    "parts": len(indices),
                    "inferred_parts": inferred.get(code, 0),
                },
            )
        )
        for unit in zone_units:
            unit.attributes["zone_code"] = code
            if not unit.attributes.get("category") and category:
                unit.attributes["category"] = category
                unit.attributes["category_source"] = "zone"
    return zones


def _finish_units(
    units: Sequence[Entity], walkways: Sequence[Entity], floor: str, result: _FloorResult
) -> None:
    walk = unary_union([w.geometry for w in walkways]).buffer(0.02) if walkways else None
    missing = []
    for unit in units:
        attrs = unit.attributes
        if walk is not None:
            attrs["frontage_m"] = round(unit.geometry.exterior.intersection(walk).length, 2)
        category = attrs.get("category")
        attrs["category_name"] = CATEGORIES[category].name_ko if category in CATEGORIES else None
        if not category:
            missing.append(unit.entity_id)
    if missing:
        result.issues.append(
            Issue(
                "warning",
                "category_unresolved",
                f"카테고리를 정하지 못한 매장 {len(missing)}개",
                floor,
                missing,
            )
        )


def _build_floor(
    drawing: FloorDrawing,
    geoms: Mapping[str, LayerGeometry],
    classification: LayerClassification,
    registry: BrandRegistry | None,
    resolver: FixtureResolver | None,
    used: set[str],
) -> _FloorResult:
    floor = drawing.floor
    fl = _FloorLayers(floor, geoms, classification)
    result = _FloorResult()
    units = _units(fl, result, used)
    walkways = _walkways(fl, result)
    links = _vertical_links(fl, result, used)
    amenities = _amenities(fl, result, used)
    openings = _openings(fl, result, used)
    _resolve_brands(units, registry, floor, result)
    zones = _zones(fl, result, units, used)
    _finish_units(units, walkways, floor, result)
    fixtures = _fixtures(fl, result, units, drawing.scale, resolver, used)
    exterior = _exterior(fl, result, [u.geometry for u in units])
    result.entities = (
        [e for e in [exterior] if e] + walkways + links + amenities + openings + zones + units + fixtures
    )
    for layer, g in geoms.items():
        if fl.semantic[layer] not in _POLYGON_SEMANTICS:
            continue
        for issue in g.issues:
            ids = sorted(
                {result.handle_map[h] for h in issue.data.get("handles", []) if h in result.handle_map}
            )
            issue.entity_ids = ids
            result.issues.append(issue)
    counts = Counter(e.kind for e in result.entities)
    polygon_layers = [g for layer, g in geoms.items() if fl.semantic[layer] in _POLYGON_SEMANTICS]
    result.stats = {
        "units": counts[EntityKind.UNIT],
        "vacant_units": sum(1 for u in units if u.attributes["vacant"]),
        "fixtures": counts[EntityKind.FIXTURE],
        "fixture_ids_generated": sum(1 for f in fixtures if f.attributes["id_generated"]),
        "walkways": counts[EntityKind.WALKWAY],
        "vertical_links": counts[EntityKind.VERTICAL_LINK],
        "amenities": counts[EntityKind.AMENITY],
        "openings": counts[EntityKind.OPENING],
        "zones": counts[EntityKind.ZONE],
        "wall_segments": sum(
            g.counts["lines"] + g.counts["open"] + g.counts["closed"]
            for g in (geoms[layer] for layer in fl.layers("wall"))
        ),
        "polygons_auto_closed": sum(g.counts["auto_closed"] for g in polygon_layers),
        "polygons_from_lines": sum(g.counts["from_lines"] for g in polygon_layers),
        "duplicates_removed": sum(g.counts["duplicates"] for g in polygon_layers),
        "units_brand_resolved": sum(1 for u in units if u.attributes.get("brand_id")),
        **drawing.text_stats,
    }
    return result


# --- relations ---------------------------------------------------------------------------------------


def _contains_relations(model: SpatialModel) -> list[Relation]:
    unit_ids = {str(u.attributes.get("unit_code")): u.entity_id for u in model.units()}
    zone_ids = {str(z.attributes.get("zone_code")): z.entity_id for z in model.by_kind(EntityKind.ZONE)}
    relations = []
    for unit in model.units():
        zone_id = zone_ids.get(str(unit.attributes.get("zone_code")))
        if zone_id:
            relations.append(Relation(zone_id, unit.entity_id, RelationType.CONTAINS))
    for fixture in model.by_kind(EntityKind.FIXTURE):
        unit_id = unit_ids.get(str(fixture.attributes.get("unit_code")))
        if unit_id:
            relations.append(Relation(unit_id, fixture.entity_id, RelationType.CONTAINS))
    return relations


def _relations(model: SpatialModel, mode: str, issues: list[Issue]) -> list[Relation]:
    contains = _contains_relations(model)
    basic = [
        Relation(e.entity_id, e.floor, RelationType.ON_FLOOR) for e in model.entities.values() if e.floor
    ]
    if mode != "full":
        return contains + basic
    try:
        from cad2anything.spatial.features import compute_relations
    except ImportError:
        issues.append(
            Issue(
                "info",
                "relations_basic",
                "spatial.features를 불러올 수 없어 CONTAINS·ON_FLOOR 관계만 계산했다.",
            )
        )
        return contains + basic
    try:
        derived = compute_relations(model)
    except Exception as exc:
        issues.append(
            Issue(
                "warning",
                "relations_failed",
                f"파생 관계 계산 실패({type(exc).__name__}); CONTAINS·ON_FLOOR 관계만 사용했다.",
            )
        )
        return contains + basic
    return contains + [r for r in derived if r.type != RelationType.CONTAINS]


# --- entry point -------------------------------------------------------------------------------------


def parse_revision(
    rev_dir: Path,
    brand_registry: Any = None,
    classifier: LayerClassifier | None = None,
    rev_id: str | None = None,
    *,
    relations: str = "full",
    review_threshold: float = REVIEW_THRESHOLD,
    fixture_resolver: FixtureResolver | None = None,
    floors: Sequence[str] | None = None,
) -> tuple[SpatialModel, ParseReport]:
    """Parse ``rev_dir`` (manifest + DXF per floor) into a spatial model and a QA report.

    ``brand_registry``: list of dicts, DataFrame or CSV path with ``brand_id``, ``brand_name``, ``category``.
    ``classifier``: optional layer classifier (e.g. ``LlmClassifier``) consulted for layers whose manifest or
    heuristic confidence is below ``review_threshold``. ``relations``: ``full`` (spatial.features when
    available) or ``basic`` (CONTAINS and ON_FLOOR only).
    """
    started = time.perf_counter()
    timings: dict[str, float] = {}
    drawings = read_revision(Path(rev_dir), floors)
    timings["read"] = time.perf_counter() - started
    mark = time.perf_counter()
    geometry = {d.floor: build_layer_geometry(d) for d in drawings.floors}
    features = [f for d in drawings.floors for f in compute_layer_features(d.floor, geometry[d.floor])]
    timings["geometry_features"] = time.perf_counter() - mark
    mark = time.perf_counter()
    classification = classify_layers(
        features, drawings.manifest, fallback=classifier, threshold=review_threshold
    )
    timings["classify"] = time.perf_counter() - mark
    mark = time.perf_counter()
    registry = load_brand_registry(brand_registry)
    revision = _revision(drawings, rev_id)
    model = SpatialModel(revision=revision, floors=_floors(drawings))
    issues: list[Issue] = list(drawings.issues)
    floor_stats: dict[str, dict[str, int]] = {}
    used: set[str] = set()
    entities: list[Entity] = []
    for drawing in drawings.floors:
        issues.extend(drawing.issues)
        result = _build_floor(
            drawing, geometry[drawing.floor], classification, registry, fixture_resolver, used
        )
        entities.extend(result.entities)
        issues.extend(result.issues)
        floor_stats[drawing.floor] = {
            **result.stats,
            "layers": sum(1 for r in classification.results if r.floor == drawing.floor),
            "review_layers": sum(1 for r in classification.review_queue if r.floor == drawing.floor),
        }
    floor_order = {f.code: i for i, f in enumerate(model.floors)}
    kind_order = {k: i for i, k in enumerate(_KIND_ORDER)}
    for entity in sorted(
        entities,
        key=lambda e: (floor_order.get(e.floor or "", 99), kind_order[e.kind], _natural_key(e.entity_id)),
    ):
        model.add(entity)
    timings["build"] = time.perf_counter() - mark
    mark = time.perf_counter()
    model.relations = _relations(model, relations, issues)
    timings["relations"] = time.perf_counter() - mark
    for result_row in classification.review_queue:
        issues.append(
            Issue(
                "warning",
                "layer_needs_review",
                f"레이어 '{result_row.layer}' 의미 확인 필요 (추정 {result_row.semantic}, 신뢰도 {result_row.confidence:.2f})",
                result_row.floor,
                result_row.layer,
            )
        )
    model.issues = [i.to_dict() for i in issues]
    totals: Counter[str] = Counter()
    for stats in floor_stats.values():
        totals.update(stats)
    llm_calls = getattr(classifier, "calls", 0) if classifier is not None else 0
    timings["total"] = time.perf_counter() - started
    report = ParseReport(
        rev_id=revision.rev_id,
        variant=drawings.manifest.get("variant"),
        source=str(revision.source),
        layer_table=classification.table(),
        issues=issues,
        stats={
            "floors": floor_stats,
            "totals": dict(totals),
            "entities": len(model.entities),
            "relations": len(model.relations),
            "dxf_versions": sorted({d.dxfversion for d in drawings.floors}),
            "insunits": sorted({d.insunits for d in drawings.floors}),
            "scale_to_m": sorted({d.scale for d in drawings.floors}),
            "codepages": sorted({str(d.codepage) for d in drawings.floors}),
            "brand_registry_size": len(registry) if registry is not None else 0,
            "classification_methods": dict(Counter(r.method for r in classification.results)),
            "review_queue_size": len(classification.review_queue),
            "llm": {"calls": llm_calls, "errors": classification.llm_errors},
        },
        review_queue=[r.to_review() for r in classification.review_queue],
        timings={k: round(v, 3) for k, v in timings.items()},
    )
    return model, report


# --- CLI ---------------------------------------------------------------------------------------------


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a CAD drawing revision folder into a spatial model.")
    parser.add_argument("--rev-dir", type=Path, required=True, help="folder with manifest.json and DXF files")
    parser.add_argument("--out", type=Path, required=True, help="output SpatialModel JSON")
    parser.add_argument(
        "--report", type=Path, default=None, help="parse report JSON (default: <out>.report.json)"
    )
    parser.add_argument(
        "--brands", type=Path, default=None, help="brand registry CSV (brand_id, brand_name, category)"
    )
    parser.add_argument(
        "--llm", action="store_true", help="ask the Foundry mini model about ambiguous layers"
    )
    parser.add_argument("--rev-id", default=None, help="override the revision ID")
    parser.add_argument("--relations", choices=("full", "basic"), default="full")
    parser.add_argument("--truth", type=Path, default=None, help="truth layout JSON to evaluate against")
    args = parser.parse_args(argv)
    classifier = None
    resolver = None
    if args.llm:
        from cad2anything.cadcore.classify import foundry_llm_classifier
        from cad2anything.cadcore.fixtures import LlmFixtureResolver

        classifier = foundry_llm_classifier()
        resolver = LlmFixtureResolver(classifier.client, classifier.model)
    model, report = parse_revision(
        args.rev_dir,
        args.brands,
        classifier,
        args.rev_id,
        relations=args.relations,
        fixture_resolver=resolver,
    )
    _write_json(args.out, model.to_dict())
    report_path = args.report or args.out.with_suffix(".report.json")
    _write_json(report_path, report.to_dict())
    summary: dict[str, Any] = {
        "rev_id": report.rev_id,
        "variant": report.variant,
        "out": str(args.out),
        "report": str(report_path),
        "totals": {
            k: report.stats["totals"].get(k)
            for k in (
                "units",
                "vacant_units",
                "fixtures",
                "fixture_ids_generated",
                "walkways",
                "vertical_links",
                "amenities",
                "openings",
                "zones",
                "units_brand_resolved",
                "polygons_auto_closed",
                "polygons_from_lines",
                "hangul_texts",
                "mojibake_suspect",
            )
        },
        "review_queue_size": report.stats["review_queue_size"],
        "issue_counts": report.issue_counts(),
        "llm": report.stats["llm"],
        "timings_s": report.timings,
    }
    if args.truth:
        from cad2anything.cadcore.evaluate import compare_with_truth

        truth = SpatialModel.from_dict(json.loads(args.truth.read_text(encoding="utf-8")))
        summary["evaluation"] = compare_with_truth(model, truth)
    print(json.dumps(summary, ensure_ascii=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
