"""Shared spatial computations: walkway grid graphs, access points, unit features and relations.

Geometry is in local meters (SRID 0). Walking distances use a grid graph over the union of a floor's
walkway polygons (cell centers inside the walkways, 8-neighborhood, Euclidean edge weights; an edge is
kept only when its straight segment stays inside the walkways). Units, openings and amenities get one
access point (midpoint of the longest boundary piece shared with a walkway); vertical links get one per
shared piece so every landing side counts. Access points hang off their nearest grid node as dead-end
nodes, and distances to the nearest escalator or entrance come from one labeled multi-source Dijkstra
per floor and target type.
"""

from __future__ import annotations

import heapq
import itertools
import math
import re
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from cad2anything.catalog import FLOOR_HEIGHT_M, VERTICAL_LINKS, floor_program
from cad2anything.model import Entity, EntityKind, Floor, Relation, RelationType, SpatialModel

DEFAULT_CELL_M = 1.0
SHARE_TOL_M = 0.05
MIN_SHARED_M = 0.5
MAX_PARALLEL_DEG = 2.0
ACCESS_KINDS = (EntityKind.UNIT, EntityKind.VERTICAL_LINK, EntityKind.OPENING, EntityKind.AMENITY)
_EPS = 1e-9
_COVER_EPS = 1e-6
_BASEMENT = re.compile(r"^B(\d+)F?$", re.IGNORECASE)
_ABOVE_GROUND = re.compile(r"^(\d+)F$", re.IGNORECASE)

UNIT_FEATURE_COLUMNS = [
    "entity_id",
    "unit_code",
    "floor",
    "brand_id",
    "brand_name",
    "category",
    "zone_code",
    "vacant",
    "area_m2",
    "frontage_m",
    "walk_to_escalator_m",
    "nearest_escalator_id",
    "escalator_fallback",
    "walk_to_entrance_m",
    "nearest_entrance_id",
    "entrance_fallback",
    "centroid_x",
    "centroid_y",
    "access_x",
    "access_y",
]
ADJACENCY_COLUMNS = ["unit_a", "unit_b", "floor", "shared_length_m", "entity_a", "entity_b"]


# --- floors -----------------------------------------------------------------------------------------


def floor_ordinal(code: str, model: SpatialModel | None = None) -> float:
    """Vertical order of a floor code: model floors first, then the catalog, then ``B2``/``3F`` parsing."""
    if model is not None:
        for floor in model.floors:
            if floor.code == code:
                return float(floor.ordinal)
    try:
        return float(floor_program(code).ordinal)
    except KeyError:
        pass
    text = str(code).strip()
    if match := _BASEMENT.match(text):
        return -float(match.group(1))
    if match := _ABOVE_GROUND.match(text):
        return float(match.group(1)) - 1.0
    return math.inf


def floor_codes(model: SpatialModel) -> list[str]:
    """Floor codes of the model (declared floors and entity floors) from bottom to top."""
    codes = {f.code for f in model.floors} | {e.floor for e in model.entities.values() if e.floor}
    return sorted(codes, key=lambda code: (floor_ordinal(code, model), code))


def link_type(entity: Entity) -> str | None:
    """``escalator``/``elevator``/``stair`` from attributes, falling back to the catalog link ID."""
    value = entity.attributes.get("link_type")
    if value:
        return str(value)
    return VERTICAL_LINKS.get(str(entity.attributes.get("link_id", "")))


# --- boundary sharing -------------------------------------------------------------------------------


def _parts(geometry: BaseGeometry | None) -> list[BaseGeometry]:
    if geometry is None or geometry.is_empty:
        return []
    if hasattr(geometry, "geoms"):
        return [part for sub in geometry.geoms for part in _parts(sub)]
    return [geometry]


