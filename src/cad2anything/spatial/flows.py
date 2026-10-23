"""Estimated foot traffic along the corridors, corridor centerlines and clear widths.

Purchases tell where and when a customer paid, not how they walked. A flow map connects consecutive purchases
of one customer on one day by the shortest walk along the corridors and adds the walks up:

- Corridor centerlines: the floor's 1 m walkway grid (``FloorNetwork``) is thinned (Zhang-Suen) to one-cell-wide
  lines along the middle of every corridor, around escalator wells and into open areas. Pieces that thinning
  separates are joined again along the grid. Chains of cells between junctions become edges; their polylines
  are smoothed (one-cell staircases of diagonal stretches removed) and measured along the smoothed line.
- Access points hang off the nearest centerline cell in clear sight: unit entrances, escalator landings (the
  ends of the long axis), stairs, elevators and building entrances. A walk leaves a store, follows the middle
  of the corridors and enters the next store.
- A floor change walks to a landing of an escalator on the origin floor and from a landing of the same
  escalator on the destination floor; the escalator minimizes the walk on both floors (as ``RoutePlanner``).
- One shortest-path tree per unit entrance is kept per revision. Trips are added to the tree of their origin
  and subtree sums give the trips on every edge, so each distinct pair of units is routed once.
- Clear width at a point: the distance to the nearest corridor boundary plus the free distance straight on in
  the opposite direction. Across a straight corridor this is the wall-to-wall width; junctions measure wider.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from cad2anything.model import Entity, EntityKind, SpatialModel
from cad2anything.spatial.features import (
    FloorNetwork,
    boundary_segments,
    build_floor_network,
    floor_codes,
    link_type,
)
from cad2anything.spatial.vocabulary import floor_bounds, side_of

SIMPLIFY_M = 0.75  # removes one-cell staircases (up to 0.71 m off the line) from thinned diagonals
SIGHT_TOL_M = 0.3
ATTACH_CANDIDATES = 12
CENTER_SHIFT_M = 0.75
WIDTH_CHUNK = 256
MIN_CLEARANCE_M = 0.05
LANDMARK_FRONT_M = 6.0
LANDMARK_SIDE_M = 5.0
BETWEEN_M = 6.0
UNIT_LABEL_M = 8.0
MAX_SEGMENTS = 400
LINK_NAMES = {"escalator": "에스컬레이터", "elevator": "엘리베이터", "stair": "계단"}
LABEL_PRIORITY = {"escalator": 0, "entrance": 0, "stair": 1, "elevator": 1}
TRIP_COLUMNS = ["customer_key", "day", "unit_a", "unit_b", "weight"]
_STEPS = ((1, 0), (0, 1), (1, 1), (1, -1))
_EPS = 1e-9

Cell = tuple[int, int]


# --- grid geometry ----------------------------------------------------------------------------------


def thin(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning of a boolean grid (axes x, y) to one-cell-wide lines; endpoints are kept."""
    image = np.pad(np.asarray(mask, dtype=np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            center = image[1:-1, 1:-1]
            ring = [
                image[1:-1, 2:],  # north
                image[2:, 2:],
                image[2:, 1:-1],  # east
                image[2:, :-2],
                image[1:-1, :-2],  # south
                image[:-2, :-2],
                image[:-2, 1:-1],  # west
                image[:-2, 2:],
            ]
            north, east, south, west = ring[0], ring[2], ring[4], ring[6]
            count = np.sum(ring, axis=0)
            transitions = sum((ring[k] == 0) & (ring[(k + 1) % 8] == 1) for k in range(8))
            if step == 0:
                side = (north & east & south) | (east & south & west)
            else:
                side = (north & east & west) | (north & south & west)
            delete = (center == 1) & (count >= 2) & (count <= 6) & (transitions == 1) & (side == 0)
            if delete.any():
                center[delete] = 0
                changed = True
        if not changed:
            return image[1:-1, 1:-1].astype(bool)


def clear_widths(region: BaseGeometry | None, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Clear width (m) at points inside ``region`` and the offset that moves each point to the middle.

    The width is the distance to the nearest boundary point plus the free distance from the point straight
    away from it (ray cast against the boundary segments). Points closer than ``MIN_CLEARANCE_M`` to the
    boundary get NaN and no offset.
    """
    xy = np.asarray(xy, dtype=float).reshape(-1, 2)
    width = np.full(len(xy), np.nan)
    shift = np.zeros((len(xy), 2))
    segments = boundary_segments(region) if region is not None else np.empty((0, 4))
    if not len(xy) or not len(segments):
        return width, shift
    start, delta = segments[:, :2], segments[:, 2:] - segments[:, :2]
    length2 = np.maximum((delta**2).sum(axis=1), _EPS)
    for lo in range(0, len(xy), WIDTH_CHUNK):
        p = xy[lo : lo + WIDTH_CHUNK]
        rel = start[None, :, :] - p[:, None, :]  # boundary segment starts seen from each point
        t = np.clip(-(rel * delta[None]).sum(axis=2) / length2[None], 0.0, 1.0)
        closest = start[None] + t[..., None] * delta[None]
        nearest = closest[np.arange(len(p)), ((closest - p[:, None, :]) ** 2).sum(axis=2).argmin(axis=1)]
        away = p - nearest
        clearance = np.hypot(away[:, 0], away[:, 1])
        ok = clearance >= MIN_CLEARANCE_M
        if not ok.any():
            continue
        u = np.zeros_like(away)
        u[ok] = away[ok] / clearance[ok, None]
        denom = u[:, None, 0] * delta[None, :, 1] - u[:, None, 1] * delta[None, :, 0]
        with np.errstate(divide="ignore", invalid="ignore"):
            along = (rel[..., 0] * delta[None, :, 1] - rel[..., 1] * delta[None, :, 0]) / denom
            across = (rel[..., 0] * u[:, None, 1] - rel[..., 1] * u[:, None, 0]) / denom
        hit = (np.abs(denom) > 1e-12) & (along > 1e-6) & (across >= -1e-9) & (across <= 1.0 + 1e-9)
        free = np.where(hit, along, np.inf).min(axis=1)
        free = np.where(np.isfinite(free), free, 0.0)
        index = np.arange(lo, lo + len(p))[ok]
        width[index] = clearance[ok] + free[ok]
        shift[index] = u[ok] * ((free[ok] - clearance[ok]) / 2.0)[:, None]
    return width, shift


def _unit_code(entity: Entity) -> str:
    return str(entity.attributes.get("unit_code") or entity.entity_id)


def link_name(kind: str | None, ref: str) -> str:
    """Korean name of a vertical link: ``ESC-A`` -> "에스컬레이터 A", ``ST-1`` -> "계단 1"."""
    base = LINK_NAMES.get(kind or "")
    if not base:
        return ref
    suffix = ref.split("-", 1)[1] if "-" in ref else ref
    return f"{base} {suffix}"


def _direction(dx: float, dy: float) -> tuple[float, float] | None:
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm > _EPS else None


def _escalator_landings(
    entity: Entity, points: list[Point]
) -> list[tuple[Point, tuple[float, float] | None]]:
    """Access points at the ends of an escalator's long axis with the direction a rider steps off."""
    centroid = entity.geometry.centroid
    axis = None
    rect = entity.geometry.minimum_rotated_rectangle
    if rect.geom_type == "Polygon":
        coords = list(rect.exterior.coords)[:4]
        (ax, ay), (bx, by) = max(
            ((coords[k], coords[(k + 1) % 4]) for k in range(4)), key=lambda e: math.dist(e[0], e[1])
        )
        axis = _direction(bx - ax, by - ay)
    found = []
    for point in points:
        facing = _direction(point.x - centroid.x, point.y - centroid.y)
        if axis is None or facing is None or abs(facing[0] * axis[0] + facing[1] * axis[1]) >= math.sqrt(0.5):
            found.append((point, facing))
    return found or [(point, None) for point in points]


def _into_building(entity: Entity, point: Point, walkable: BaseGeometry | None) -> tuple[float, float] | None:
    """Normal of a door line pointing into the walkways."""
    coords = list(entity.geometry.coords) if entity.geometry.geom_type == "LineString" else []
    if len(coords) < 2 or walkable is None:
        return None
    (x0, y0), (x1, y1) = coords[0][:2], coords[-1][:2]
    normal = _direction(y1 - y0, -(x1 - x0))
    if normal is None:
        return None
    ahead = Point(point.x + normal[0], point.y + normal[1])
    behind = Point(point.x - normal[0], point.y - normal[1])
    return normal if walkable.distance(ahead) <= walkable.distance(behind) else (-normal[0], -normal[1])


def _landing_eyes(spot: Point, facing: tuple[float, float] | None) -> list[tuple[float, float]]:
    """Where riders stand after stepping off a landing: 1 m and 2.5 m ahead, and 1.5 m to each side."""
    if facing is None:
        return [(spot.x, spot.y)]
    fx, fy = facing
    return [
        (spot.x + fx * ahead - fy * aside, spot.y + fy * ahead + fx * aside)
        for ahead, aside in ((1.0, 0.0), (2.5, 0.0), (1.0, -1.5), (1.0, 1.5))
    ]


@dataclass
class Landmark:
    """A vertical link or building entrance with the points where people step on or off."""

    entity_id: str
    kind: str  # escalator, elevator, stair, entrance
    ref: str  # ESC-A, ST-1, ENT-1
    name: str  # 에스컬레이터 A, 계단 1, 정문(ENT-1)
    geometry: BaseGeometry
    points: list[Point]
    facing: list[tuple[float, float] | None]


def floor_landmarks(model: SpatialModel, floor: str, network: FloorNetwork) -> list[Landmark]:
    """Vertical links and building entrances of a floor (escalators keep their landings only)."""
    found: list[Landmark] = []
    for entity in sorted(model.entities.values(), key=lambda e: e.entity_id):
        if entity.floor != floor or entity.geometry.is_empty:
            continue
        attrs = entity.attributes
        points = list(network.all_access_points.get(entity.entity_id) or [])
        if entity.kind == EntityKind.VERTICAL_LINK:
            kind = link_type(entity) or "vertical_link"
            ref = str(attrs.get("link_id") or entity.entity_id)
            if kind == "escalator":
                pairs = _escalator_landings(entity, points)
            else:
                pairs = [(point, None) for point in points]
            name = link_name(kind, ref)
        elif entity.kind == EntityKind.OPENING:
            kind = "entrance"
            ref = str(attrs.get("opening_id") or entity.entity_id)
            name = f"{attrs['name_ko']}({ref})" if attrs.get("name_ko") else ref
            pairs = [(point, _into_building(entity, point, network.walkable)) for point in points]
        else:
            continue
        found.append(
            Landmark(
                entity.entity_id,
                kind,
                ref,
                name,
                entity.geometry,
                [p for p, _ in pairs],
                [f for _, f in pairs],
            )
        )
    return found


# --- corridor centerline graph ----------------------------------------------------------------------


def simplify_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) > 2:
        points = list(LineString(points).simplify(SIMPLIFY_M, preserve_topology=False).coords)
    return [(round(float(x), 2), round(float(y), 2)) for x, y in points]


@dataclass
class CorridorEdge:
    """A chain of centerline cells between two graph nodes, or a stub from an access point to its cell."""

    u: int
    v: int
    length: float
    points: list[tuple[float, float]]
    cells: list[int] = field(default_factory=list)  # centerline cells strictly inside the chain
    stub: bool = False


def _centerline_links(graph: nx.Graph, kept: set[Cell]) -> nx.Graph:
    """Graph of thinned cells linked by grid edges; diagonal links beside an L of straight links are dropped."""
    skeleton = nx.Graph()
    skeleton.add_nodes_from(kept)
    adjacency = graph.adj
    for i, j in kept:
        for di, dj in _STEPS:
            other = (i + di, j + dj)
            if other in kept and other in adjacency[(i, j)]:
                skeleton.add_edge((i, j), other, weight=float(adjacency[(i, j)][other]["weight"]))
    for a, b in sorted(skeleton.edges()):
        if a[0] != b[0] and a[1] != b[1]:
            for corner in ((a[0], b[1]), (b[0], a[1])):
                if skeleton.has_edge(a, corner) and skeleton.has_edge(corner, b):
                    skeleton.remove_edge(a, b)
                    break
    return skeleton


def _grid_components(graph: nx.Graph, cells: list[Cell]) -> list[set[Cell]]:
    """Connected pieces of the walkway grid (access point nodes are skipped)."""
    seen: set[Cell] = set()
    components: list[set[Cell]] = []
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        component, stack = {start}, [start]
        while stack:
            for other in graph.adj[stack.pop()]:
                if isinstance(other, tuple) and other not in seen:
                    seen.add(other)
                    component.add(other)
                    stack.append(other)
        components.append(component)
    return components


def _join_pieces(graph: nx.Graph, cells: list[Cell], skeleton: nx.Graph) -> None:
    """Give every connected piece of walkway one connected centerline (thinning may split thin parts)."""
    for component in _grid_components(graph, cells):
        inside = [cell for cell in component if cell in skeleton]
        if not inside:
            center = np.asarray(sorted(component), dtype=float).mean(axis=0)
            skeleton.add_node(
                min(component, key=lambda c: ((c[0] - center[0]) ** 2 + (c[1] - center[1]) ** 2, c))
            )
            continue
        pieces = sorted((set(p) for p in nx.connected_components(skeleton.subgraph(inside))), key=min)
        joined, rest = pieces[0], pieces[1:]
        while rest:
            dist, paths = nx.multi_source_dijkstra(graph, joined, weight="weight")
            owner = {cell: k for k, piece in enumerate(rest) for cell in piece}
            reached = [(dist[cell], cell) for cell in owner if cell in dist]
            if not reached:
                break
            target = min(reached)[1]
            path = paths[target]
            for a, b in zip(path, path[1:], strict=False):
                skeleton.add_edge(a, b, weight=float(graph[a][b]["weight"]))
            joined = joined | set(path) | rest.pop(owner[target])


def _attach(cell_xy: np.ndarray, region: BaseGeometry, points: list[Point]) -> list[int]:
    """Nearest centerline cell in clear sight of each point (the nearest cell when none is in sight)."""
    if not points or not len(cell_xy):
        return []
    xy = np.array([(p.x, p.y) for p in points], dtype=float)
    distance = np.hypot(xy[:, None, 0] - cell_xy[None, :, 0], xy[:, None, 1] - cell_xy[None, :, 1])
    k = min(ATTACH_CANDIDATES, len(cell_xy))
    order = np.argsort(distance, axis=1, kind="stable")[:, :k]
    lines = shapely.linestrings(np.stack([np.repeat(xy, k, axis=0), cell_xy[order.ravel()]], axis=1))
    visible = np.asarray(shapely.covers(region, lines), dtype=bool).reshape(-1, k)
    first = np.where(visible.any(axis=1), visible.argmax(axis=1), 0)
    return order[np.arange(len(xy)), first].tolist()


def _chains(skeleton: nx.Graph, node_index: dict[Cell, int]) -> list[list[Cell]]:
    """Cell chains between key cells; every centerline link is used once."""
    used: set[frozenset[Cell]] = set()
    chains: list[list[Cell]] = []
    for start in sorted(node_index):
        for first in sorted(skeleton.adj[start]):
            if frozenset((start, first)) in used:
                continue
            used.add(frozenset((start, first)))
            chain = [start, first]
            previous, current = start, first
            while current not in node_index:
                following = next(c for c in sorted(skeleton.adj[current]) if c != previous)
                used.add(frozenset((current, following)))
                chain.append(following)
                previous, current = current, following
            chains.append(chain)
    return chains


@dataclass
class CorridorGraph:
    """Corridor centerline graph of one floor with access points hanging off it.

    Nodes ``0 .. n_centerline-1`` are centerline junctions, ends and attachment cells; the following nodes are
    access points (leaves). ``access`` maps an entity ID to its access nodes (a unit has its entrance only,
    an escalator its landings). Centerline cells keep their clear width and the edge or node they belong to.
    """

    floor: str
    walkable: BaseGeometry
    cells: list[Cell]
    cell_xy: np.ndarray
    cell_draw: np.ndarray  # cell centers moved to the middle of straight corridors (for drawing)
    cell_width: np.ndarray
    cell_neighbors: list[list[int]]
    cell_edge: np.ndarray
    cell_node: np.ndarray
    node_xy: np.ndarray
    node_entity: list[str | None]
    n_centerline: int
    edges: list[CorridorEdge]
    adjacency: list[list[tuple[int, int, float]]]
    access: dict[str, list[int]]
    landings: dict[str, list[int]]
    landmarks: list[Landmark]
    units: list[Entity]
    walkways: list[Entity]
    grid_xy: np.ndarray
    bounds: tuple[float, float, float, float] | None = None
    cell_size: float = 1.0
    _labels: dict[tuple[float, float], str] = field(default_factory=dict, repr=False)
    _unit_tree: STRtree | None = field(default=None, repr=False)
    _sight_region: BaseGeometry | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.edge_u = np.array([e.u for e in self.edges], dtype=int)
        self.edge_v = np.array([e.v for e in self.edges], dtype=int)

    # Flows -------------------------------------------------------------------------------------------
    def node_flow(self, edge_flow: np.ndarray) -> np.ndarray:
        """Trips through each node: half the flow on its edges (a walk enters and leaves every node)."""
        through = np.zeros(len(self.node_xy))
        np.add.at(through, self.edge_u, edge_flow)
        np.add.at(through, self.edge_v, edge_flow)
        return through / 2.0

    def cell_flow(self, edge_flow: np.ndarray) -> np.ndarray:
        """Trips through each centerline cell."""
        through = self.node_flow(edge_flow)
        flow = np.zeros(len(self.cells))
        inner = self.cell_edge >= 0
        flow[inner] = edge_flow[self.cell_edge[inner]]
        nodes = self.cell_node >= 0
        flow[nodes] = through[self.cell_node[nodes]]
        return flow

    def pass_by(self, edge_flow: np.ndarray) -> dict[str, float]:
        """Trips passing the entrance of each unit (by unit code), without trips to or from the unit itself."""
        through = self.node_flow(edge_flow)
        result: dict[str, float] = {}
        for unit in self.units:
            nodes = self.access.get(unit.entity_id)
            if not nodes or not self.adjacency[nodes[0]]:
                continue
            target, stub, _ = self.adjacency[nodes[0]][0]
            result[_unit_code(unit)] = max(float(through[target] - edge_flow[stub]), 0.0)
        return result

    def _join(self, edge_flow: np.ndarray, same_flow: bool) -> list[list[int]]:
        """Corridor edges with flow grouped where exactly two of them meet (with equal flow if asked)."""
        flowing = [
            e
            for e, edge in enumerate(self.edges)
            if not edge.stub and edge.u != edge.v and edge_flow[e] > _EPS
        ]
        at_node: dict[int, list[int]] = defaultdict(list)
        for e in flowing:
            at_node[self.edges[e].u].append(e)
            at_node[self.edges[e].v].append(e)
        parent = {e: e for e in flowing}

        def find(e: int) -> int:
            while parent[e] != e:
                parent[e] = parent[parent[e]]
                e = parent[e]
            return e

        for incident in at_node.values():
            if len(incident) != 2 or incident[0] == incident[1]:
                continue
            a, b = incident
            if not same_flow or abs(edge_flow[a] - edge_flow[b]) <= 1e-6 * max(edge_flow[a], edge_flow[b]):
                parent[find(a)] = find(b)
        groups: dict[int, list[int]] = defaultdict(list)
        for e in flowing:
            groups[find(e)].append(e)
        return sorted(groups.values(), key=min)

    def polyline(self, group: list[int]) -> list[tuple[float, float]]:
        """Points along a path (or loop) of edges."""
        if len(group) == 1:
            return list(self.edges[group[0]].points)
        incident: dict[int, list[int]] = defaultdict(list)
        for e in sorted(group):
            incident[self.edges[e].u].append(e)
            incident[self.edges[e].v].append(e)
        ends = sorted(n for n, edges in incident.items() if len(edges) == 1)
        node = ends[0] if ends else self.edges[min(group)].u
        remaining = set(group)
        points: list[tuple[float, float]] = []
        while remaining:
            e = next((x for x in incident[node] if x in remaining), None)
            if e is None:
                break
            remaining.discard(e)
            edge = self.edges[e]
            coords = edge.points if edge.u == node else list(reversed(edge.points))
            points.extend(coords if not points else coords[1:])
            node = edge.v if edge.u == node else edge.u
        return simplify_points(points)

    def segments(
        self, edge_flow: np.ndarray, max_segments: int = MAX_SEGMENTS
    ) -> list[tuple[list[tuple[float, float]], float]]:
        """Polylines of corridor stretches with flow (edges joined where the flow does not change)."""
        found = [(self.polyline(group), float(edge_flow[group[0]])) for group in self._join(edge_flow, True)]
        found.sort(key=lambda item: (-item[1], item[0][0]))
        return found[:max_segments]

    def runs(self, edge_flow: np.ndarray) -> list[tuple[list[int], float, int]]:
        """Corridor stretches between junctions of flowing corridors: (edges, peak flow, peak edge)."""
        found = []
        for group in self._join(edge_flow, False):
            peak = max(group, key=lambda e: (edge_flow[e], -e))
            found.append((group, float(edge_flow[peak]), peak))
        return sorted(found, key=lambda item: (-item[1], item[2]))

    # Labels ------------------------------------------------------------------------------------------
    def label_at(self, x: float, y: float) -> str:
        """Korean description of a corridor point by the nearest landmark or units."""
        key = (round(float(x), 1), round(float(y), 1))
        if key not in self._labels:
            self._labels[key] = self._label(float(x), float(y))
        return self._labels[key]

    def edge_label(self, points: list[tuple[float, float]]) -> str:
        middle = LineString(points).interpolate(0.5, normalized=True) if len(points) > 1 else Point(points[0])
        return self.label_at(middle.x, middle.y)

    def _label(self, x: float, y: float) -> str:
        point = Point(x, y)
        best: tuple[int, float, str] | None = None
        beside: list[tuple[float, str, str]] = []
        for mark in self.landmarks:
            for spot, facing in zip(mark.points, mark.facing, strict=True):
                dx, dy = x - spot.x, y - spot.y
                distance = math.hypot(dx, dy)
                ahead = facing is None or dx * facing[0] + dy * facing[1] >= -0.5
                if distance <= LANDMARK_FRONT_M and ahead:
                    option = (LABEL_PRIORITY.get(mark.kind, 1), distance, f"{self.floor} {mark.name} 앞")
                    best = option if best is None else min(best, option)
            if mark.kind == "escalator":
                distance = float(mark.geometry.distance(point))
                if distance <= BETWEEN_M:
                    beside.append((distance, mark.name, mark.ref.split("-", 1)[-1]))
        if best is not None:
            return best[2]
        if len(beside) >= 2:
            names = "·".join(sorted({suffix for _, _, suffix in beside}))
            return f"{self.floor} {LINK_NAMES['escalator']} {names} 사이 통로"
        if beside and min(beside)[0] <= LANDMARK_SIDE_M:
            return f"{self.floor} {min(beside)[1]} 옆 통로"
        if self.units:
            if self._unit_tree is None:
                self._unit_tree = STRtree([u.geometry for u in self.units])
            near = self._unit_tree.query(point.buffer(UNIT_LABEL_M))
            ranked = sorted(
                (float(self.units[k].geometry.distance(point)), _unit_code(self.units[k]))
                for k in near.tolist()
            )
            codes = sorted(code for distance, code in ranked[:2] if distance <= UNIT_LABEL_M)
            if codes:
                return f"{'·'.join(codes)} 앞 통로"
        for walkway in self.walkways:
            name = walkway.attributes.get("name_ko")
            if name and walkway.geometry.covers(point):
                return f"{self.floor} {name}"
        sector = side_of(x, y, self.bounds) if self.bounds else ""
        return f"{self.floor} {sector} 통로" if sector else f"{self.floor} 통로"

    def _sight(self) -> BaseGeometry:
        if self._sight_region is None:
            region = self.walkable.buffer(SIGHT_TOL_M)
            shapely.prepare(region)
            self._sight_region = region
        return self._sight_region

    def nearby_max(self, xy: np.ndarray, values: np.ndarray, radius: float) -> np.ndarray:
        """Largest value of the centerline cells within ``radius`` in clear sight of each point (0 if none)."""
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        result = np.zeros(len(xy))
        if not len(xy) or not len(self.cell_xy):
            return result
        distance = np.hypot(
            xy[:, None, 0] - self.cell_xy[None, :, 0], xy[:, None, 1] - self.cell_xy[None, :, 1]
        )
        pi, ci = np.nonzero(distance <= radius)
        if not len(pi):
            return result
        lines = shapely.linestrings(np.stack([xy[pi], self.cell_xy[ci]], axis=1))
        visible = np.asarray(shapely.covers(self._sight(), lines), dtype=bool) | (distance[pi, ci] < 1e-6)
        np.maximum.at(result, pi[visible], values[ci[visible]])
        return result

    def landing_view(self, xy: np.ndarray, max_m: float) -> list[tuple[float, str] | None]:
        """Nearest escalator landing whose landing area sees each point: (distance from the landing, name).

        Sight lines start where riders stand after stepping off (a few points in front of the landing), so
        the escalator body itself does not hide things beside it."""
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        found: list[tuple[float, str] | None] = [None] * len(xy)
        region = self._sight()
        for mark in self.landmarks:
            if mark.kind != "escalator":
                continue
            for spot, facing in zip(mark.points, mark.facing, strict=True):
                distance = np.hypot(xy[:, 0] - spot.x, xy[:, 1] - spot.y)
                near = np.flatnonzero(distance <= max_m)
                seen = np.zeros(len(near), dtype=bool)
                for eye in _landing_eyes(spot, facing):
                    todo = np.flatnonzero(~seen)
                    if not len(todo):
                        break
                    targets = xy[near[todo]]
                    lines = shapely.linestrings(
                        np.stack([targets, np.repeat([eye], len(todo), axis=0)], axis=1)
                    )
                    seen[todo] = np.asarray(shapely.covers(region, lines), dtype=bool)
                for k in near[seen].tolist():
                    if found[k] is None or distance[k] < found[k][0]:
                        found[k] = (float(distance[k]), mark.name)
        return found


def build_corridor_graph(
    model: SpatialModel, floor: str, network: FloorNetwork | None = None
) -> CorridorGraph | None:
    """Corridor centerline graph of a floor (None when the floor has no walkway)."""
    network = network if network is not None else build_floor_network(model, floor)
    if network.walkable is None or not network.node_ids:
        return None
    ids = np.asarray(network.node_ids, dtype=int)
    origin = ids.min(axis=0)
    mask = np.zeros(tuple(ids.max(axis=0) - origin + 1), dtype=bool)
    mask[ids[:, 0] - origin[0], ids[:, 1] - origin[1]] = True
    thinned = np.nonzero(thin(mask))
    kept = {(int(i + origin[0]), int(j + origin[1])) for i, j in zip(*thinned, strict=True)}
    skeleton = _centerline_links(network.graph, kept)
    _join_pieces(network.graph, sorted(network.node_ids), skeleton)

    cells = sorted(skeleton.nodes)
    index = {cell: k for k, cell in enumerate(cells)}
    cell_xy = (np.asarray(cells, dtype=float).reshape(-1, 2) + 0.5) * network.cell_size
    width, shift = clear_widths(network.walkable, cell_xy)
    centered = np.hypot(shift[:, 0], shift[:, 1]) <= CENTER_SHIFT_M
    draw_xy = np.where(centered[:, None], cell_xy + shift, cell_xy)

    landmarks = floor_landmarks(model, floor, network)
    units = sorted(model.units(floor), key=lambda u: (_unit_code(u), u.entity_id))
    anchors: list[tuple[str, Point]] = [
        (u.entity_id, network.access_points[u.entity_id])
        for u in units
        if u.entity_id in network.access_points
    ]
    for mark in landmarks:
        anchors.extend((mark.entity_id, point) for point in mark.points)
    region = network.walkable.buffer(SIGHT_TOL_M)
    shapely.prepare(region)
    attached = _attach(cell_xy, region, [point for _, point in anchors])

    keys = {cells[k] for k in attached} | {cell for cell in cells if skeleton.degree(cell) != 2}
    for component in nx.connected_components(skeleton):
        if not keys & component:
            keys.add(min(component))
    node_index = {cell: n for n, cell in enumerate(sorted(keys))}
    cell_edge = np.full(len(cells), -1, dtype=int)
    cell_node = np.full(len(cells), -1, dtype=int)
    for cell, n in node_index.items():
        cell_node[index[cell]] = n
    edges: list[CorridorEdge] = []
    for chain in _chains(skeleton, node_index):
        inner = [index[c] for c in chain[1:-1]]
        cell_edge[inner] = len(edges)
        # One-cell staircases of a thinned diagonal corridor are smoothed away before measuring.
        steps = simplify_points([(float(cell_xy[index[c], 0]), float(cell_xy[index[c], 1])) for c in chain])
        length = sum(math.dist(a, b) for a, b in zip(steps, steps[1:], strict=False))
        points = simplify_points([(float(draw_xy[index[c], 0]), float(draw_xy[index[c], 1])) for c in chain])
        edges.append(CorridorEdge(node_index[chain[0]], node_index[chain[-1]], length, points, inner))

    node_xy = [draw_xy[index[cell]] for cell in sorted(keys)]
    node_entity: list[str | None] = [None] * len(node_xy)
    access: dict[str, list[int]] = defaultdict(list)
    for (entity_id, point), k in zip(anchors, attached, strict=True):
        node = len(node_xy)
        node_xy.append(np.array([point.x, point.y]))
        node_entity.append(entity_id)
        end = (float(draw_xy[k, 0]), float(draw_xy[k, 1]))
        length = math.dist((point.x, point.y), end)
        edges.append(
            CorridorEdge(
                node, node_index[cells[k]], length, simplify_points([(point.x, point.y), end]), stub=True
            )
        )
        access[entity_id].append(node)
    adjacency: list[list[tuple[int, int, float]]] = [[] for _ in node_xy]
    for e, edge in enumerate(edges):
        if edge.u != edge.v:
            adjacency[edge.u].append((edge.v, e, edge.length))
            adjacency[edge.v].append((edge.u, e, edge.length))
    landings = {
        mark.ref: list(access[mark.entity_id])
        for mark in landmarks
        if mark.kind == "escalator" and access.get(mark.entity_id)
    }
    return CorridorGraph(
        floor=floor,
        walkable=network.walkable,
        cells=cells,
        cell_xy=cell_xy,
        cell_draw=draw_xy,
        cell_width=width,
        cell_neighbors=[[index[c] for c in sorted(skeleton.adj[cell])] for cell in cells],
        cell_edge=cell_edge,
        cell_node=cell_node,
        node_xy=np.asarray(node_xy, dtype=float).reshape(-1, 2),
        node_entity=node_entity,
        n_centerline=len(node_index),
        edges=edges,
        adjacency=adjacency,
        access=dict(access),
        landings=landings,
        landmarks=landmarks,
        units=units,
        walkways=sorted(model.by_kind(EntityKind.WALKWAY, floor), key=lambda w: w.entity_id),
        grid_xy=np.asarray(network.node_xy, dtype=float).reshape(-1, 2),
        bounds=floor_bounds(model, floor),
        cell_size=float(network.cell_size),
    )


# --- routing many trips -----------------------------------------------------------------------------


@dataclass
class ShortestTree:
    """Shortest paths from one node: distance, predecessor node and edge, nodes by increasing distance."""

    source: int
    dist: np.ndarray
    pred_node: np.ndarray
    pred_edge: np.ndarray
    order: np.ndarray


def shortest_tree(adjacency: list[list[tuple[int, int, float]]], source: int) -> ShortestTree:
    """Dijkstra from ``source`` (ties: lower node index first)."""
    n = len(adjacency)
    dist = [math.inf] * n
    pred_node = [-1] * n
    pred_edge = [-1] * n
    done = [False] * n
    order: list[int] = []
    dist[source] = 0.0
    heap = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if done[node]:
            continue
        done[node] = True
        order.append(node)
        for other, edge, length in adjacency[node]:
            candidate = d + length
            if candidate < dist[other] - 1e-9:
                dist[other] = candidate
                pred_node[other] = node
                pred_edge[other] = edge
                heapq.heappush(heap, (candidate, other))
    return ShortestTree(
        source, np.asarray(dist), np.asarray(pred_node), np.asarray(pred_edge), np.asarray(order, dtype=int)
    )


def accumulate(tree: ShortestTree, demand: dict[int, float], edge_flow: list[float]) -> float:
    """Add trips from the tree's source to the ``demand`` nodes onto the edges; returns the weight routed."""
    pending: dict[int, float] = defaultdict(float)
    routed = 0.0
    for node, weight in demand.items():
        if node != tree.source and math.isfinite(tree.dist[node]) and weight:
            pending[node] += weight
            routed += weight
    if not pending:
        return 0.0
    pred_node, pred_edge = tree.pred_node.tolist(), tree.pred_edge.tolist()
    for node in reversed(tree.order.tolist()):
        weight = pending.get(node)
        if not weight or node == tree.source:
            continue
        edge_flow[pred_edge[node]] += weight
        pending[pred_node[node]] += weight
    return routed


@dataclass
class FlowMap:
    """Trips on the corridor edges of every floor."""

    edge_flow: dict[str, np.ndarray]
    legs: dict[str, float]  # walks per floor; a trip between floors walks on two floors
    routed: float
    unrouted: float  # a unit missing from the drawing, or no corridor or escalator connection
    via: dict[str, float]  # trips between floors per escalator
    routed_rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))  # per input row


