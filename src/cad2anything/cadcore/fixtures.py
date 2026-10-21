"""Fixture blocks (INSERT) -> typed fixtures with footprints, owning unit and stable IDs.

Type resolution order: ``FX_{TYPE}`` block names, the ``TYPE`` attribute, Korean/English block-name keywords
(``쇼케이스``, ``행거_01``, ``식탁_4인``, ...), then an optional resolver hook (e.g. an LLM). Footprints come
from the block geometry, else from ``W``/``D`` attributes, else from the catalog dimensions. Fixtures without
a ``FIXTURE_ID`` attribute get ``{unit_code}-F{nnn}`` IDs in a stable spatial order and are flagged.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import shapely
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.strtree import STRtree

from cad2anything.cadcore.reader import Issue, Primitive
from cad2anything.catalog import FIXTURE_TYPES

FIXTURE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SHOWCASE": ("SHOWCASE", "SHOW_CASE", "SHOW-CASE", "쇼케이스", "진열장", "냉장케이스"),
    "HANGER": ("HANGER", "RACK", "행거", "옷걸이"),
    "GONDOLA": ("GONDOLA", "SHELF", "곤돌라", "선반"),
    "COUNTER": ("COUNTER", "CASHIER", "CASH", "POS", "카운터", "계산대"),
    "TABLE": ("TABLE", "진열테이블", "테이블", "진열대"),
    "MANNEQUIN": ("MANNEQUIN", "MANIKIN", "STAGE", "마네킹"),
    "FITTING": ("FITTINGROOM", "FITTING", "피팅룸", "피팅", "탈의실", "탈의"),
    "DINING": ("DINING_TABLE", "DINING", "식탁"),
}
_FX_CODE = re.compile(r"^FX[_\-\s]?([A-Z]+)", re.IGNORECASE)
_TOKEN_SPLIT = re.compile(r"[^0-9A-Z\uac00-\ud7a3]+")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
FixtureResolver = Callable[[str, Mapping[str, str]], tuple[str | None, float, str] | None]


@dataclass
class FixtureTyping:
    fixture_type: str
    confidence: float
    method: str
    evidence: str


@dataclass
class FixtureRecord:
    prim: Primitive
    typing: FixtureTyping
    polygon: Polygon
    width_m: float
    depth_m: float
    rotation_deg: float
    footprint_source: str
    unit_code: str | None = None
    fixture_id: str = ""
    id_generated: bool = False


def _keyword_type(text: str) -> tuple[str | None, str]:
    upper = text.upper()
    tokens = {t for t in _TOKEN_SPLIT.split(upper) if t}
    best: tuple[int, str | None, str] = (0, None, "")
    for fixture_type, keywords in FIXTURE_KEYWORDS.items():
        for keyword in keywords:
            if keyword.isascii():
                hit = keyword in tokens if len(keyword) <= 4 else keyword in upper
            else:
                hit = keyword in text
            if hit and len(keyword) > best[0]:
                best = (len(keyword), fixture_type, keyword)
    return best[1], best[2]


def resolve_fixture_type(
    block: str, attributes: Mapping[str, str], resolver: FixtureResolver | None = None
) -> FixtureTyping:
    """Fixture type of a block reference (see module docstring for the resolution order)."""
    match = _FX_CODE.match(block.strip())
    if match and match.group(1).upper() in FIXTURE_TYPES:
        return FixtureTyping(match.group(1).upper(), 1.0, "block_code", f"블록명 {block}")
    attr = str(attributes.get("TYPE", "")).strip()
    if attr:
        for code, spec in FIXTURE_TYPES.items():
            if attr.upper() == code or attr == spec.name_ko:
                return FixtureTyping(code, 0.95, "attribute", f"TYPE 속성 {attr}")
        found, keyword = _keyword_type(attr)
        if found:
            return FixtureTyping(found, 0.85, "attribute_keyword", f"TYPE 속성 키워드 {keyword}")
    found, keyword = _keyword_type(block)
    if found:
        return FixtureTyping(found, 0.9, "block_keyword", f"블록명 키워드 {keyword}")
    if resolver is not None:
        answer = resolver(block, attributes)
        if answer and answer[0] in FIXTURE_TYPES:
            return FixtureTyping(str(answer[0]), float(answer[1]), "llm", answer[2])
    return FixtureTyping("UNKNOWN", 0.0, "none", f"블록명 {block}")


class LlmFixtureResolver:
    """LLM hook for unknown block names; strict JSON ``{block, fixture_type, confidence, evidence}``."""

    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model
        self.calls = 0
        self.errors: list[str] = []
        self._cache: dict[str, tuple[str | None, float, str] | None] = {}

    def __call__(self, block: str, attributes: Mapping[str, str]) -> tuple[str | None, float, str] | None:
        from cad2anything.cadcore.classify import request_json

        if block in self._cache:
            return self._cache[block]
        schema = {
            "type": "object",
            "properties": {
                "block": {"type": "string"},
                "fixture_type": {"type": "string", "enum": [*FIXTURE_TYPES, "UNKNOWN"]},
                "confidence": {"type": "number"},
                "evidence": {"type": "string"},
            },
            "required": ["block", "fixture_type", "confidence", "evidence"],
            "additionalProperties": False,
        }
        system = (
            "Map a CAD block name of a retail fixture to one fixture type. Types: "
            + ", ".join(f"{code} ({spec.name_ko})" for code, spec in FIXTURE_TYPES.items())
            + ". Answer UNKNOWN when unsure. Return JSON only."
        )
        result: tuple[str | None, float, str] | None = None
        try:
            self.calls += 1
            data = request_json(
                self.client,
                self.model,
                system,
                {"block": block, "attributes": dict(attributes)},
                schema,
                "fixture",
            )
            fixture_type = str(data.get("fixture_type", "")).upper()
            if fixture_type in FIXTURE_TYPES:
                confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
                result = (
                    fixture_type,
                    confidence,
                    f"LLM({self.model}): {str(data.get('evidence', ''))[:200]}",
                )
        except Exception as exc:
            self.errors.append(f"{block}: {type(exc).__name__}")
        self._cache[block] = result
        return result


def _dims_from_attributes(attributes: Mapping[str, str], scale: float) -> tuple[float, float] | None:
    values = []
    for key in ("W", "D"):
        match = _NUMBER.search(str(attributes.get(key, "")))
        if not match:
            return None
        values.append(float(match.group(0)) * scale)
    width, depth = values
    return (width, depth) if width > 0 and depth > 0 else None


def _rect(center: tuple[float, float], width: float, depth: float, rotation: float) -> Polygon:
    x, y = center
    rect = box(x - width / 2, y - depth / 2, x + width / 2, y + depth / 2)
    return affinity.rotate(rect, rotation, origin=(x, y)) if rotation else rect


def fixture_footprint(prim: Primitive, fixture_type: str, scale: float) -> tuple[Polygon, float, float, str]:
    """``(footprint, width_m, depth_m, source)``; source is block, attributes, catalog or default."""
    center = prim.anchor or prim.points[0]
    dims = _dims_from_attributes(prim.attributes, scale)
    if prim.footprint and len(prim.footprint) >= 3:
        polygon = Polygon(prim.footprint)
        if not polygon.is_valid:
            polygon = shapely.make_valid(polygon).convex_hull
        if isinstance(polygon, Polygon) and polygon.area > 1e-6:
            if dims is None:
                envelope = list(shapely.oriented_envelope(polygon).exterior.coords)
                sides = sorted(
                    (
                        ((envelope[0][0] - envelope[1][0]) ** 2 + (envelope[0][1] - envelope[1][1]) ** 2)
                        ** 0.5,
                        ((envelope[1][0] - envelope[2][0]) ** 2 + (envelope[1][1] - envelope[2][1]) ** 2)
                        ** 0.5,
                    ),
                    reverse=True,
                )
                dims = (sides[0], sides[1])
            return polygon, round(dims[0], 3), round(dims[1], 3), "block"
    if dims is not None:
        return (
            _rect(center, dims[0], dims[1], prim.rotation),
            round(dims[0], 3),
            round(dims[1], 3),
            "attributes",
        )
    if fixture_type in FIXTURE_TYPES:
        spec = FIXTURE_TYPES[fixture_type]
        return _rect(center, spec.width_m, spec.depth_m, prim.rotation), spec.width_m, spec.depth_m, "catalog"
    return _rect(center, 0.5, 0.5, prim.rotation), 0.5, 0.5, "default"


def assign_units(
    polygons: Sequence[Polygon], units: Sequence[tuple[str, Polygon]], max_distance: float = 0.5
) -> list[str | None]:
    """Unit code containing each fixture (centroid in polygon, else max overlap, else nearest)."""
    if not units:
        return [None] * len(polygons)
    shapes = [u for _, u in units]
    tree = STRtree(shapes)
    owners: list[str | None] = []
    for polygon in polygons:
        centroid = polygon.centroid
        hits = [int(i) for i in tree.query(centroid, predicate="intersects")]
        if hits:
            owners.append(units[min(hits, key=lambda i: shapes[i].area)][0])
            continue
        overlaps = [(shapes[int(i)].intersection(polygon).area, int(i)) for i in tree.query(polygon)]
        overlaps = [o for o in overlaps if o[0] > 1e-6]
        if overlaps:
            owners.append(units[max(overlaps)[1]][0])
            continue
        nearest = tree.query_nearest(Point(centroid), max_distance=max_distance)
        owners.append(units[int(nearest[0])][0] if len(nearest) else None)
    return owners


def assign_ids(records: list[FixtureRecord], floor: str) -> list[FixtureRecord]:
    """Keep ``FIXTURE_ID`` attributes (unique per floor); number the rest per unit in a stable order."""
    seen: set[str] = set()
    missing: list[FixtureRecord] = []
    for record in records:
        given = str(record.prim.attributes.get("FIXTURE_ID", "")).strip()
        if given and given not in seen:
            record.fixture_id = given
            seen.add(given)
        else:
            missing.append(record)
    by_owner: dict[str, list[FixtureRecord]] = {}
    for record in missing:
        by_owner.setdefault(record.unit_code or f"{floor}-X", []).append(record)
    for owner, group in sorted(by_owner.items()):
        prefix = f"{owner}-F"
        used = {int(i[len(prefix) :]) for i in seen if i.startswith(prefix) and i[len(prefix) :].isdigit()}
        number = 0
        for record in sorted(
            group, key=lambda r: (round(r.polygon.centroid.y, 3), round(r.polygon.centroid.x, 3))
        ):
            number += 1
            while number in used:
                number += 1
            record.fixture_id = f"{prefix}{number:03d}"
            record.id_generated = True
            seen.add(record.fixture_id)
    return records


def build_fixtures(
    inserts: Sequence[Primitive],
    units: Sequence[tuple[str, Polygon]],
    floor: str,
    scale: float,
    resolver: FixtureResolver | None = None,
) -> tuple[list[FixtureRecord], list[Issue]]:
    """Typed fixtures with footprints, owning units and IDs for one floor."""
    issues: list[Issue] = []
    cache: dict[tuple[str, str], FixtureTyping] = {}
    records: list[FixtureRecord] = []
    for prim in inserts:
        key = (str(prim.block), str(prim.attributes.get("TYPE", "")))
        if key not in cache:
            cache[key] = resolve_fixture_type(key[0], prim.attributes, resolver)
        typing = cache[key]
        polygon, width, depth, source = fixture_footprint(prim, typing.fixture_type, scale)
        records.append(FixtureRecord(prim, typing, polygon, width, depth, prim.rotation, source))
    for record, owner in zip(records, assign_units([r.polygon for r in records], units), strict=True):
        record.unit_code = owner
    given = Counter(str(r.prim.attributes.get("FIXTURE_ID", "")).strip() for r in records)
    duplicates = sorted(i for i, n in given.items() if i and n > 1)
    assign_ids(records, floor)
    if duplicates:
        issues.append(
            Issue(
                "warning",
                "fixture_id_duplicate",
                f"FIXTURE_ID가 중복된 집기 {len(duplicates)}종: 두 번째부터 새 ID를 부여했다.",
                floor,
                data={"fixture_ids": duplicates[:50]},
            )
        )
    unknown = Counter(str(r.prim.block) for r in records if r.typing.fixture_type == "UNKNOWN")
    if unknown:
        issues.append(
            Issue(
                "warning",
                "fixture_type_unknown",
                f"집기 유형을 판별하지 못한 블록 {sum(unknown.values())}개: {', '.join(sorted(unknown))}",
                floor,
                data={"blocks": dict(unknown)},
            )
        )
    outside = [r for r in records if r.unit_code is None]
    if outside:
        issues.append(
            Issue(
                "warning",
                "fixture_outside_unit",
                f"매장 경계 밖 집기 {len(outside)}개",
                floor,
                [f"F-{r.fixture_id}" for r in outside],
            )
        )
    generated = [r for r in records if r.id_generated]
    if generated:
        issues.append(
            Issue(
                "info",
                "fixture_id_generated",
                f"FIXTURE_ID 속성이 없는 집기 {len(generated)}개에 매장별 순번 ID를 부여했다.",
                floor,
                [f"F-{r.fixture_id}" for r in generated],
                data={"blocks": dict(Counter(str(r.prim.block) for r in generated))},
            )
        )
    return records, issues