def boundary_segments(geometry: BaseGeometry | None) -> np.ndarray:
    """Straight boundary segments ``[x0, y0, x1, y1]`` of polygons (all rings) or lines."""
    chunks: list[np.ndarray] = []
    for part in _parts(geometry):
        if isinstance(part, Polygon):
            rings = [part.exterior, *part.interiors]
        elif isinstance(part, LineString):
            rings = [part]
        else:
            continue
        for ring in rings:
            coords = np.asarray(ring.coords, dtype=float)[:, :2]
            if len(coords) >= 2:
                chunks.append(np.hstack([coords[:-1], coords[1:]]))
    if not chunks:
        return np.empty((0, 4))
    segments = np.vstack(chunks)
    keep = np.hypot(segments[:, 2] - segments[:, 0], segments[:, 3] - segments[:, 1]) > _EPS
    return segments[keep]


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + _EPS:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def shared_segments(
    geom_a: BaseGeometry | None, geom_b: BaseGeometry | None, tol: float = SHARE_TOL_M
) -> list[LineString]:
    """Pieces of ``geom_a``'s boundary that run parallel to and within ``tol`` of ``geom_b``'s boundary.

    Edges are matched segment by segment (direction within ``MAX_PARALLEL_DEG``), so boundaries that turn
    away from each other at a corner do not add tolerance-sized slivers to the shared length.
    """
    seg_a = boundary_segments(geom_a)
    seg_b = boundary_segments(geom_b)
    if not len(seg_a) or not len(seg_b):
        return []
    b0, b1 = seg_b[:, :2], seg_b[:, 2:]
    db = b1 - b0
    lb = np.hypot(db[:, 0], db[:, 1])
    sin_tol = math.sin(math.radians(MAX_PARALLEL_DEG))
    pieces: list[LineString] = []
    for x0, y0, x1, y1 in seg_a.tolist():
        length = math.hypot(x1 - x0, y1 - y0)
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        rx0, ry0 = b0[:, 0] - x0, b0[:, 1] - y0
        rx1, ry1 = b1[:, 0] - x0, b1[:, 1] - y0
        t0, t1 = rx0 * ux + ry0 * uy, rx1 * ux + ry1 * uy
        s0, s1 = ry0 * ux - rx0 * uy, ry1 * ux - rx1 * uy
        lo = np.maximum(np.minimum(t0, t1), 0.0)
        hi = np.minimum(np.maximum(t0, t1), length)
        parallel = np.abs(ux * db[:, 1] - uy * db[:, 0]) <= sin_tol * lb
        intervals: list[tuple[float, float]] = []
        for k in np.flatnonzero(parallel & (hi - lo > _EPS)).tolist():
            ta, tb, sa, sb = float(t0[k]), float(t1[k]), float(s0[k]), float(s1[k])
            if tb < ta:
                ta, tb, sa, sb = tb, ta, sb, sa
            slope = (sb - sa) / (tb - ta)
            start, end = float(lo[k]), float(hi[k])
            if abs(slope) <= 1e-12:
                if abs(sa) > tol + _EPS:
                    continue
            else:
                cut_a, cut_b = ta + (tol - sa) / slope, ta + (-tol - sa) / slope
                start, end = max(start, min(cut_a, cut_b)), min(end, max(cut_a, cut_b))
            if end - start > _EPS:
                intervals.append((start, end))
        for start, end in _merge_intervals(intervals):
            pieces.append(LineString([(x0 + ux * start, y0 + uy * start), (x0 + ux * end, y0 + uy * end)]))
    return pieces


def shared_length(
    geom_a: BaseGeometry | None, geom_b: BaseGeometry | None, tol: float = SHARE_TOL_M
) -> float:
    """Length (m) of ``geom_a``'s boundary shared with ``geom_b``'s boundary (touching or within ``tol``)."""
    return float(sum(piece.length for piece in shared_segments(geom_a, geom_b, tol)))


def access_points(
    geometry: BaseGeometry,
    walkable: BaseGeometry | None,
    tol: float = SHARE_TOL_M,
    min_length: float = MIN_SHARED_M,
) -> list[Point]:
    """All access points of an entity, primary first.

    Polygons: midpoints of the boundary pieces shared with ``walkable``, longest first (ties: smallest x,
    then y); pieces shorter than ``min_length`` are dropped unless no piece qualifies. Without a shared
    piece: the boundary point nearest to ``walkable`` (the centroid when the floor has no walkway).
    Lines (openings): midpoint of the longest part. Points: the point itself.
    """
    parts = _parts(geometry)
    if not parts:
        raise ValueError("empty geometry has no access point")
    if all(isinstance(p, Point) for p in parts):
        return [parts[0]]
    if all(isinstance(p, LineString) for p in parts):
        longest = max(parts, key=lambda p: p.length)
        return [longest.interpolate(0.5, normalized=True)]
    if walkable is None or walkable.is_empty:
        return [geometry.centroid]
    candidates = sorted(
        (
            (piece.length, piece.interpolate(0.5, normalized=True))
            for piece in shared_segments(geometry, walkable, tol)
        ),
        key=lambda c: (-round(c[0], 6), round(c[1].x, 6), round(c[1].y, 6)),
    )
    if candidates:
        kept = [mid for length, mid in candidates if length + _EPS >= min_length]
        return kept or [candidates[0][1]]
    return [nearest_points(geometry.boundary, walkable)[0]]