class FlowRouter:
    """Routes many trips on one revision: corridor graphs and one shortest-path tree per unit entrance."""

    def __init__(self, model: SpatialModel, networks: dict[str, FloorNetwork] | None = None):
        self.model = model
        self.graphs: dict[str, CorridorGraph] = {}
        for floor in floor_codes(model):
            graph = build_corridor_graph(model, floor, (networks or {}).get(floor))
            if graph is not None:
                self.graphs[floor] = graph
        self.unit_node: dict[str, tuple[str, int]] = {}
        for floor, graph in self.graphs.items():
            for unit in graph.units:
                nodes = graph.access.get(unit.entity_id)
                if nodes:
                    self.unit_node.setdefault(_unit_code(unit), (floor, nodes[0]))
        self.codes = sorted(self.unit_node)
        self.code_index = {code: k for k, code in enumerate(self.codes)}
        self.code_floor = np.array([self.unit_node[c][0] for c in self.codes] or [""], dtype=object)
        self.trees = {
            code: shortest_tree(self.graphs[floor].adjacency, node)
            for code, (floor, node) in self.unit_node.items()
        }
        self.links = sorted({ref for graph in self.graphs.values() for ref in graph.landings})
        self.reach = np.full((len(self.codes), len(self.links)), np.inf)
        self.reach_node = np.full((len(self.codes), len(self.links)), -1, dtype=int)
        for k, code in enumerate(self.codes):
            floor, _ = self.unit_node[code]
            tree = self.trees[code]
            for m, link in enumerate(self.links):
                for node in self.graphs[floor].landings.get(link, ()):
                    if tree.dist[node] < self.reach[k, m] - 1e-9:
                        self.reach[k, m] = tree.dist[node]
                        self.reach_node[k, m] = node

    def route(self, pairs: pd.DataFrame) -> FlowMap:
        """Flows of trips given as rows ``unit_a``, ``unit_b``, ``weight`` (one row per trip or per pair)."""
        edge_flow = {floor: [0.0] * len(graph.edges) for floor, graph in self.graphs.items()}
        legs: dict[str, float] = defaultdict(float)
        via: dict[str, float] = defaultdict(float)
        demand: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        ok = np.zeros(len(pairs), dtype=bool)
        if len(pairs):
            a = pairs["unit_a"].astype(str).to_numpy()
            b = pairs["unit_b"].astype(str).to_numpy()
            w = pairs["weight"].astype(float).to_numpy()
            ia = pd.Series(a).map(self.code_index).fillna(-1).astype(int).to_numpy()
            ib = pd.Series(b).map(self.code_index).fillna(-1).astype(int).to_numpy()
            known = (ia >= 0) & (ib >= 0) & (a != b)
            fa, fb = self.code_floor[np.maximum(ia, 0)], self.code_floor[np.maximum(ib, 0)]
            for k in np.flatnonzero(known & (fa == fb)).tolist():
                floor, node = self.unit_node[b[k]]
                if math.isfinite(self.trees[a[k]].dist[node]):
                    demand[a[k]][node] += w[k]
                    legs[floor] += w[k]
                    ok[k] = True
            cross = np.flatnonzero(known & (fa != fb))
            if len(cross) and self.links:
                total = self.reach[ia[cross]] + self.reach[ib[cross]]
                best = np.argmin(total, axis=1)
                reachable = np.isfinite(total[np.arange(len(cross)), best])
                for k, m in zip(cross[reachable].tolist(), best[reachable].tolist(), strict=True):
                    demand[a[k]][int(self.reach_node[ia[k], m])] += w[k]
                    demand[b[k]][int(self.reach_node[ib[k], m])] += w[k]
                    legs[fa[k]] += w[k]
                    legs[fb[k]] += w[k]
                    via[self.links[m]] += w[k]
                    ok[k] = True
        for code, targets in demand.items():
            accumulate(self.trees[code], targets, edge_flow[self.unit_node[code][0]])
        weights = pairs["weight"].astype(float).to_numpy() if len(pairs) else np.zeros(0)
        return FlowMap(
            {floor: np.asarray(values, dtype=float) for floor, values in edge_flow.items()},
            dict(legs),
            float(weights[ok].sum()),
            float(weights[~ok].sum()),
            dict(via),
            ok,
        )


