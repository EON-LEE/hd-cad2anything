"""Corridor width and escape distance review on the drawing (검토용, not a code compliance check).

- Clear width along the corridors comes from the corridor centerlines of ``cad2anything.spatial.flows``: each
  centerline cell knows the wall-to-wall width across the corridor there (storefronts, walls and escalator
  wells are the walls). Centerline cells narrower than the review width form narrow stretches.
- Travel distance: the walk along the 1 m walkway grid from each unit's entrance (where it meets the walkway)
  to the nearest stair (vertical link of type ``stair``) or building entrance (opening) on the same floor.
  The walk inside the unit is not included.

The review values (2.4 m, 50 m by default) are examples a reviewer can change; they are not legal criteria.
Fixtures and temporary booths narrow a corridor only when they are part of the model's walkway geometry.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from cad2anything.model import EntityKind, SpatialModel
from cad2anything.spatial.features import FloorNetwork, link_type
from cad2anything.spatial.flows import CorridorGraph, link_name, simplify_points

DEFAULT_MIN_WIDTH_M = 2.4
DEFAULT_MAX_TRAVEL_M = 50.0
EXIT_LINK_TYPES = ("stair",)


@dataclass
class NarrowStretch:
    """A chain of corridor centerline narrower than the review width."""

    floor: str
    points: list[tuple[float, float]]
    min_width_m: float
    length_m: float
    x: float  # narrowest point
    y: float
    label: str


@dataclass
class ExitReach:
    """Walking distance from a unit entrance to the nearest stair or building entrance on its floor."""

    unit_code: str
    floor: str
    brand_name: str | None
    vacant: bool
    travel_m: float  # NaN when no exit is reachable along the walkways
    exit_ref: str | None  # ST-1, ENT-1
    exit_name: str | None  # 계단 1, 정문(ENT-1)
    x: float
    y: float


def width_stats(graph: CorridorGraph) -> dict[str, float | None]:
    """Minimum and median clear width (m) over the corridor centerline of a floor."""
    widths = graph.cell_width[np.isfinite(graph.cell_width)]
    if not len(widths):
        return {"min_width_m": None, "median_width_m": None}
    return {"min_width_m": float(widths.min()), "median_width_m": float(np.median(widths))}


def _longest_chain(graph: CorridorGraph, component: list[int]) -> list[int]:
    """Cells of the longest path inside a connected set of centerline cells (two breadth-first sweeps)."""
    members = set(component)

    def sweep(start: int) -> tuple[int, dict[int, int]]:
        parent = {start: start}
        queue, last = deque([start]), start
        while queue:
            last = queue.popleft()
            for other in graph.cell_neighbors[last]:
                if other in members and other not in parent:
                    parent[other] = last
                    queue.append(other)
        return last, parent

    end, _ = sweep(min(component))
    other_end, parent = sweep(end)
    path = [other_end]
    while path[-1] != end:
        path.append(parent[path[-1]])
    return path


def narrow_stretches(graph: CorridorGraph, min_width_m: float) -> list[NarrowStretch]:
    """Connected centerline stretches narrower than ``min_width_m``, narrowest first."""
    width = np.nan_to_num(graph.cell_width, nan=np.inf)
    narrow = {int(k) for k in np.flatnonzero(width < min_width_m)}
    seen: set[int] = set()
    found: list[NarrowStretch] = []
    for start in sorted(narrow):
        if start in seen:
            continue
        component, stack = [], [start]
        seen.add(start)
        while stack:
            k = stack.pop()
            component.append(k)
            for other in graph.cell_neighbors[k]:
                if other in narrow and other not in seen:
                    seen.add(other)
                    stack.append(other)
        path = _longest_chain(graph, component)
        coords = [(float(graph.cell_draw[k, 0]), float(graph.cell_draw[k, 1])) for k in path]
        steps = sum(math.dist(a, b) for a, b in zip(coords, coords[1:], strict=False))
        tightest = min(component, key=lambda k: (width[k], k))
        x, y = float(graph.cell_draw[tightest, 0]), float(graph.cell_draw[tightest, 1])
        points = simplify_points(coords) if len(coords) > 1 else [coords[0], coords[0]]
        found.append(
            NarrowStretch(
                floor=graph.floor,
                points=points,
                min_width_m=round(float(width[tightest]), 2),
                length_m=round(steps + graph.cell_size, 1),
                x=round(x, 2),
                y=round(y, 2),
                label=graph.label_at(x, y),
            )
        )
    return sorted(found, key=lambda s: (s.min_width_m, -s.length_m, s.x, s.y))


def exit_distances(model: SpatialModel, floor: str, network: FloorNetwork) -> list[ExitReach]:
    """Walking distance from every unit entrance on a floor to its nearest stair or building entrance."""
    exits: dict[str, tuple[str, str]] = {}
    for entity in model.by_kind(EntityKind.VERTICAL_LINK, floor):
        kind = link_type(entity)
        if kind in EXIT_LINK_TYPES:
            ref = str(entity.attributes.get("link_id") or entity.entity_id)
            exits[entity.entity_id] = (ref, link_name(kind, ref))
    for entity in model.by_kind(EntityKind.OPENING, floor):
        ref = str(entity.attributes.get("opening_id") or entity.entity_id)
        name = entity.attributes.get("name_ko")
        exits[entity.entity_id] = (ref, f"{name}({ref})" if name else ref)
    targets = [entity_id for entity_id in exits if entity_id in network.access_nodes]
    distances, nearest = network.distances_from(targets) if targets else ({}, {})
    found: list[ExitReach] = []
    units = sorted(model.units(floor), key=lambda u: str(u.attributes.get("unit_code") or u.entity_id))
    for unit in units:
        attrs = unit.attributes
        point = network.access_points.get(unit.entity_id) or unit.geometry.centroid
        target = nearest.get(unit.entity_id)
        ref, name = exits.get(target, (None, None)) if target else (None, None)
        vacant = bool(attrs.get("vacant")) if attrs.get("vacant") is not None else not attrs.get("brand_id")
        found.append(
            ExitReach(
                unit_code=str(attrs.get("unit_code") or unit.entity_id),
                floor=floor,
                brand_name=None if vacant else attrs.get("brand_name"),
                vacant=vacant,
                travel_m=round(float(distances.get(unit.entity_id, math.nan)), 1),
                exit_ref=ref,
                exit_name=name,
                x=round(float(point.x), 2),
                y=round(float(point.y), 2),
            )
        )
    return found
