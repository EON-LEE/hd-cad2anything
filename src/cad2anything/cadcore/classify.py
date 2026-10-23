"""Layer semantics: manifest mapping -> heuristics (name keywords + content features) -> optional LLM ->
human review queue.

Content features are computed per (floor, layer) from reconstructed geometry and text patterns, so the
classification works for meaningless layer names (``0``, ``LAYER_17``) as long as the content is telling.
The LLM stage is optional and only sees a compact feature summary of layers below the review threshold; it
must return strict JSON ``{layer, semantic, confidence, evidence}``.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad2anything.cadcore.geometry import LayerGeometry, iou, polygon_parts
from cad2anything.cadcore.labels import link_info, text_kind

SEMANTICS: tuple[str, ...] = (
    "exterior",
    "wall",
    "unit",
    "unit_label",
    "zone",
    "zone_label",
    "walkway",
    "escalator",
    "elevator",
    "stair",
    "opening",
    "amenity",
    "amenity_label",
    "fixture",
    "annotation",
    "noise",
)
REVIEW_THRESHOLD = 0.7
SEMANTIC_NAMES_KO: dict[str, str] = {
    "exterior": "건물 외곽",
    "wall": "벽",
    "unit": "매장 경계",
    "unit_label": "매장 라벨",
    "zone": "존 경계",
    "zone_label": "존 라벨",
    "walkway": "보행 통로",
    "escalator": "에스컬레이터",
    "elevator": "엘리베이터",
    "stair": "계단",
    "opening": "출입구",
    "amenity": "편의시설",
    "amenity_label": "편의시설 라벨",
    "fixture": "집기",
    "annotation": "주석",
    "noise": "잡음",
}

# --- features ----------------------------------------------------------------------------------------


@dataclass
class LayerFeatures:
    """Content summary of one layer on one floor (all lengths in meters, areas in m²)."""

    floor: str
    layer: str
    counts: dict[str, int]
    n_polygons: int = 0
    polygon_sources: dict[str, int] = field(default_factory=dict)
    area_median_m2: float = 0.0
    area_max_m2: float = 0.0
    area_total_m2: float = 0.0
    largest_share: float = 0.0
    contains_frac: float = 0.0
    elongation_median: float = 0.0
    union_parts: int = 0
    unit_code_hits_frac: float = 0.0
    unit_code_hits_mean: float = 0.0
    amenity_hits_frac: float = 0.0
    link_hits_frac: float = 0.0
    link_hit_types: dict[str, int] = field(default_factory=dict)
    loops_match_other: float = 0.0
    loops_match_layers: dict[str, int] = field(default_factory=dict)
    text_kinds: dict[str, int] = field(default_factory=dict)
    link_types: dict[str, int] = field(default_factory=dict)
    sample_texts: list[str] = field(default_factory=list)
    text_height_median: float = 0.0
    texts_outside_frac: float = 0.0
    n_segments: int = 0
    lines_axis_frac: float = 0.0
    lines_outside_frac: float = 0.0
    lines_on_boundary_frac: float = 0.0
    insert_blocks: dict[str, int] = field(default_factory=dict)
    insert_attrib_frac: float = 0.0
    other_types: dict[str, int] = field(default_factory=dict)

    @property
    def n_entities(self) -> int:
        c = self.counts
        return sum(c.get(k, 0) for k in ("closed", "open", "lines", "texts", "inserts", "other"))

    @property
    def n_polyline_polygons(self) -> int:
        return self.polygon_sources.get("closed", 0) + self.polygon_sources.get("auto_closed", 0)

    def summary(self, max_texts: int = 8) -> dict[str, Any]:
        """Compact JSON-able summary (used for the report and the LLM prompt)."""
        c = self.counts
        data: dict[str, Any] = {
            "floor": self.floor,
            "layer": self.layer,
            "entities": {
                k: c.get(k, 0)
                for k in ("closed", "open", "lines", "texts", "inserts", "other")
                if c.get(k, 0)
            },
        }
        if self.n_polygons:
            data["polygons"] = {
                "count": self.n_polygons,
                "sources": self.polygon_sources,
                "area_median_m2": round(self.area_median_m2, 1),
                "area_max_m2": round(self.area_max_m2, 1),
                "largest_share_of_building": round(self.largest_share, 3),
                "contains_other_geometry_frac": round(self.contains_frac, 3),
                "elongation_median": round(self.elongation_median, 2),
                "union_parts": self.union_parts,
                "with_unit_code_frac": round(self.unit_code_hits_frac, 3),
                "unit_codes_per_polygon": round(self.unit_code_hits_mean, 2),
                "with_amenity_label_frac": round(self.amenity_hits_frac, 3),
                "with_link_label_frac": round(self.link_hits_frac, 3),
                "line_loops_matching_other_layers": round(self.loops_match_other, 3),
                "line_loops_traced_layers": self.loops_match_layers,
            }
        if c.get("texts"):
            data["texts"] = {
                "count": c["texts"],
                "kinds": self.text_kinds,
                "samples": [t[:40] for t in self.sample_texts[:max_texts]],
                "height_median_m": round(self.text_height_median, 2),
                "outside_building_frac": round(self.texts_outside_frac, 3),
            }
        if self.n_segments:
            data["lines"] = {
                "segments": self.n_segments,
                "axis_aligned_frac": round(self.lines_axis_frac, 3),
                "outside_building_frac": round(self.lines_outside_frac, 3),
                "on_building_outline_frac": round(self.lines_on_boundary_frac, 3),
            }
        if c.get("inserts"):
            top = dict(Counter(self.insert_blocks).most_common(5))
            data["inserts"] = {
                "count": c["inserts"],
                "blocks": top,
                "with_attributes_frac": round(self.insert_attrib_frac, 3),
            }
        if self.other_types:
            data["other_types"] = self.other_types
        return data


@dataclass
class _FloorContext:
    floor: str
    building: Polygon | None
    building_area: float
    inside: BaseGeometry | None
    outline: BaseGeometry | None
    code_xy: np.ndarray
    amenity_xy: np.ndarray
    link_xy: np.ndarray
    link_types: list[str]
    kinds: dict[int, str]
    polygons: list[tuple[str, Polygon]]


def _xy(points: list[tuple[float, float]]) -> np.ndarray:
    return np.array(points, dtype=float).reshape(-1, 2)


def _floor_context(floor: str, geoms: Mapping[str, LayerGeometry]) -> _FloorContext:
    polygons = [(layer, r.polygon) for layer, g in geoms.items() for r in g.polygons]
    building: Polygon | None = None
    if polygons:
        candidate = max(polygons, key=lambda lp: lp[1].area)[1]
        others = [p for _, p in polygons if p is not candidate]
        prepared = candidate.buffer(0.5)
        inside = sum(1 for p in others if prepared.covers(p.representative_point()))
        if not others or inside / len(others) >= 0.8:
            building = candidate
    if building is None:
        points = [pt for g in geoms.values() for r in g.polygons for pt in r.polygon.exterior.coords]
        points += [pt for g in geoms.values() for t in g.texts for pt in t.points]
        if len(points) >= 3:
            hull = shapely.convex_hull(shapely.MultiPoint(points))
            building = hull if isinstance(hull, Polygon) else None
    kinds: dict[int, str] = {}
    codes, amenities, links, link_types = [], [], [], []
    for g in geoms.values():
        for t in g.texts:
            kind = text_kind(t.text)
            kinds[id(t)] = kind
            point = t.anchor or t.points[0]
            if kind in ("unit_code", "unit_combined"):
                codes.append(point)
            elif kind == "amenity":
                amenities.append(point)
            elif kind == "link":
                links.append(point)
                link_types.append(link_info(t.text)[1] or "")
    inside = building.buffer(0.5) if building is not None else None
    outline = building.exterior.buffer(0.1) if building is not None else None
    if inside is not None:
        shapely.prepare(inside)
    return _FloorContext(
        floor,
        building,
        building.area if building is not None else 0.0,
        inside,
        outline,
        _xy(codes),
        _xy(amenities),
        _xy(links),
        link_types,
        kinds,
        polygons,
    )


def _hits(polygon: Polygon, xy: np.ndarray) -> np.ndarray:
    if not len(xy):
        return np.zeros(0, dtype=bool)
    return shapely.contains_xy(polygon, xy[:, 0], xy[:, 1])


def _elongation(polygon: Polygon) -> float:
    envelope = shapely.oriented_envelope(polygon)
    coords = list(getattr(envelope, "exterior", envelope).coords)
    if len(coords) < 4:
        return 1.0
    a, b = math.dist(coords[0], coords[1]), math.dist(coords[1], coords[2])
    short, long = sorted((a, b))
    return long / short if short > 1e-9 else 99.0


def _segments(lines: Sequence[LineString]) -> list[LineString]:
    segments: list[LineString] = []
    for line in lines:
        coords = list(line.coords)
        segments.extend(LineString([a, b]) for a, b in zip(coords, coords[1:], strict=False) if a != b)
    return segments


def _layer_features(ctx: _FloorContext, g: LayerGeometry) -> LayerFeatures:
    counts = {k: int(v) for k, v in g.counts.items()}
    features = LayerFeatures(ctx.floor, g.layer, counts)
    polygons = [r.polygon for r in g.polygons]
    if polygons:
        areas = [p.area for p in polygons]
        features.n_polygons = len(polygons)
        features.polygon_sources = dict(Counter(r.source for r in g.polygons))
        features.area_median_m2 = float(statistics.median(areas))
        features.area_max_m2 = max(areas)
        features.area_total_m2 = sum(areas)
        largest = polygons[int(np.argmax(areas))]
        if ctx.building_area > 0:
            features.largest_share = largest.area / ctx.building_area
        others = [p for layer, p in ctx.polygons if layer != g.layer]
        if others:
            grown = largest.buffer(0.5)
            features.contains_frac = sum(grown.covers(p.representative_point()) for p in others) / len(others)
        features.elongation_median = float(statistics.median(_elongation(p) for p in polygons))
        features.union_parts = len(polygon_parts(unary_union([p.buffer(0.01) for p in polygons])))
        code_hits = [int(_hits(p, ctx.code_xy).sum()) for p in polygons]
        features.unit_code_hits_frac = sum(h >= 1 for h in code_hits) / len(polygons)
        features.unit_code_hits_mean = sum(code_hits) / len(polygons)
        features.amenity_hits_frac = sum(bool(_hits(p, ctx.amenity_xy).any()) for p in polygons) / len(
            polygons
        )
        link_types: Counter[str] = Counter()
        with_link = 0
        for p in polygons:
            mask = _hits(p, ctx.link_xy)
            if mask.any():
                with_link += 1
                link_types.update(ctx.link_types[i] for i in np.flatnonzero(mask) if ctx.link_types[i])
        features.link_hits_frac = with_link / len(polygons)
        features.link_hit_types = dict(link_types)
        loops = [r.polygon for r in g.polygons if r.source == "lines"]
        if loops and others:
            other_layers = [layer for layer, _ in ctx.polygons if layer != g.layer]
            tree = STRtree(others)
            matched_layers: Counter[str] = Counter()
            matched = 0
            for loop in loops:
                hits = [int(j) for j in tree.query(loop) if iou(loop, others[int(j)]) >= 0.9]
                matched += bool(hits)
                matched_layers.update({other_layers[j] for j in hits})
            features.loops_match_other = matched / len(loops)
            features.loops_match_layers = dict(matched_layers)
    if g.texts:
        kinds = Counter(ctx.kinds.get(id(t)) or text_kind(t.text) for t in g.texts)
        features.text_kinds = dict(kinds)
        features.link_types = dict(Counter(lt for t in g.texts if (lt := link_info(t.text)[1])))
        features.sample_texts = [t.text.replace("\n", " / ") for t in g.texts[:12]]
        features.text_height_median = float(statistics.median(t.height for t in g.texts))
        if ctx.inside is not None:
            outside = sum(not ctx.inside.covers(shapely.Point(t.anchor or t.points[0])) for t in g.texts)
            features.texts_outside_frac = outside / len(g.texts)
    segments = _segments(g.raw_lines)
    if segments:
        features.n_segments = len(segments)
        axis = 0
        for seg in segments:
            (x0, y0), (x1, y1) = seg.coords
            angle = math.degrees(math.atan2(abs(y1 - y0), abs(x1 - x0)))
            axis += angle <= 1.0 or angle >= 89.0
        features.lines_axis_frac = axis / len(segments)
        total = sum(s.length for s in segments) or 1.0
        if ctx.inside is not None:
            merged = shapely.MultiLineString([list(s.coords) for s in segments])
            features.lines_outside_frac = merged.difference(ctx.inside).length / total
        if ctx.outline is not None:
            on = sum(s.length for s in segments if ctx.outline.covers(s))
            features.lines_on_boundary_frac = on / total
    if g.inserts:
        features.insert_blocks = dict(Counter(str(i.block) for i in g.inserts))
        features.insert_attrib_frac = sum(bool(i.attributes) for i in g.inserts) / len(g.inserts)
    if g.others:
        features.other_types = dict(Counter(o.dxftype for o in g.others))
    return features


def compute_layer_features(floor: str, geoms: Mapping[str, LayerGeometry]) -> list[LayerFeatures]:
    """Features for every non-empty layer of one floor."""
    ctx = _floor_context(floor, geoms)
    return [_layer_features(ctx, g) for g in geoms.values() if g.entity_count]


# --- decisions ---------------------------------------------------------------------------------------


@dataclass
class LayerDecision:
    semantic: str
    confidence: float
    method: str
    evidence: list[str] = field(default_factory=list)
    alternatives: dict[str, float] = field(default_factory=dict)


@dataclass
class LayerResult:
    floor: str
    layer: str
    semantic: str
    confidence: float
    method: str
    evidence: list[str]
    alternatives: dict[str, float]
    features: LayerFeatures
    threshold: float = REVIEW_THRESHOLD

    @property
    def needs_review(self) -> bool:
        return self.confidence < self.threshold

    def to_row(self) -> dict[str, Any]:
        c = self.features.counts
        return {
            "floor": self.floor,
            "layer": self.layer,
            "semantic": self.semantic,
            "semantic_ko": SEMANTIC_NAMES_KO.get(self.semantic, self.semantic),
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "evidence": list(self.evidence),
            "alternatives": dict(self.alternatives),
            "needs_review": self.needs_review,
            "entities": {k: c.get(k, 0) for k in ("closed", "open", "lines", "texts", "inserts", "other")},
            "polygons": self.features.n_polygons,
        }

    def to_review(self) -> dict[str, Any]:
        row = self.to_row()
        row["sample_texts"] = self.features.sample_texts[:6]
        row["question"] = (
            f"{self.floor} 레이어 '{self.layer}'의 의미가 불확실하다 "
            f"(추정: {SEMANTIC_NAMES_KO.get(self.semantic, self.semantic)}, 신뢰도 {self.confidence:.2f}). 확인 필요."
        )
        return row


@dataclass
class LayerClassification:
    results: list[LayerResult]
    threshold: float = REVIEW_THRESHOLD
    llm_errors: list[str] = field(default_factory=list)

    def get(self, floor: str, layer: str) -> LayerResult | None:
        for result in self.results:
            if result.floor == floor and result.layer == layer:
                return result
        return None

    def semantic_of(self, floor: str, layer: str) -> str | None:
        result = self.get(floor, layer)
        return result.semantic if result else None

    def layers_for(self, floor: str, *semantics: str) -> list[str]:
        return [r.layer for r in self.results if r.floor == floor and r.semantic in semantics]

    @property
    def review_queue(self) -> list[LayerResult]:
        return [r for r in self.results if r.confidence < self.threshold]

    def table(self) -> list[dict[str, Any]]:
        return [r.to_row() for r in self.results]


@runtime_checkable
class LayerClassifier(Protocol):
    """Anything that maps layer features to decisions (None = no opinion)."""

    method: str

    def classify(self, layers: Sequence[LayerFeatures]) -> list[LayerDecision | None]: ...


# --- manifest ----------------------------------------------------------------------------------------


def _layer_name_map(layers: Any) -> dict[str, str]:
    """Layer name -> semantic from a ``{semantic: name | [names]}`` mapping (unknown semantics ignored)."""
    mapping: dict[str, str] = {}
    for semantic, names in dict(layers or {}).items():
        if semantic not in SEMANTICS:
            continue
        for name in [names] if isinstance(names, str) else list(names):
            mapping[str(name)] = semantic
    return mapping


class ManifestClassifier:
    """Uses the manifest ``layers`` mapping (semantic -> layer name or list of names) when present.

    ``layers_by_floor`` (``{floor: {semantic: names}}``, e.g. human review answers for one floor) wins over
    ``layers`` on its floor.
    """

    method = "manifest"

    def __init__(self, manifest: Mapping[str, Any] | None) -> None:
        manifest = manifest or {}
        self.mapping: dict[str, str] = _layer_name_map(manifest.get("layers"))
        self.by_floor: dict[str, dict[str, str]] = {
            str(floor): _layer_name_map(layers)
            for floor, layers in dict(manifest.get("layers_by_floor") or {}).items()
        }

    def _decide(self, f: LayerFeatures) -> LayerDecision | None:
        floor_map = self.by_floor.get(f.floor, {})
        if f.layer in floor_map:
            semantic = floor_map[f.layer]
            return LayerDecision(
                semantic, 1.0, self.method, [f"매니페스트 layers_by_floor 매핑({f.floor}): {semantic}"]
            )
        if f.layer in self.mapping:
            semantic = self.mapping[f.layer]
            return LayerDecision(semantic, 1.0, self.method, [f"매니페스트 layers 매핑: {semantic}"])
        return None

    def classify(self, layers: Sequence[LayerFeatures]) -> list[LayerDecision | None]:
        return [self._decide(f) for f in layers]


# --- heuristics --------------------------------------------------------------------------------------

_NAME_KEYWORDS: dict[str, tuple[tuple[str, float], ...]] = {
    "exterior": (
        ("OUTLINE", 0.6), ("OTLN", 0.6), ("EXTR", 0.6), ("EXTERIOR", 0.6), ("BLDG", 0.5),
        ("외곽", 0.6), ("외벽", 0.6), ("건물", 0.5),
    ),
    "wall": (
        ("WALL", 0.6), ("WALLS", 0.6), ("COLUMN", 0.5), ("STRUCT", 0.4),
        ("벽", 0.6), ("벽체", 0.6), ("구조벽", 0.6), ("기둥", 0.5), ("구조", 0.4),
    ),
    "unit": (
        ("UNIT", 0.6), ("TENANT", 0.6), ("SHOP", 0.6), ("STORE", 0.6), ("BNDY", 0.5), ("LEASE", 0.5),
        ("매장", 0.6), ("구획", 0.6), ("임대", 0.5), ("점포", 0.6), ("테넌트", 0.6),
    ),
    "zone": (("ZONE", 0.6), ("ZONING", 0.6), ("AREA", 0.35), ("존", 0.6), ("조닝", 0.6), ("구역", 0.6)),
    "walkway": (
        ("CORR", 0.6), ("CORRIDOR", 0.6), ("CIRC", 0.6), ("WALKWAY", 0.6), ("AISLE", 0.6),
        ("통로", 0.6), ("복도", 0.6), ("동선", 0.6), ("보행", 0.6),
    ),
    "escalator": (("ESC", 0.6), ("ESCL", 0.6), ("ESCALATOR", 0.6), ("ES", 0.45), ("에스컬레이터", 0.6)),
    "elevator": (
        ("ELEV", 0.6), ("ELV", 0.6), ("EVTR", 0.6), ("ELEVATOR", 0.6), ("LIFT", 0.6), ("EV", 0.45),
        ("엘리베이터", 0.6), ("승강기", 0.6),
    ),
    "stair": (("STAIR", 0.6), ("STAIRS", 0.6), ("STAI", 0.6), ("STRS", 0.6), ("STR", 0.45), ("계단", 0.6)),
    "opening": (
        ("DOOR", 0.6), ("DOORS", 0.6), ("OPENING", 0.6), ("ENTRANCE", 0.6), ("GATE", 0.5),
        ("출입구", 0.6), ("출입", 0.6), ("입구", 0.6),
    ),
    "amenity": (
        ("AMEN", 0.6), ("AMENITY", 0.6), ("FACILITY", 0.5), ("FAC", 0.45), ("TOILET", 0.6),
        ("편의시설", 0.6), ("공용시설", 0.6), ("편의", 0.5), ("시설", 0.4), ("화장실", 0.6),
    ),
    "fixture": (
        ("FIXT", 0.6), ("FIXTURE", 0.6), ("FURN", 0.6), ("FURNITURE", 0.6), ("EQPM", 0.5),
        ("집기", 0.6), ("가구", 0.6), ("비품", 0.5), ("진열", 0.5),
    ),
    "annotation": (
        ("ANNO", 0.6), ("NOTE", 0.5), ("NOTES", 0.5), ("TITLE", 0.6), ("TITL", 0.6),
        ("주석", 0.6), ("도면명", 0.6), ("표제", 0.6), ("범례", 0.5),
    ),
    "noise": (
        ("DIM", 0.6), ("DIMS", 0.6), ("DIMENSION", 0.6), ("MEMO", 0.6), ("TEMP", 0.6), ("TMP", 0.5),
        ("XREF", 0.6), ("OLD", 0.5), ("DEFPOINTS", 0.6),
        ("치수", 0.6), ("메모", 0.6), ("임시", 0.6), ("참조", 0.6), ("구도면", 0.6), ("보조선", 0.6),
    ),
}  # fmt: skip
_EXACT_NAMES: dict[str, tuple[str, float]] = {
    "E-S": ("escalator", 0.6),
    "E-V": ("elevator", 0.6),
    "문": ("opening", 0.6),
}
_LABEL_TOKENS = ("IDEN", "TXT", "TEXT", "NAME", "LABEL", "TAG")
_LABEL_WORDS_KO = ("문자", "번호", "명칭", "이름")
_LABELED = {"unit": "unit_label", "zone": "zone_label", "amenity": "amenity_label"}
_NAME_SPLIT = re.compile(r"[^0-9A-Z\uac00-\ud7a3]+")


def name_scores(layer: str) -> dict[str, tuple[float, str]]:
    """Keyword prior from the layer name: semantic -> (score, evidence)."""
    raw = layer.strip()
    upper = raw.upper()
    if upper in _EXACT_NAMES:
        semantic, score = _EXACT_NAMES[upper]
        return {semantic: (score, f"레이어명 '{raw}' = {SEMANTIC_NAMES_KO[semantic]} 약어")}
    tokens = {t for t in _NAME_SPLIT.split(upper) if t}
    ascii_tokens = {t for t in tokens if t.isascii()}
    hits: list[tuple[str, str, float]] = []
    for semantic, keywords in _NAME_KEYWORDS.items():
        for keyword, score in keywords:
            if (keyword in ascii_tokens) if keyword.isascii() else (keyword in raw):
                hits.append((semantic, keyword, score))
    korean = [k for _, k, _ in hits if not k.isascii()]
    hits = [
        (s, k, v)
        for s, k, v in hits
        if k.isascii() or not any(k != o and k in o and o in raw for o in korean)
    ]
    result: dict[str, tuple[float, str]] = {}
    for semantic, keyword, score in hits:
        if score > result.get(semantic, (0.0, ""))[0]:
            result[semantic] = (score, f"레이어명 키워드 '{keyword}' → {SEMANTIC_NAMES_KO[semantic]}")
    is_label = (
        bool(ascii_tokens & set(_LABEL_TOKENS))
        or any(w in raw for w in _LABEL_WORDS_KO)
        or raw.endswith("명")
    )
    if is_label:
        for base, labeled in _LABELED.items():
            if base in result:
                score, _ = result.pop(base)
                result[labeled] = (max(score, 0.55), f"레이어명에 {base} 키워드와 문자/명칭 표시가 함께 있음")
    return result


def content_scores(f: LayerFeatures) -> dict[str, tuple[float, str]]:
    """Content-based evidence: semantic -> (score, evidence)."""
    scores: dict[str, tuple[float, str]] = {}

    def put(semantic: str, score: float, evidence: str) -> None:
        if score > scores.get(semantic, (0.0, ""))[0]:
            scores[semantic] = (min(score, 0.97), evidence)

    c = f.counts
    n_text, n_ins = c.get("texts", 0), c.get("inserts", 0)
    n_lines = c.get("lines", 0) + c.get("open", 0)
    total = max(f.n_entities, 1)
    kinds = Counter(f.text_kinds)
    pl_polys = f.n_polyline_polygons
    if n_ins:
        share = n_ins / total
        if n_ins >= 3 and share >= 0.6:
            put("fixture", 0.9, f"블록 참조 {n_ins}개가 레이어의 {share:.0%}를 차지")
        else:
            put("fixture", 0.6 if share >= 0.3 else 0.3, f"블록 참조 {n_ins}개")
    if f.other_types.get("DIMENSION"):
        put("noise", 0.7, f"치수 엔티티 {f.other_types['DIMENSION']}개")
    if n_text:
        codes = kinds["unit_code"] + kinds["unit_combined"]
        if kinds["link"]:
            link_type = Counter(f.link_types).most_common(1)[0][0] if f.link_types else None
            if link_type:
                put(
                    link_type,
                    0.92 if pl_polys else 0.6,
                    f"수직동선 ID/명칭 문자 {kinds['link']}개({SEMANTIC_NAMES_KO[link_type]})"
                    + (" + 닫힌 폴리곤" if pl_polys else ""),
                )
        if kinds["opening"]:
            put(
                "opening",
                0.9 if n_lines else 0.65,
                f"출입구 ID 문자 {kinds['opening']}개" + (" + 외곽 위 선" if n_lines else ""),
            )
        if kinds["zone_code"] / n_text >= 0.5:
            put(
                "zone" if pl_polys else "zone_label",
                0.9,
                f"존 코드 문자 {kinds['zone_code']}/{n_text}개",
            )
        if codes >= 2 and codes / n_text >= 0.3:
            if pl_polys and f.unit_code_hits_frac >= 0.6 and f.unit_code_hits_mean <= 1.3:
                put("unit", 0.9, f"매장 코드 문자 {codes}개와 매장 경계 폴리곤이 같은 레이어에 있음")
            else:
                put("unit_label", 0.92, f"매장 코드 패턴 문자 {codes}/{n_text}개")
        if kinds["amenity"] / n_text >= 0.5:
            if pl_polys:
                put("amenity", 0.9, f"편의시설 명칭 문자 {kinds['amenity']}개 + 폴리곤")
            else:
                put("amenity_label", 0.88, f"편의시설 명칭 문자 {kinds['amenity']}/{n_text}개")
        if kinds["numeric"] / n_text >= 0.5:
            bonus = 0.1 if f.lines_outside_frac >= 0.5 else 0.0
            put("noise", 0.6 + bonus, f"숫자만 있는 치수형 문자 {kinds['numeric']}/{n_text}개")
        if kinds["title"] and n_text <= 3:
            put("annotation", 0.68, f"도면 제목형 문자: {f.sample_texts[0][:30]!r}")
        elif (
            n_text <= 2
            and kinds["free"] == n_text
            and (f.texts_outside_frac >= 0.5 or f.text_height_median >= 1.0)
        ):
            put("annotation", 0.62, "건물 밖 또는 큰 글자의 단독 문자")
        if n_text >= 3 and kinds["free"] / n_text >= 0.6 and not pl_polys and not n_ins:
            put(
                "noise",
                0.55 + (0.05 if f.texts_outside_frac >= 0.2 else 0.0),
                f"패턴 없는 메모성 문자 {kinds['free']}개",
            )
            put("annotation", 0.4, "패턴 없는 문자")
    if pl_polys:
        if f.largest_share >= 0.85 and f.contains_frac >= 0.8 and f.n_polygons <= 3:
            put("exterior", 0.88, f"가장 큰 폴리곤이 층 전체({f.largest_share:.0%})를 덮고 다른 요소를 포함")
        if f.n_polygons >= 2 and f.unit_code_hits_frac >= 0.6:
            if f.unit_code_hits_mean <= 1.3:
                put(
                    "unit",
                    0.88,
                    f"닫힌 폴리곤 {f.n_polygons}개 중 {f.unit_code_hits_frac:.0%}가 매장 코드를 1개씩 포함",
                )
            else:
                put(
                    "zone",
                    0.85,
                    f"폴리곤마다 매장 코드 평균 {f.unit_code_hits_mean:.1f}개 포함(여러 매장을 묶은 구역)",
                )
        elif f.n_polygons == 1 and f.unit_code_hits_frac == 1.0 and f.largest_share < 0.85:
            if f.unit_code_hits_mean <= 1.3:
                put("unit", 0.5, "매장 코드 1개를 포함한 폴리곤 1개")
            else:
                put("zone", 0.5, f"매장 코드 {f.unit_code_hits_mean:.0f}개를 포함한 폴리곤 1개")
        unlabeled = f.unit_code_hits_frac < 0.3
        if unlabeled and f.amenity_hits_frac >= 0.5 and f.n_polygons <= 8:
            put("amenity", 0.85, f"폴리곤 {f.amenity_hits_frac:.0%}가 편의시설 명칭을 포함")
        if unlabeled and f.link_hits_frac >= 0.5 and f.n_polygons <= 8 and f.link_hit_types:
            link_type = Counter(f.link_hit_types).most_common(1)[0][0]
            put(link_type, 0.75, f"폴리곤이 수직동선 라벨({SEMANTIC_NAMES_KO[link_type]})을 포함")
        if (
            unlabeled
            and f.n_polygons >= 2
            and f.amenity_hits_frac < 0.5
            and f.link_hits_frac < 0.5
            and f.largest_share < 0.85
        ):
            score = (
                0.6 + (0.15 if f.union_parts == 1 else 0.0) + (0.08 if f.elongation_median >= 2.5 else 0.0)
            )
            put(
                "walkway",
                score,
                f"라벨 없는 폴리곤 {f.n_polygons}개, 합집합 {f.union_parts}개 덩어리, 세장비 중앙값 "
                f"{f.elongation_median:.1f}",
            )
    elif f.n_polygons and n_lines and not n_text and not n_ins:
        if f.unit_code_hits_frac >= 0.6:
            put("unit", 0.8, f"LINE으로 닫힌 영역 {f.n_polygons}개가 매장 코드를 포함")
        elif f.loops_match_other >= 0.5 and f.lines_axis_frac >= 0.9:
            put(
                "wall",
                0.68,
                f"LINE이 다른 레이어의 공간 경계(계단·승강기 등)와 겹치는 닫힌 영역 {f.n_polygons}개를 형성",
            )
        else:
            put("wall", 0.55, f"LINE으로 닫힌 영역 {f.n_polygons}개")
    if n_lines and not n_text and not n_ins and not pl_polys:
        if f.lines_on_boundary_frac >= 0.8:
            put("opening", 0.62, f"선 {f.lines_on_boundary_frac:.0%}가 건물 외곽선 위에 있음")
        elif f.lines_outside_frac >= 0.5:
            put("noise", 0.6, f"선 {f.lines_outside_frac:.0%}가 건물 밖에 있음")
        elif f.lines_axis_frac < 0.5:
            put("noise", 0.55, f"축에 맞지 않는 임의 방향 선 {f.n_segments}개")
        elif not f.n_polygons:
            put("wall", 0.45, f"축 정렬 선 {f.n_segments}개")
            put("noise", 0.35, "닫힌 영역을 만들지 않는 선")
    return scores


def combine_scores(
    content: Mapping[str, tuple[float, str]], name: Mapping[str, tuple[float, str]]
) -> LayerDecision:
    """Noisy-OR per semantic, minus a penalty for the strongest competing semantic."""
    semantics = set(content) | set(name)
    if not semantics:
        return LayerDecision("noise", 0.2, "heuristic", ["내용·레이어명 근거 없음"])
    combined = {
        s: 1 - (1 - content.get(s, (0.0, ""))[0]) * (1 - name.get(s, (0.0, ""))[0]) for s in semantics
    }
    ranked = sorted(combined.items(), key=lambda kv: (-kv[1], SEMANTICS.index(kv[0])))
    best, score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = max(0.05, min(0.99, score - 0.25 * second))
    evidence = [ev for ev in (content.get(best, (0, ""))[1], name.get(best, (0, ""))[1]) if ev]
    if len(ranked) > 1 and second >= 0.4:
        evidence.append(f"경합: {SEMANTIC_NAMES_KO[ranked[1][0]]}({second:.2f})")
    alternatives = {s: round(v, 3) for s, v in ranked[1:4]}
    return LayerDecision(best, round(confidence, 3), "heuristic", evidence, alternatives)


class HeuristicClassifier:
    """Korean/English name keywords combined with content features; deterministic."""

    method = "heuristic"

    def classify(self, layers: Sequence[LayerFeatures]) -> list[LayerDecision | None]:
        decisions = [combine_scores(content_scores(f), name_scores(f.layer)) for f in layers]
        self._exclusive(layers, decisions)
        return list(decisions)

    @staticmethod
    def _exclusive(layers: Sequence[LayerFeatures], decisions: list[LayerDecision]) -> None:
        """One exterior per floor; among several unit layers the one with more codes per polygon is a zone."""
        by_floor: dict[str, list[int]] = defaultdict(list)
        for i, f in enumerate(layers):
            by_floor[f.floor].append(i)
        for indices in by_floor.values():
            exteriors = [i for i in indices if decisions[i].semantic == "exterior"]
            if len(exteriors) > 1:
                keep = max(exteriors, key=lambda i: layers[i].area_max_m2)
                for i in exteriors:
                    if i != keep and decisions[i].alternatives:
                        alt, score = next(iter(decisions[i].alternatives.items()))
                        decisions[i] = LayerDecision(
                            alt,
                            min(score, 0.6),
                            "heuristic",
                            [*decisions[i].evidence, "층에 외곽 레이어가 여럿"],
                        )
            units = [i for i in indices if decisions[i].semantic == "unit" and layers[i].n_polygons]
            if len(units) > 1:
                base = min(layers[i].unit_code_hits_mean for i in units)
                for i in units:
                    if layers[i].unit_code_hits_mean >= max(1.3, base * 1.3):
                        decisions[i] = LayerDecision(
                            "zone",
                            0.75,
                            "heuristic",
                            [*decisions[i].evidence, "다른 매장 레이어보다 코드 밀도가 높음"],
                        )


# --- LLM ---------------------------------------------------------------------------------------------

LAYER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "layer": {"type": "string"},
        "semantic": {"type": "string", "enum": list(SEMANTICS)},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
    "required": ["layer", "semantic", "confidence", "evidence"],
    "additionalProperties": False,
}
_LAYER_SYSTEM = (
    "You classify layers of a CAD floor plan of a Korean department store. Layer names may be Korean, "
    "English, abbreviations or meaningless (e.g. '0', 'LAYER_17'). Decide the layer's semantic from the "
    "allowed list using the name and the content summary (entity counts, polygon statistics, text kinds and "
    "samples, line statistics, block names). Semantics: exterior=building outline, wall=walls/partitions, "
    "unit=tenant unit boundaries, unit_label=unit code/brand texts, zone=MD zone boundaries (groups of units), "
    "zone_label=zone code/name texts, walkway=corridor polygons, escalator/elevator/stair=vertical "
    "circulation, opening=entrances/doors on the outline, amenity=restroom/info/ATM/lounge spaces, "
    "amenity_label=their names, fixture=furniture/fixture blocks, annotation=title/notes, "
    "noise=dimensions, memos, temporary or reference geometry. Field guide: polygons.sources counts closed "
    "polylines ('closed'), nearly closed polylines ('auto_closed') and loops formed by separate LINE entities "
    "('lines'); line_loops_matching_other_layers near 1 means the LINE loops trace spaces drawn on other "
    "layers (line_loops_traced_layers names them); a LINE-only layer whose loops trace several different kinds "
    "of spaces at once (e.g. stairs, elevators and restrooms) is usually walls; with_unit_code_frac and "
    "unit_codes_per_polygon count unit-code texts (from any layer) inside the polygons: about one per polygon "
    "means tenant units, several per polygon means zones, none means walkways or other spaces; "
    "with_link_label_frac and with_amenity_label_frac count polygons containing vertical-link or amenity "
    "labels from any layer. Return JSON only: "
    '{"layer": str, "semantic": str, "confidence": number 0..1, "evidence": short reason}.'
)


def parse_json_object(text: str) -> dict[str, Any]:
    """First JSON object in ``text`` (tolerates code fences and surrounding prose)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in LLM response")
    return json.loads(text[start : end + 1])


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return str(text)
    chunks: list[str] = []
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            value = getattr(content, "text", None)
            if value:
                chunks.append(str(value))
    if chunks:
        return "".join(chunks)
    choices = getattr(response, "choices", None)
    if choices:
        return str(choices[0].message.content)
    return str(response)