def purchase_trips(rows: pd.DataFrame) -> pd.DataFrame:
    """Consecutive purchases of one customer on one day in different units, one row per trip.

    ``rows`` needs ``customer_key``, ``ts``, ``unit_code`` and ``weight``. Repeated purchases in the same unit
    count as one stop. A trip's ``weight`` is the mean sampling weight of its two purchases.
    """
    if rows.empty:
        return pd.DataFrame(columns=TRIP_COLUMNS)
    keys = pd.factorize(rows["customer_key"])[0]
    ts = rows["ts"].to_numpy(dtype="datetime64[ns]")
    order = np.lexsort((ts, keys))
    keys, ts = keys[order], ts[order]
    day = ts.astype("datetime64[D]")
    unit = rows["unit_code"].astype(str).to_numpy()[order]
    weight = rows["weight"].to_numpy(dtype=float)[order]
    customer = rows["customer_key"].astype(str).to_numpy()[order]
    same = np.r_[False, (keys[1:] == keys[:-1]) & (day[1:] == day[:-1])]
    stop = ~(same & np.r_[False, unit[1:] == unit[:-1]])
    keys, day, unit, weight, customer = keys[stop], day[stop], unit[stop], weight[stop], customer[stop]
    second = np.flatnonzero(np.r_[False, (keys[1:] == keys[:-1]) & (day[1:] == day[:-1])])
    return pd.DataFrame(
        {
            "customer_key": customer[second - 1],
            "day": day[second - 1],
            "unit_a": unit[second - 1],
            "unit_b": unit[second],
            "weight": (weight[second - 1] + weight[second]) / 2.0,
        },
        columns=TRIP_COLUMNS,
    )