def access_point(geometry: BaseGeometry, walkable: BaseGeometry | None, tol: float = SHARE_TOL_M) -> Point:
    """Primary access point: midpoint of the longest boundary piece shared with ``walkable``
    (fallbacks as in ``access_points``)."""
    return access_points(geometry, walkable, tol, min_length=0.0)[0]


# --- walkway grid graph -----------------------------------------------------------------------------


def walkable_area(model: SpatialModel, floor: str) -> BaseGeometry | None:
    """Union of the floor's walkway polygons, or None when the floor has no walkway."""
    geoms = [w.geometry for w in model.by_kind(EntityKind.WALKWAY, floor) if w.geometry.area > 0]
    if not geoms:
        return None
    union = unary_union(geoms)
    return None if union.is_empty else union


def _grid(walkable: BaseGeometry, cell_size: float) -> tuple[nx.Graph, list[tuple[int, int]], np.ndarray]:
    if cell_size <= 0:
        raise ValueError("cell_size must be positive")
    minx, miny, maxx, maxy = walkable.bounds
    i0, i1 = math.floor(minx / cell_size), math.ceil(maxx / cell_size)
    j0, j1 = math.floor(miny / cell_size), math.ceil(maxy / cell_size)
    xs = (np.arange(i0, i1) + 0.5) * cell_size
    ys = (np.arange(j0, j1) + 0.5) * cell_size
    region = walkable.buffer(_COVER_EPS)
    shapely.prepare(region)
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    mask = np.asarray(shapely.contains_xy(region, gx, gy), dtype=bool)
    idx_i, idx_j = np.nonzero(mask)
    nodes = [(int(i0 + a), int(j0 + b)) for a, b in zip(idx_i.tolist(), idx_j.tolist(), strict=True)]
    node_xy = np.column_stack([xs[idx_i], ys[idx_j]]) if nodes else np.empty((0, 2))
    graph = nx.Graph()
    graph.add_nodes_from(
        (node, {"pos": (float(x), float(y))}) for node, (x, y) in zip(nodes, node_xy.tolist(), strict=True)
    )
    ni, nj = mask.shape
    edges: list[tuple[tuple[int, int], tuple[int, int], float]] = []
    for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
        a_hi = ni - di
        b_lo, b_hi = (0, nj - dj) if dj >= 0 else (-dj, nj)
        if a_hi <= 0 or b_hi <= b_lo:
            continue
        both = mask[:a_hi, b_lo:b_hi] & mask[di : a_hi + di, b_lo + dj : b_hi + dj]
        sa, sb = np.nonzero(both)
        if not len(sa):
            continue
        sb = sb + b_lo
        ta, tb = sa + di, sb + dj
        coords = np.stack([np.column_stack([xs[sa], ys[sb]]), np.column_stack([xs[ta], ys[tb]])], axis=1)
        inside = np.asarray(shapely.covers(region, shapely.linestrings(coords)), dtype=bool)
        weight = cell_size * math.hypot(di, dj)
        for p, q, r, s in zip(
            sa[inside].tolist(), sb[inside].tolist(), ta[inside].tolist(), tb[inside].tolist(), strict=True
        ):
            edges.append(((i0 + p, j0 + q), (i0 + r, j0 + s), weight))
    graph.add_weighted_edges_from(edges)
    return graph, nodes, node_xy


