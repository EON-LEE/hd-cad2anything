"""Spatial vocabulary: deterministic attributes that put a unit's place on the floor into planners' words.

Agents should not reason over coordinates. These attributes turn the geometry into the terms people use
("에스컬레이터 정면", "동쪽", "코너 매장", "메인 동선") so an agent can filter, aggregate and explain with
them, and every answer can be traced back to the drawing:

- ``side``: position on the drawing, up = 북. Nine sectors from thirds of the floor outline.
- ``frontage_sides`` / ``corner``: outward directions of the facades that face a walkway (at least
  ``MIN_FACADE_M``); a corner unit faces walkways on two perpendicular sides.
- ``escalator_front`` / ``escalator_view_m``: the unit entrance is in clear sight from an escalator landing
  (the straight line stays inside the walkways). "Front" also needs the entrance within ``FRONT_ANGLE_DEG``
  of the direction a rider walks off the escalator and within ``FRONT_DISTANCE_M``.
- ``entrance_front`` / ``entrance_view_m``: the same seen from a building entrance.
- ``traffic_potential``: how strongly the shortest walks between landmarks (escalators, elevators, stairs,
  entrances) pass in front of the unit, scaled to 0..1 per floor. It comes from the drawing alone and is not
  observed traffic. ``main_walkway`` marks units at or above ``MAIN_WALKWAY_SHARE``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from cad2anything.model import Entity, EntityKind, SpatialModel
from cad2anything.spatial.features import (
    MIN_SHARED_M,
    FloorNetwork,
    access_points,
    build_floor_network,
    floor_codes,
    link_type,
    shared_segments,
)

FRONT_DISTANCE_M = 20.0
FRONT_ANGLE_DEG = 45.0
VIEW_MAX_M = 60.0
SIGHT_BUFFER_M = 0.3
MIN_FACADE_M = 2.0
PASSING_M = 2.5
MAIN_WALKWAY_SHARE = 0.5
DIRECTIONS = ("동", "북", "서", "남")  # outward normal angle 0°, 90°, 180°, 270°
SIDE_ORDER = ("북서", "북", "북동", "서", "중앙", "동", "남서", "남", "남동")
LANDMARK_KINDS = {"escalator": "에스컬레이터", "elevator": "엘리베이터", "stair": "계단"}
VOCABULARY_COLUMNS = [
    "unit_code",
    "floor",
    "zone_name",
    "side",
    "frontage_sides",
    "facade_directions",
    "corner",
    "escalator_front",
    "escalator_view_m",
    "front_escalator",
    "entrance_front",
    "entrance_view_m",
    "traffic_potential",
    "main_walkway",
]


@dataclass
class Landmark:
    """A place people walk to or from: vertical link, building entrance or amenity."""

    entity_id: str
    ref: str  # ESC-A, EV-1, ST-1, ENT-1, RESTROOM...
    kind: str  # escalator, elevator, stairs, entrance, amenity
    name: str
    floor: str
    side: str
    x: float
    y: float


def side_of(x: float, y: float, bounds: tuple[float, float, float, float]) -> str:
    """Sector of a point on the drawing (up = 북): 북서, 북, 북동, 서, 중앙, 동, 남서, 남, 남동."""
    minx, miny, maxx, maxy = bounds
    u = (x - minx) / max(maxx - minx, 1e-9)
    v = (y - miny) / max(maxy - miny, 1e-9)
    north_south = "북" if v > 2 / 3 else "남" if v < 1 / 3 else ""
    east_west = "동" if u > 2 / 3 else "서" if u < 1 / 3 else ""
    return (north_south + east_west) or "중앙"


def floor_bounds(model: SpatialModel, floor: str) -> tuple[float, float, float, float] | None:
    """Outline of a floor: its exterior, else the extent of everything on it."""
    exteriors = [e.geometry for e in model.by_kind(EntityKind.EXTERIOR, floor) if not e.geometry.is_empty]
    geoms = exteriors or [
        e.geometry for e in model.entities.values() if e.floor == floor and not e.geometry.is_empty
    ]
    if not geoms:
        return None
    bounds = np.array([g.bounds for g in geoms])
    return (
        float(bounds[:, 0].min()),
        float(bounds[:, 1].min()),
        float(bounds[:, 2].max()),
        float(bounds[:, 3].max()),
    )


def _direction(nx_: float, ny_: float) -> str:
    angle = math.degrees(math.atan2(ny_, nx_)) % 360.0
    return DIRECTIONS[int(((angle + 45.0) % 360.0) // 90.0)]


def facade_directions(
    geometry: BaseGeometry, walkable: BaseGeometry | None, min_length: float = MIN_FACADE_M
) -> list[str]:
    """Outward directions (동/북/서/남) of the facade pieces that face a walkway, longest total first."""
    if walkable is None or walkable.is_empty:
        return []
    lengths: dict[str, float] = defaultdict(float)
    for piece in shared_segments(geometry, walkable):
        (x0, y0), (x1, y1) = piece.coords[0], piece.coords[-1]
        length = math.hypot(x1 - x0, y1 - y0)
        if length <= 0:
            continue
        nx_, ny_ = (y1 - y0) / length, -(x1 - x0) / length
        probe = Point((x0 + x1) / 2 + nx_ * 0.2, (y0 + y1) / 2 + ny_ * 0.2)
        if geometry.contains(probe):
            nx_, ny_ = -nx_, -ny_
        lengths[_direction(nx_, ny_)] += length
    kept = [(length, d) for d, length in lengths.items() if length + 1e-9 >= min_length]
    return [d for _, d in sorted(kept, key=lambda item: (-item[0], DIRECTIONS.index(item[1])))]


def _unit_vector(dx: float, dy: float) -> tuple[float, float] | None:
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm > 1e-9 else None


def _into_walkway(entity: Entity, point: Point, walkable: BaseGeometry | None) -> tuple[float, float] | None:
    """Normal of an opening (a line on the outer wall) that points into the walkways."""
    coords = list(entity.geometry.coords) if entity.geometry.geom_type == "LineString" else []
    if len(coords) < 2 or walkable is None:
        return None
    (x0, y0), (x1, y1) = coords[0][:2], coords[-1][:2]
    normal = _unit_vector(y1 - y0, -(x1 - x0))
    if normal is None:
        return None
    ahead = Point(point.x + normal[0], point.y + normal[1])
    behind = Point(point.x - normal[0], point.y - normal[1])
    return normal if walkable.distance(ahead) <= walkable.distance(behind) else (-normal[0], -normal[1])


def _landings(entity: Entity, network: FloorNetwork) -> list[tuple[Point, tuple[float, float] | None]]:
    """Points where people step off a landmark, with the direction they face then (None if unknown).

    Escalators are boarded at the ends of their long axis, so only access points there count.
    Entrances face into the building, along the door's normal towards the walkways.
    """
    points = network.all_access_points.get(entity.entity_id) or []
    centroid = entity.geometry.centroid
    landings: list[tuple[Point, tuple[float, float] | None]] = []
    axis: tuple[float, float] | None = None
    if entity.kind == EntityKind.VERTICAL_LINK and link_type(entity) == "escalator":
        rect = entity.geometry.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)[:4] if hasattr(rect, "exterior") else []
        if len(coords) == 4:
            edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
            (ax, ay), (bx, by) = max(edges, key=lambda e: math.dist(e[0], e[1]))
            axis = _unit_vector(bx - ax, by - ay)
    for point in points:
        if entity.kind == EntityKind.OPENING:
            facing = _into_walkway(entity, point, network.walkable)
        else:
            facing = _unit_vector(point.x - centroid.x, point.y - centroid.y)
        # Long sides of an escalator are not landings: keep points along its axis only.
        if (
            axis is not None
            and facing is not None
            and abs(facing[0] * axis[0] + facing[1] * axis[1]) < math.cos(math.radians(45.0))
        ):
            continue
        landings.append((point, facing))
    return landings


def floor_landmarks(model: SpatialModel, floor: str, network: FloorNetwork | None = None) -> list[Landmark]:
    """Vertical links, entrances and amenities of a floor with their sector and access point."""
    network = network or build_floor_network(model, floor)
    bounds = floor_bounds(model, floor)
    found: list[Landmark] = []
    for entity in sorted(model.entities.values(), key=lambda e: e.entity_id):
        if entity.floor != floor or entity.geometry.is_empty:
            continue
        attrs = entity.attributes
        if entity.kind == EntityKind.VERTICAL_LINK:
            kind = {"stair": "stairs"}.get(link_type(entity) or "", link_type(entity) or "vertical_link")
            ref = str(attrs.get("link_id") or entity.entity_id)
            name = str(attrs.get("name_ko") or LANDMARK_KINDS.get(link_type(entity) or "", ref))
        elif entity.kind == EntityKind.OPENING and attrs.get("opening_type", "entrance") == "entrance":
            kind, ref = "entrance", str(attrs.get("opening_id") or entity.entity_id)
            name = str(attrs.get("name_ko") or ref)
        elif entity.kind == EntityKind.AMENITY:
            kind, ref = "amenity", str(attrs.get("amenity_type") or entity.entity_id)
            name = str(attrs.get("name_ko") or ref)
        else:
            continue
        point = network.access_points.get(entity.entity_id) or entity.geometry.centroid
        side = side_of(point.x, point.y, bounds) if bounds else "중앙"
        found.append(
            Landmark(entity.entity_id, ref, kind, name, floor, side, round(point.x, 2), round(point.y, 2))
        )
    return found


def _sightlines(
    region: BaseGeometry, origin: Point, targets: np.ndarray, max_m: float
) -> tuple[np.ndarray, np.ndarray]:
    """Distances from ``origin`` to each target point and whether each straight line stays in ``region``."""
    dist = np.hypot(targets[:, 0] - origin.x, targets[:, 1] - origin.y)
    visible = np.zeros(len(targets), dtype=bool)
    candidates = np.flatnonzero((dist <= max_m) & (dist > 1e-6))
    if len(candidates):
        lines = shapely.linestrings(
            np.stack(
                [np.repeat([[origin.x, origin.y]], len(candidates), axis=0), targets[candidates]], axis=1
            )
        )
        visible[candidates] = np.asarray(shapely.covers(region, lines), dtype=bool)
    return dist, visible


def _front_view(
    landmarks: list[tuple[Entity, list[tuple[Point, tuple[float, float] | None]]]],
    region: BaseGeometry,
    targets: np.ndarray,
    owners: np.ndarray,
    n_units: int,
    front_m: float,
) -> tuple[np.ndarray, np.ndarray, list[str | None]]:
    """Per unit: nearest clear view distance, in front of a landing, and that landmark's reference.

    ``targets`` are facade points (one row each) and ``owners`` the index of the unit each belongs to, so
    a unit counts as seen when any of its walkway-facing facades is seen.
    """
    view = np.full(n_units, np.nan)
    front = np.zeros(n_units, dtype=bool)
    front_ref: list[str | None] = [None] * n_units
    front_best = np.full(n_units, np.inf)
    cos_front = math.cos(math.radians(FRONT_ANGLE_DEG))
    for entity, landings in landmarks:
        ref = str(entity.attributes.get("link_id") or entity.attributes.get("opening_id") or entity.entity_id)
        for point, facing in landings:
            dist, visible = _sightlines(region, point, targets, VIEW_MAX_M)
            seen = np.full(n_units, np.inf)
            np.minimum.at(seen, owners[visible], dist[visible])
            better = np.isfinite(seen) & (seen < np.where(np.isnan(view), np.inf, view))
            view = np.where(better, seen, view)
            if facing is None:
                continue
            dx, dy = targets[:, 0] - point.x, targets[:, 1] - point.y
            with np.errstate(invalid="ignore", divide="ignore"):
                cos = (dx * facing[0] + dy * facing[1]) / np.where(dist > 0, dist, np.nan)
            ahead = visible & (dist <= front_m) & (np.nan_to_num(cos, nan=-1.0) >= cos_front)
            nearest_ahead = np.full(n_units, np.inf)
            np.minimum.at(nearest_ahead, owners[ahead], dist[ahead])
            for k in np.flatnonzero(nearest_ahead < front_best).tolist():
                front_best[k] = nearest_ahead[k]
                front_ref[k] = ref
            front |= np.isfinite(nearest_ahead)
    return view, front, front_ref


def _traffic(network: FloorNetwork, landmark_ids: list[str], units: list[Entity]) -> dict[str, float]:
    """Raw passing value per unit: max subset betweenness of walkway nodes within ``PASSING_M`` of it."""
    sources = sorted({n for entity_id in landmark_ids for n in network.access_nodes.get(entity_id, ())})
    if len(sources) < 2 or not network.node_ids:
        return {}
    centrality = nx.betweenness_centrality_subset(
        network.graph, sources=sources, targets=sources, normalized=False, weight="weight"
    )
    values = np.array([centrality.get(node, 0.0) for node in network.node_ids])
    points = shapely.points(network.node_xy)
    result: dict[str, float] = {}
    for unit in units:
        near = np.asarray(shapely.dwithin(unit.geometry, points, PASSING_M), dtype=bool)
        result[unit.entity_id] = float(values[near].max()) if near.any() else 0.0
    return result


def floor_vocabulary(model: SpatialModel, floor: str, network: FloorNetwork | None = None) -> pd.DataFrame:
    """Vocabulary rows for the units of one floor (``VOCABULARY_COLUMNS``)."""
    units = sorted(model.units(floor), key=lambda u: str(u.attributes.get("unit_code") or u.entity_id))
    if not units:
        return pd.DataFrame(columns=VOCABULARY_COLUMNS)
    network = network or build_floor_network(model, floor)
    bounds = floor_bounds(model, floor)
    zone_names = {
        str(z.attributes.get("zone_code")): str(z.attributes.get("zone_name") or "")
        for z in model.by_kind(EntityKind.ZONE, floor)
        if z.attributes.get("zone_code")
    }
    facade_points: list[tuple[float, float]] = []
    owner_index: list[int] = []
    for k, unit in enumerate(units):
        points = (
            access_points(unit.geometry, network.walkable, min_length=MIN_SHARED_M)
            if network.walkable is not None
            else [unit.geometry.centroid]
        )
        for point in points:
            facade_points.append((point.x, point.y))
            owner_index.append(k)
    targets = np.array(facade_points, dtype=float).reshape(-1, 2)
    owners = np.array(owner_index, dtype=int)
    escalators = [e for e in model.by_kind(EntityKind.VERTICAL_LINK, floor) if link_type(e) == "escalator"]
    entrances = [
        e
        for e in model.by_kind(EntityKind.OPENING, floor)
        if e.attributes.get("opening_type", "entrance") == "entrance"
    ]
    if network.walkable is not None and not network.walkable.is_empty:
        region = network.walkable.buffer(SIGHT_BUFFER_M)
        shapely.prepare(region)
        esc_view, esc_front, esc_ref = _front_view(
            [(e, _landings(e, network)) for e in escalators],
            region,
            targets,
            owners,
            len(units),
            FRONT_DISTANCE_M,
        )
        ent_view, ent_front, _ = _front_view(
            [(e, _landings(e, network)) for e in entrances],
            region,
            targets,
            owners,
            len(units),
            FRONT_DISTANCE_M + 5.0,
        )
    else:
        esc_view = ent_view = np.full(len(units), np.nan)
        esc_front = ent_front = np.zeros(len(units), dtype=bool)
        esc_ref = [None] * len(units)
    landmark_ids = [
        e.entity_id
        for e in model.entities.values()
        if e.floor == floor
        and (
            e.kind == EntityKind.VERTICAL_LINK
            or (e.kind == EntityKind.OPENING and e.attributes.get("opening_type", "entrance") == "entrance")
        )
    ]
    traffic = _traffic(network, landmark_ids, units)
    peak = max(traffic.values(), default=0.0)
    rows = []
    for k, unit in enumerate(units):
        attrs = unit.attributes
        directions = facade_directions(unit.geometry, network.walkable)
        corner = bool({"동", "서"} & set(directions)) and bool({"북", "남"} & set(directions))
        potential = traffic.get(unit.entity_id, 0.0) / peak if peak > 0 else 0.0
        centroid = unit.geometry.centroid
        rows.append(
            {
                "unit_code": str(attrs.get("unit_code") or unit.entity_id),
                "floor": floor,
                "zone_name": zone_names.get(str(attrs.get("zone_code")), ""),
                "side": side_of(centroid.x, centroid.y, bounds) if bounds else "중앙",
                "frontage_sides": len(directions),
                "facade_directions": "·".join(directions),
                "corner": corner,
                "escalator_front": bool(esc_front[k]),
                "escalator_view_m": None if np.isnan(esc_view[k]) else round(float(esc_view[k]), 1),
                "front_escalator": esc_ref[k],
                "entrance_front": bool(ent_front[k]),
                "entrance_view_m": None if np.isnan(ent_view[k]) else round(float(ent_view[k]), 1),
                "traffic_potential": round(potential, 3),
                "main_walkway": potential >= MAIN_WALKWAY_SHARE,
            }
        )
    return pd.DataFrame(rows, columns=VOCABULARY_COLUMNS)


def unit_vocabulary(model: SpatialModel, networks: dict[str, FloorNetwork] | None = None) -> pd.DataFrame:
    """Vocabulary rows for every unit of the model, floors bottom-up."""
    frames = [
        floor_vocabulary(model, floor, (networks or {}).get(floor))
        for floor in floor_codes(model)
        if model.units(floor)
    ]
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=VOCABULARY_COLUMNS)