def booth_spots(graph: CorridorGraph, side_m: float, clear_m: float, keep_m: float) -> pd.DataFrame:
    """Walkway grid cell centers where a square booth fits with room to walk around it.

    The booth (``side_m`` wide) keeps ``clear_m`` from every corridor boundary (storefronts, walls, escalator
    wells) and ``keep_m`` from escalator landings, stairs, elevators and entrances. Columns: x, y, open_m
    (diameter of the free circle around the center).
    """
    columns = ["x", "y", "open_m"]
    xy = graph.grid_xy
    inner = graph.walkable.buffer(-clear_m, join_style="mitre") if len(xy) else None
    if inner is None or inner.is_empty:
        return pd.DataFrame(columns=columns)
    shapely.prepare(inner)
    half = side_m / 2.0
    boxes = shapely.box(xy[:, 0] - half, xy[:, 1] - half, xy[:, 0] + half, xy[:, 1] + half)
    fits = np.asarray(shapely.contains(inner, boxes), dtype=bool)
    marks = [(p.x, p.y) for mark in graph.landmarks for p in mark.points]
    if marks and fits.any():
        far = np.asarray(shapely.distance(boxes[fits], shapely.multipoints(marks)), dtype=float) >= keep_m
        fits[np.flatnonzero(fits)[~far]] = False
    room = 2.0 * np.asarray(shapely.distance(shapely.points(xy[fits]), graph.walkable.boundary), dtype=float)
    return pd.DataFrame({"x": xy[fits, 0], "y": xy[fits, 1], "open_m": room}, columns=columns)