def walkway_grid_graph(model: SpatialModel, floor: str, cell_size: float = DEFAULT_CELL_M) -> nx.Graph:
    """Grid graph of a floor's walkways.

    Nodes are ``(i, j)`` cell indices whose centers ``((i + 0.5) * cell, (j + 0.5) * cell)`` lie inside the
    walkways (``pos`` attribute in meters); 8-neighborhood edges carry their Euclidean length as ``weight``.
    The graph is empty when the floor has no walkway.
    """
    walkable = walkable_area(model, floor)
    if walkable is None:
        return nx.Graph()
    return _grid(walkable, cell_size)[0]


def multi_source_dijkstra(
    graph: nx.Graph, sources: Mapping[Hashable, Hashable], weight: str = "weight"
) -> tuple[dict[Hashable, float], dict[Hashable, Hashable]]:
    """Distance from the nearest source and that source's label for every reachable node.

    ``sources`` maps source node -> label. Ties are broken deterministically by the sorted source order.
    """
    dist: dict[Hashable, float] = {}
    label: dict[Hashable, Hashable] = {}
    tentative: dict[Hashable, float] = {}
    counter = itertools.count()
    heap: list[tuple[float, int, Hashable, Hashable]] = []
    for node, name in sorted(sources.items(), key=lambda item: (str(item[1]), str(item[0]))):
        if node in graph and node not in tentative:
            tentative[node] = 0.0
            heapq.heappush(heap, (0.0, next(counter), node, name))
    while heap:
        d, _, node, name = heapq.heappop(heap)
        if node in dist:
            continue
        dist[node] = d
        label[node] = name
        for neighbor, attrs in graph.adj[node].items():
            if neighbor in dist:
                continue
            candidate = d + float(attrs.get(weight, 1.0))
            if candidate < tentative.get(neighbor, math.inf) - 1e-12:
                tentative[neighbor] = candidate
                heapq.heappush(heap, (candidate, next(counter), neighbor, name))
    return dist, label


@dataclass
class FloorNetwork:
    """Walkway grid graph of one floor plus attached entity access nodes.

    Each access point becomes a dead-end node (``AP:{entity_id}``, extra points ``AP:{entity_id}#2``...)
    linked to its nearest grid node, so paths never pass through an entity. Units, openings and amenities
    have one access point; vertical links have one per shared boundary piece (every landing side).
    """

    floor: str
    cell_size: float
    walkable: BaseGeometry | None
    graph: nx.Graph
    access_points: dict[str, Point] = field(default_factory=dict)
    all_access_points: dict[str, list[Point]] = field(default_factory=dict)
    access_nodes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    node_ids: list[tuple[int, int]] = field(default_factory=list, repr=False)
    node_xy: np.ndarray = field(default_factory=lambda: np.empty((0, 2)), repr=False)

    def nearest_node(self, point: Point) -> tuple[tuple[int, int], float] | None:
        """Nearest grid node and its distance (ties: lowest ``(i, j)``)."""
        if not self.node_ids:
            return None
        d2 = (self.node_xy[:, 0] - point.x) ** 2 + (self.node_xy[:, 1] - point.y) ** 2
        k = int(np.argmin(d2))
        return self.node_ids[k], math.sqrt(float(d2[k]))

    def attach(self, entity_id: str, points: Point | Iterable[Point]) -> tuple[str, ...]:
        """Attach access point(s) of an entity; returns the created nodes (empty without a grid)."""
        points = [points] if isinstance(points, Point) else list(points)
        if not points:
            return ()
        self.access_points[entity_id] = points[0]
        self.all_access_points[entity_id] = points
        nodes: list[str] = []
        for k, point in enumerate(points):
            found = self.nearest_node(point)
            if found is None:
                continue
            node, distance = found
            key = f"AP:{entity_id}" if k == 0 else f"AP:{entity_id}#{k + 1}"
            self.graph.add_node(key, pos=(float(point.x), float(point.y)), entity_id=entity_id)
            self.graph.add_edge(key, node, weight=distance)
            nodes.append(key)
        if nodes:
            self.access_nodes[entity_id] = tuple(nodes)
        return tuple(nodes)

    def distances_from(self, source_ids: Iterable[str]) -> tuple[dict[str, float], dict[str, str]]:
        """Walking distance from the nearest of ``source_ids`` to every reachable attached entity
        (minimum over the entity's access nodes) and the ID of that nearest source."""
        sources = {node: s for s in sorted(set(source_ids)) for node in self.access_nodes.get(s, ())}
        dist, label = multi_source_dijkstra(self.graph, sources)
        distances: dict[str, float] = {}
        nearest: dict[str, str] = {}
        for entity_id, nodes in self.access_nodes.items():
            reached = [(dist[n], str(label[n])) for n in nodes if n in dist]
            if reached:
                distances[entity_id], nearest[entity_id] = min(reached)
        return distances, nearest

    def walk_distance(self, entity_a: str, entity_b: str) -> float:
        """Walking distance between two attached entities (NaN when not connected)."""
        return self.distances_from([entity_a])[0].get(entity_b, math.nan)