def request_json(
    client: Any, model: str, system: str, payload: Mapping[str, Any], schema: Mapping[str, Any], name: str
) -> dict[str, Any]:
    """One Responses API call with a strict JSON schema (plain JSON prompting as a fallback)."""
    messages = [
        {"type": "message", "role": "system", "content": system},
        {"type": "message", "role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    text_format = {"format": {"type": "json_schema", "name": name, "schema": dict(schema), "strict": True}}
    try:
        response = client.responses.create(model=model, input=messages, text=text_format)
    except TypeError:
        response = client.responses.create(model=model, input=messages)
    return parse_json_object(_response_text(response))


class LlmClassifier:
    """LLM fallback for ambiguous layers; returns None (no opinion) on any failure."""

    method = "llm"

    def __init__(self, client: Any, model: str, max_texts: int = 8) -> None:
        self.client = client
        self.model = model
        self.max_texts = max_texts
        self.calls = 0
        self.errors: list[str] = []
        self._cache: dict[str, LayerDecision | None] = {}

    def classify(self, layers: Sequence[LayerFeatures]) -> list[LayerDecision | None]:
        return [self._classify_one(f) for f in layers]

    def _classify_one(self, f: LayerFeatures) -> LayerDecision | None:
        payload = {"allowed_semantics": list(SEMANTICS), **f.summary(self.max_texts)}
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if key in self._cache:
            return self._cache[key]
        decision: LayerDecision | None = None
        try:
            self.calls += 1
            data = request_json(
                self.client, self.model, _LAYER_SYSTEM, payload, LAYER_SCHEMA, "layer_semantic"
            )
            semantic = str(data.get("semantic", "")).strip().lower()
            if semantic not in SEMANTICS:
                raise ValueError(f"unknown semantic {semantic!r}")
            confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
            evidence = str(data.get("evidence", "")).strip()[:300]
            decision = LayerDecision(
                semantic, round(confidence, 3), self.method, [f"LLM({self.model}): {evidence}"]
            )
        except Exception as exc:
            self.errors.append(f"{f.floor}/{f.layer}: {type(exc).__name__}: {str(exc)[:120]}")
        self._cache[key] = decision
        return decision


def load_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` pairs from a dotenv file (values are never logged)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return values


def foundry_settings(
    env_file: Path | None = None, model_env: str = "MINI_MODEL_DEPLOYMENT"
) -> tuple[str, str]:
    """``(project endpoint, model deployment)`` from the environment or the repository ``.env``."""
    candidates = (
        [env_file] if env_file else [Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"]
    )
    values: dict[str, str] = {}
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            values = load_env_file(candidate)
            break
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or values.get("FOUNDRY_PROJECT_ENDPOINT")
    model = os.environ.get(model_env) or values.get(model_env)
    if not endpoint or not model:
        raise RuntimeError(f"FOUNDRY_PROJECT_ENDPOINT/{model_env} is not configured")
    return endpoint, model


def foundry_openai_client(endpoint: str) -> Any:
    """OpenAI-compatible client of a Foundry project (Azure CLI credential)."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    return AIProjectClient(endpoint=endpoint, credential=AzureCliCredential()).get_openai_client()


def foundry_llm_classifier(env_file: Path | None = None) -> LlmClassifier:
    endpoint, model = foundry_settings(env_file)
    return LlmClassifier(foundry_openai_client(endpoint), model)


# --- pipeline entry ----------------------------------------------------------------------------------

WEAK_EVIDENCE = 0.4


def merge_second_opinion(current: LayerDecision, answer: LayerDecision) -> LayerDecision:
    """Combine a below-threshold decision with a second opinion (e.g. the LLM).

    - Agreement: noisy-OR of both confidences (usually leaves the review queue).
    - Disagreement with weak deterministic evidence (< ``WEAK_EVIDENCE``): the second opinion is adopted.
    - Disagreement otherwise: the deterministic semantic and confidence are kept, the second opinion is recorded
      as an alternative and the layer stays in the human review queue.
    """
    if answer.semantic == current.semantic:
        confidence = 1 - (1 - answer.confidence) * (1 - current.confidence)
        return LayerDecision(
            current.semantic,
            round(min(confidence, 0.99), 3),
            f"{current.method}+{answer.method}",
            [*current.evidence, *answer.evidence],
            current.alternatives,
        )
    if current.confidence < WEAK_EVIDENCE and answer.confidence > current.confidence:
        return LayerDecision(
            answer.semantic,
            answer.confidence,
            answer.method,
            [
                *answer.evidence,
                f"근거가 약한 휴리스틱 추정 {current.semantic}({current.confidence:.2f})을 대체",
            ],
            {current.semantic: current.confidence, **current.alternatives},
        )
    note = f"{answer.method} 의견 불일치: {answer.semantic}({answer.confidence:.2f}) - 사람 확인 필요"
    alternatives = {**current.alternatives, answer.semantic: answer.confidence}
    return LayerDecision(
        current.semantic,
        current.confidence,
        current.method,
        [*current.evidence, note, *answer.evidence],
        alternatives,
    )


def classify_layers(
    features: Sequence[LayerFeatures],
    manifest: Mapping[str, Any] | None = None,
    heuristic: LayerClassifier | None = None,
    fallback: LayerClassifier | None = None,
    threshold: float = REVIEW_THRESHOLD,
) -> LayerClassification:
    """Manifest mapping first, then heuristics, then ``fallback`` (e.g. LLM) below ``threshold``.

    Layers still below ``threshold`` form the human review queue.
    """
    features = list(features)
    manifest_decisions = ManifestClassifier(manifest).classify(features)
    heuristic_decisions = (heuristic or HeuristicClassifier()).classify(features)
    decisions: list[LayerDecision] = []
    for manifest_decision, heuristic_decision in zip(manifest_decisions, heuristic_decisions, strict=True):
        decisions.append(manifest_decision or heuristic_decision or LayerDecision("noise", 0.0, "none"))
    errors: list[str] = []
    if fallback is not None:
        pending = [i for i, d in enumerate(decisions) if d.confidence < threshold]
        if pending:
            answers = fallback.classify([features[i] for i in pending])
            for i, answer in zip(pending, answers, strict=True):
                if answer is not None:
                    decisions[i] = merge_second_opinion(decisions[i], answer)
        errors = list(getattr(fallback, "errors", []) or [])
    results = [
        LayerResult(
            f.floor, f.layer, d.semantic, d.confidence, d.method, d.evidence, d.alternatives, f, threshold
        )
        for f, d in zip(features, decisions, strict=True)
    ]
    return LayerClassification(results, threshold, errors)
