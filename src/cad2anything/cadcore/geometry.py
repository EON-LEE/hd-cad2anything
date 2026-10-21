"""Polygon reconstruction from CAD linework (meters).

- Closed polylines become polygons.
- Open polylines whose end gap is at most ``CLOSE_TOL_M`` are closed automatically (issue ``auto_closed``).
- LINEs and the remaining open polylines are snapped (endpoints within ``SNAP_TOL_M``) and polygonized
  (issue ``polygonized_from_lines``).
- Invalid rings are repaired, slivers dropped and overlapping duplicates (IoU >= ``DUPLICATE_IOU``) removed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from cad2anything.cadcore.reader import FloorDrawing, Issue, Primitive

SNAP_TOL_M = 0.05
CLOSE_TOL_M = 0.05
MIN_AREA_M2 = 0.01
DUPLICATE_IOU = 0.98
T_SNAP_MAX_LINES = 2000
_SOURCE_ORDER = {"closed": 0, "auto_closed": 1, "lines": 2}


@dataclass
class PolygonRecord:
    polygon: Polygon
    layer: str
    source: str
    handles: list[str]
    gap_m: float = 0.0


@dataclass
class LayerGeometry:
    """Reconstructed geometry and raw primitives of one layer on one floor."""

    layer: str
    polygons: list[PolygonRecord] = field(default_factory=list)
    lines: list[LineString] = field(default_factory=list)
    raw_lines: list[LineString] = field(default_factory=list)
    texts: list[Primitive] = field(default_factory=list)
    inserts: list[Primitive] = field(default_factory=list)
    others: list[Primitive] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    issues: list[Issue] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        c = self.counts
        return c["closed"] + c["open"] + c["lines"] + c["texts"] + c["inserts"] + c["other"]


def clean_points(points: Sequence[tuple[float, float]], eps: float = 1e-9) -> list[tuple[float, float]]:
    """Drop consecutive duplicate vertices and a repeated closing vertex."""
    result: list[tuple[float, float]] = []
    for x, y in points:
        if not result or abs(result[-1][0] - x) > eps or abs(result[-1][1] - y) > eps:
            result.append((float(x), float(y)))
    if len(result) > 1 and math.dist(result[0], result[-1]) <= eps:
        result.pop()
    return result


def polygon_parts(geometry: BaseGeometry | None) -> list[Polygon]:
    """Non-empty polygons contained in any (multi/collection) geometry."""
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon) or hasattr(geometry, "geoms"):
        return [p for g in geometry.geoms for p in polygon_parts(g)]
    return []


def largest_polygon(geometry: BaseGeometry | None) -> Polygon | None:
    parts = polygon_parts(geometry)
    return max(parts, key=lambda p: p.area) if parts else None


def ring_polygon(points: Sequence[tuple[float, float]]) -> tuple[Polygon | None, bool]:
    """Polygon from a ring (repaired when invalid); returns ``(polygon, repaired)``."""
    pts = clean_points(points)
    if len(pts) < 3:
        return None, False
    polygon = Polygon(pts)
    repaired = False
    if not polygon.is_valid:
        polygon = largest_polygon(shapely.make_valid(polygon))
        repaired = True
    if polygon is None or polygon.area < MIN_AREA_M2:
        return None, repaired
    return shapely.normalize(polygon.simplify(0.0)), repaired


def snap_endpoints(lines: list[list[tuple[float, float]]], tol: float) -> list[list[tuple[float, float]]]:
    """Merge line endpoints closer than ``tol`` into their cluster mean (union-find on a grid)."""
    ends: list[tuple[float, float]] = []
    for coords in lines:
        ends.extend((coords[0], coords[-1]))
    if not ends or tol <= 0:
        return lines
    parent = list(range(len(ends)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x, y) in enumerate(ends):
        cells[(math.floor(x / tol), math.floor(y / tol))].append(i)
    for i, (x, y) in enumerate(ends):
        cx, cy = math.floor(x / tol), math.floor(y / tol)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in cells.get((cx + dx, cy + dy), ()):
                    if j > i and math.dist(ends[i], ends[j]) <= tol:
                        parent[find(j)] = find(i)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(ends)):
        groups[find(i)].append(i)
    snapped = list(ends)
    for members in groups.values():
        if len(members) > 1:
            mx = sum(ends[i][0] for i in members) / len(members)
            my = sum(ends[i][1] for i in members) / len(members)
            for i in members:
                snapped[i] = (mx, my)
    result = []
    for k, coords in enumerate(lines):
        new = list(coords)
        new[0], new[-1] = snapped[2 * k], snapped[2 * k + 1]
        result.append(new)
    return result


def polygonize_lines(
    lines: list[list[tuple[float, float]]], tol: float = SNAP_TOL_M
) -> tuple[list[Polygon], list[LineString]]:
    """Polygons enclosed by snapped linework and the leftover (dangling) lines."""
    snapped = [c for c in snap_endpoints(lines, tol) if len(c) >= 2]
    geoms = [LineString(c) for c in snapped]
    geoms = [g for g in geoms if g.length > 1e-9]
    if not geoms:
        return [], []
    merged = unary_union(geoms)
    if len(geoms) <= T_SNAP_MAX_LINES:
        merged = unary_union([shapely.snap(g, merged, tol) for g in geoms])
    polygons = [p for p in polygonize(merged) if p.area >= MIN_AREA_M2]
    if not polygons:
        return [], geoms
    boundary = unary_union([p.exterior for p in polygons]).buffer(tol / 2)
    leftover = [g for g in geoms if not boundary.covers(g)]
    return [shapely.normalize(p.simplify(0.0)) for p in polygons], leftover


def iou(a: BaseGeometry, b: BaseGeometry) -> float:
    inter = a.intersection(b).area
    if inter <= 0:
        return 0.0
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def dedupe(records: list[PolygonRecord], threshold: float = DUPLICATE_IOU) -> tuple[list[PolygonRecord], int]:
    """Drop polygons overlapping an earlier (preferred) polygon with IoU >= ``threshold``."""
    ordered = sorted(records, key=lambda r: _SOURCE_ORDER.get(r.source, 9))
    if len(ordered) < 2:
        return ordered, 0
    tree = STRtree([r.polygon for r in ordered])
    keep = [True] * len(ordered)
    for i, record in enumerate(ordered):
        if not keep[i]:
            continue
        for j in tree.query(record.polygon):
            j = int(j)
            if j > i and keep[j] and iou(record.polygon, ordered[j].polygon) >= threshold:
                keep[j] = False
                record.handles.extend(ordered[j].handles)
    kept = [r for r, k in zip(ordered, keep, strict=True) if k]
    return kept, len(ordered) - len(kept)


def subtract_nested(records: list[PolygonRecord], min_ratio: float = 0.999) -> int:
    """Cut polygons nested inside a larger polygon of the same set out of it (outer ring + hole rings).

    The nested polygon is kept as its own record (it may be another part, e.g. of another zone).
    Returns the number of holes applied.
    """
    if len(records) < 2:
        return 0
    order = sorted(range(len(records)), key=lambda i: -records[i].polygon.area)
    tree = STRtree([r.polygon for r in records])
    holes = 0
    for i in order:
        outer = records[i]
        for j in tree.query(outer.polygon):
            j = int(j)
            inner = records[j].polygon
            if j == i or inner.area >= outer.polygon.area * 0.999:
                continue
            if inner.intersection(outer.polygon).area >= min_ratio * inner.area:
                cut = largest_polygon(outer.polygon.difference(inner))
                if cut is not None:
                    outer.polygon = shapely.normalize(cut)
                    holes += 1
    return holes


def overlap_pairs(polygons: Sequence[BaseGeometry], min_area: float = 0.5) -> list[tuple[int, int, float]]:
    """Index pairs whose overlap area exceeds ``min_area``."""
    if len(polygons) < 2:
        return []
    tree = STRtree(list(polygons))
    pairs = []
    for i, polygon in enumerate(polygons):
        for j in tree.query(polygon):
            j = int(j)
            if j > i:
                area = polygon.intersection(polygons[j]).area
                if area > min_area:
                    pairs.append((i, j, area))
    return pairs


def _issue(code: str, message: str, floor: str, layer: str, severity: str = "info", **data: object) -> Issue:
    return Issue(severity, code, message, floor, layer, data=dict(data))


def layer_geometry(
    floor: str,
    layer: str,
    prims: Iterable[Primitive],
    close_tol: float = CLOSE_TOL_M,
    snap_tol: float = SNAP_TOL_M,
) -> LayerGeometry:
    """Reconstruct polygons and linework for one layer."""
    result = LayerGeometry(layer)
    counts = result.counts
    records: list[PolygonRecord] = []
    loose: list[list[tuple[float, float]]] = []
    loose_handles: list[str] = []
    auto_closed: list[tuple[str, float]] = []
    unclosed: list[tuple[str, float, list[tuple[float, float]]]] = []
    repaired: list[str] = []
    for prim in prims:
        if prim.kind == "text":
            counts["texts"] += 1
            result.texts.append(prim)
        elif prim.kind == "insert":
            counts["inserts"] += 1
            result.inserts.append(prim)
        elif prim.kind == "line":
            counts["lines"] += 1
            result.raw_lines.append(LineString(prim.points))
            loose.append(list(prim.points))
            loose_handles.append(prim.handle)
        elif prim.kind == "polyline":
            pts = clean_points(prim.points)
            if prim.closed:
                counts["closed"] += 1
                polygon, fixed = ring_polygon(pts)
                if polygon is not None:
                    records.append(PolygonRecord(polygon, layer, "closed", [prim.handle]))
                if fixed:
                    repaired.append(prim.handle)
                continue
            counts["open"] += 1
            if len(pts) >= 2:
                result.raw_lines.append(LineString(pts))
            gap = math.dist(prim.points[0], prim.points[-1]) if len(prim.points) >= 2 else math.inf
            if len(pts) >= 3 and gap <= close_tol:
                polygon, fixed = ring_polygon(pts)
                if polygon is not None:
                    source = "auto_closed" if gap > 1e-9 else "closed"
                    records.append(PolygonRecord(polygon, layer, source, [prim.handle], gap))
                    if source == "auto_closed":
                        auto_closed.append((prim.handle, gap))
                    if fixed:
                        repaired.append(prim.handle)
                    continue
            if len(pts) >= 2:
                loose.append(pts)
                loose_handles.append(prim.handle)
                if len(pts) >= 3:
                    unclosed.append((prim.handle, gap, pts))
        else:
            counts["other"] += 1
            result.others.append(prim)
    from_lines = 0
    if loose:
        polygons, leftover = polygonize_lines(loose, snap_tol)
        sources = [LineString(c) for c in loose]
        for polygon in polygons:
            edge = polygon.exterior.buffer(snap_tol)
            handles = [h for h, line in zip(loose_handles, sources, strict=True) if edge.covers(line)]
            records.append(PolygonRecord(polygon, layer, "lines", handles))
        from_lines = len(polygons)
        result.lines = leftover
        if polygons and unclosed:
            edges = unary_union([p.exterior for p in polygons]).buffer(snap_tol)
            unclosed = [u for u in unclosed if not edges.covers(LineString(u[2]))]
    kept, duplicates = dedupe(records)
    result.polygons = kept
    counts["polygons"] = len(kept)
    counts["auto_closed"] = len(auto_closed)
    counts["from_lines"] = sum(1 for r in kept if r.source == "lines")
    counts["duplicates"] = duplicates
    if auto_closed:
        result.issues.append(
            _issue(
                "auto_closed",
                f"열린 폴리라인 {len(auto_closed)}개를 끝점 간격 {close_tol * 1000:.0f} mm 이내로 보고 닫았다 "
                f"(최대 간격 {max(g for _, g in auto_closed) * 1000:.0f} mm).",
                floor,
                layer,
                handles=[h for h, _ in auto_closed],
                max_gap_mm=round(max(g for _, g in auto_closed) * 1000, 1),
            )
        )
    if from_lines:
        result.issues.append(
            _issue(
                "polygonized_from_lines",
                f"LINE/열린 선 {len(loose)}개를 스냅({snap_tol * 1000:.0f} mm)하여 폴리곤 {from_lines}개를 구성했다.",
                floor,
                layer,
                handles=sorted({h for r in records if r.source == "lines" for h in r.handles}),
                polygons=from_lines,
                lines=len(loose),
            )
        )
    if unclosed:
        result.issues.append(
            _issue(
                "open_polyline_unclosed",
                f"끝점 간격이 허용치를 넘는 열린 폴리라인 {len(unclosed)}개를 닫지 못했다.",
                floor,
                layer,
                "warning",
                handles=[u[0] for u in unclosed],
                min_gap_m=round(min(u[1] for u in unclosed), 3),
            )
        )
    if repaired:
        result.issues.append(
            _issue(
                "invalid_fixed",
                f"자기교차 등 유효하지 않은 폴리곤 {len(repaired)}개를 보정했다.",
                floor,
                layer,
                "warning",
                handles=repaired,
            )
        )
    if duplicates:
        result.issues.append(
            _issue(
                "duplicate_removed",
                f"겹치는 중복 폴리곤 {duplicates}개를 제거했다 (IoU >= {DUPLICATE_IOU}).",
                floor,
                layer,
                duplicates=duplicates,
            )
        )
    return result


def build_layer_geometry(
    drawing: FloorDrawing, close_tol: float = CLOSE_TOL_M, snap_tol: float = SNAP_TOL_M
) -> dict[str, LayerGeometry]:
    """Per-layer reconstructed geometry for one floor (layers without entities are omitted)."""
    return {
        layer: layer_geometry(drawing.floor, layer, prims, close_tol, snap_tol)
        for layer, prims in drawing.by_layer().items()
    }