def build_floor_network(
    model: SpatialModel, floor: str, cell_size: float = DEFAULT_CELL_M, tol: float = SHARE_TOL_M
) -> FloorNetwork:
    """Grid graph of one floor with access points of units, vertical links, openings and amenities."""
    walkable = walkable_area(model, floor)
    if walkable is None:
        network = FloorNetwork(floor, cell_size, None, nx.Graph())
    else:
        graph, nodes, node_xy = _grid(walkable, cell_size)
        network = FloorNetwork(floor, cell_size, walkable, graph, node_ids=nodes, node_xy=node_xy)
    entities = sorted(
        (e for e in model.entities.values() if e.floor == floor and e.kind in ACCESS_KINDS),
        key=lambda e: e.entity_id,
    )
    for entity in entities:
        if entity.geometry.is_empty:
            continue
        if entity.kind == EntityKind.VERTICAL_LINK:
            network.attach(entity.entity_id, access_points(entity.geometry, walkable, tol))
        else:
            network.attach(entity.entity_id, access_point(entity.geometry, walkable, tol))
    return network


def build_networks(
    model: SpatialModel, cell_size: float = DEFAULT_CELL_M, tol: float = SHARE_TOL_M
) -> dict[str, FloorNetwork]:
    """Floor networks for every floor that has entities (reusable across feature and relation calls)."""
    floors = {e.floor for e in model.entities.values() if e.floor}
    return {
        code: build_floor_network(model, code, cell_size, tol)
        for code in floor_codes(model)
        if code in floors
    }


# --- unit features ----------------------------------------------------------------------------------


def _unit_code(entity: Entity) -> str:
    return str(entity.attributes.get("unit_code") or entity.entity_id)


def _is_vacant(attributes: Mapping[str, object]) -> bool:
    value = attributes.get("vacant")
    if value is None:
        return not attributes.get("brand_id")
    return bool(value)


def _nearest_targets(
    network: FloorNetwork, units: list[Entity], target_ids: list[str]
) -> dict[str, tuple[float, str, bool]]:
    """Per unit: (walking distance, nearest target ID, straight-line fallback used)."""
    if not target_ids:
        return {}
    distances, nearest = network.distances_from(target_ids)
    result: dict[str, tuple[float, str, bool]] = {}
    for unit in units:
        uid = unit.entity_id
        if uid in distances:
            result[uid] = (distances[uid], nearest[uid], False)
            continue
        origin = network.access_points.get(uid, unit.geometry.centroid)
        options = [
            (float(origin.distance(point)), t)
            for t in sorted(target_ids)
            for point in network.all_access_points.get(t, ())
        ]
        if options:
            distance, target = min(options)
            result[uid] = (distance, target, True)
    return result


def unit_features(
    model: SpatialModel,
    cell_size: float = DEFAULT_CELL_M,
    networks: Mapping[str, FloorNetwork] | None = None,
    tol: float = SHARE_TOL_M,
) -> pd.DataFrame:
    """One row per unit (floors bottom-up, then unit code) with area, frontage, walks and centroid.

    ``frontage_m`` is the unit boundary shared with the floor's walkways (tolerance ``tol``).
    ``walk_to_escalator_m`` is the walking distance to the nearest escalator access point on the same
    floor; when no escalator is reachable through the walkways the straight-line distance is used and
    ``escalator_fallback`` is True (NaN when the floor has no escalator). ``walk_to_entrance_m`` works the
    same with openings and is NaN on floors without openings.
    """
    networks = dict(networks) if networks is not None else build_networks(model, cell_size, tol)
    rows: list[dict[str, object]] = []
    for floor in floor_codes(model):
        units = sorted(model.units(floor), key=lambda u: (_unit_code(u), u.entity_id))
        if not units:
            continue
        network = networks.get(floor) or build_floor_network(model, floor, cell_size, tol)
        escalators = [
            e.entity_id for e in model.by_kind(EntityKind.VERTICAL_LINK, floor) if link_type(e) == "escalator"
        ]
        openings = [e.entity_id for e in model.by_kind(EntityKind.OPENING, floor)]
        to_escalator = _nearest_targets(network, units, escalators)
        to_entrance = _nearest_targets(network, units, openings)
        for unit in units:
            attrs = unit.attributes
            centroid = unit.geometry.centroid
            access = network.access_points.get(unit.entity_id)
            esc = to_escalator.get(unit.entity_id)
            ent = to_entrance.get(unit.entity_id)
            rows.append(
                {
                    "entity_id": unit.entity_id,
                    "unit_code": _unit_code(unit),
                    "floor": floor,
                    "brand_id": attrs.get("brand_id"),
                    "brand_name": attrs.get("brand_name"),
                    "category": attrs.get("category"),
                    "zone_code": attrs.get("zone_code"),
                    "vacant": _is_vacant(attrs),
                    "area_m2": round(float(unit.geometry.area), 3),
                    "frontage_m": round(shared_length(unit.geometry, network.walkable, tol), 3),
                    "walk_to_escalator_m": round(esc[0], 3) if esc else math.nan,
                    "nearest_escalator_id": esc[1] if esc else None,
                    "escalator_fallback": bool(esc[2]) if esc else False,
                    "walk_to_entrance_m": round(ent[0], 3) if ent else math.nan,
                    "nearest_entrance_id": ent[1] if ent else None,
                    "entrance_fallback": bool(ent[2]) if ent else False,
                    "centroid_x": round(float(centroid.x), 3),
                    "centroid_y": round(float(centroid.y), 3),
                    "access_x": round(float(access.x), 3) if access is not None else math.nan,
                    "access_y": round(float(access.y), 3) if access is not None else math.nan,
                }
            )
    return pd.DataFrame(rows, columns=UNIT_FEATURE_COLUMNS)


def adjacency_pairs(
    model: SpatialModel, min_shared_m: float = MIN_SHARED_M, tol: float = SHARE_TOL_M
) -> pd.DataFrame:
    """Unit pairs on the same floor whose boundaries touch or lie within ``tol`` along ``min_shared_m``+.

    ``unit_a``/``unit_b`` are unit codes with ``unit_a < unit_b``; entity IDs are included for joins.
    """
    rows: list[dict[str, object]] = []
    for floor in floor_codes(model):
        units = sorted(model.units(floor), key=lambda u: (_unit_code(u), u.entity_id))
        if len(units) < 2:
            continue
        tree = STRtree([u.geometry for u in units])
        floor_rows: list[dict[str, object]] = []
        for i, unit in enumerate(units):
            for j in sorted(int(k) for k in tree.query(unit.geometry.buffer(tol))):
                if j <= i:
                    continue
                other = units[j]
                if unit.geometry.distance(other.geometry) > tol + _EPS:
                    continue
                length = shared_length(unit.geometry, other.geometry, tol)
                if length + _EPS < min_shared_m:
                    continue
                (code_a, id_a), (code_b, id_b) = sorted(
                    [(_unit_code(unit), unit.entity_id), (_unit_code(other), other.entity_id)]
                )
                floor_rows.append(
                    {
                        "unit_a": code_a,
                        "unit_b": code_b,
                        "floor": floor,
                        "shared_length_m": round(length, 3),
                        "entity_a": id_a,
                        "entity_b": id_b,
                    }
                )
        rows.extend(sorted(floor_rows, key=lambda r: (str(r["unit_a"]), str(r["unit_b"]))))
    return pd.DataFrame(rows, columns=ADJACENCY_COLUMNS)


# --- relations --------------------------------------------------------------------------------------


def _vertical_m(lower: Floor | None, upper: Floor | None) -> float:
    if lower is not None and upper is not None and upper.elevation_m > lower.elevation_m:
        return float(upper.elevation_m - lower.elevation_m)
    if lower is not None and lower.height_m > 0:
        return float(lower.height_m)
    return float(FLOOR_HEIGHT_M)


def compute_relations(
    model: SpatialModel,
    cell_size: float = DEFAULT_CELL_M,
    min_shared_m: float = MIN_SHARED_M,
    tol: float = SHARE_TOL_M,
    networks: Mapping[str, FloorNetwork] | None = None,
) -> list[Relation]:
    """Derived relations: ON_FLOOR, ADJACENT_TO, FACES, NEAR and CONNECTED_TO.

    - ON_FLOOR: every entity with a floor -> floor code (same convention as the mock generator).
    - ADJACENT_TO: unit -> unit (lower unit code first), ``shared_length_m``.
    - FACES: unit -> walkway sharing at least ``min_shared_m`` of boundary, ``frontage_m``.
    - NEAR: unit -> nearest escalator and unit -> nearest opening on the floor, ``distance_m`` (walking)
      and ``euclidean_fallback`` (1.0 when the walkway graph did not connect them).
    - CONNECTED_TO: vertical link -> same ``link_id`` on the next floor up, ``floor_gap`` and ``vertical_m``.

    CONTAINS relations are not produced; callers merge these with the model's existing relations.
    """
    networks = dict(networks) if networks is not None else build_networks(model, cell_size, tol)
    relations: list[Relation] = [
        Relation(e.entity_id, e.floor, RelationType.ON_FLOOR)
        for e in sorted(model.entities.values(), key=lambda e: e.entity_id)
        if e.floor and e.kind != EntityKind.FLOOR
    ]
    for row in adjacency_pairs(model, min_shared_m, tol).itertuples(index=False):
        relations.append(
            Relation(
                str(row.entity_a),
                str(row.entity_b),
                RelationType.ADJACENT_TO,
                {"shared_length_m": float(row.shared_length_m)},
            )
        )
    for floor in floor_codes(model):
        walkways = sorted(model.by_kind(EntityKind.WALKWAY, floor), key=lambda e: e.entity_id)
        if not walkways:
            continue
        tree = STRtree([w.geometry for w in walkways])
        for unit in sorted(model.units(floor), key=lambda e: e.entity_id):
            for k in sorted(int(i) for i in tree.query(unit.geometry.buffer(tol))):
                frontage = shared_length(unit.geometry, walkways[k].geometry, tol)
                if frontage + _EPS >= min_shared_m:
                    relations.append(
                        Relation(
                            unit.entity_id,
                            walkways[k].entity_id,
                            RelationType.FACES,
                            {"frontage_m": round(frontage, 3)},
                        )
                    )
    features = unit_features(model, cell_size, networks, tol)
    for row in features.itertuples(index=False):
        for target, distance, fallback in (
            (row.nearest_escalator_id, row.walk_to_escalator_m, row.escalator_fallback),
            (row.nearest_entrance_id, row.walk_to_entrance_m, row.entrance_fallback),
        ):
            if isinstance(target, str) and target:
                relations.append(
                    Relation(
                        str(row.entity_id),
                        target,
                        RelationType.NEAR,
                        {"distance_m": float(distance), "euclidean_fallback": 1.0 if fallback else 0.0},
                    )
                )
    order = floor_codes(model)
    position = {code: i for i, code in enumerate(order)}
    floors_by_code = {f.code: f for f in model.floors}
    links: dict[str, dict[str, Entity]] = defaultdict(dict)
    for link in sorted(model.by_kind(EntityKind.VERTICAL_LINK), key=lambda e: e.entity_id):
        link_id = link.attributes.get("link_id")
        if link_id and link.floor and link.floor not in links[str(link_id)]:
            links[str(link_id)][link.floor] = link
    for link_id in sorted(links):
        stack = sorted(links[link_id], key=lambda code: position[code])
        for lower, upper in itertools.pairwise(stack):
            if position[upper] - position[lower] != 1:
                continue
            relations.append(
                Relation(
                    links[link_id][lower].entity_id,
                    links[link_id][upper].entity_id,
                    RelationType.CONNECTED_TO,
                    {
                        "floor_gap": 1.0,
                        "vertical_m": _vertical_m(floors_by_code.get(lower), floors_by_code.get(upper)),
                    },
                )
            )
    return relations
