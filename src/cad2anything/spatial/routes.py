"""Estimated walking routes between units along the walkway networks.

Used to draw a customer's purchase sequence on the floor plan. Purchases tell where and when a customer
paid, not how they walked: a route is the shortest walkway path between two store entrances, and a floor
change goes through the escalator that minimizes the total walking distance on both floors.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import networkx as nx
from shapely.geometry import LineString

from cad2anything.model import Entity, EntityKind, SpatialModel
from cad2anything.spatial.features import FloorNetwork, build_floor_network, link_type

ROUTE_LINK_TYPES = ("escalator",)
SIMPLIFY_M = 0.25


@dataclass
class RouteLeg:
    floor: str
    points: list[tuple[float, float]]
    length_m: float


@dataclass
class Route:
    legs: list[RouteLeg] = field(default_factory=list)
    via: str | None = None
    straight: bool = False  # fallback when no walkway path exists

    @property
    def length_m(self) -> float:
        return sum(leg.length_m for leg in self.legs)


def _simplify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) > 2:
        points = list(LineString(points).simplify(SIMPLIFY_M, preserve_topology=False).coords)
    return [(round(float(x), 2), round(float(y), 2)) for x, y in points]


class RoutePlanner:
    """Shortest walkway routes between units of one spatial model (floor networks are built lazily)."""

    def __init__(self, model: SpatialModel, link_types: Iterable[str] = ROUTE_LINK_TYPES):
        self.model = model
        self._networks: dict[str, FloorNetwork] = {}
        self._searches: dict[tuple[str, str], tuple[dict, dict] | None] = {}
        self._units: dict[str, Entity] = {
            str(u.attributes.get("unit_code")): u for u in model.units() if u.attributes.get("unit_code")
        }
        self._links: dict[str, dict[str, str]] = {}
        allowed = set(link_types)
        for entity in model.by_kind(EntityKind.VERTICAL_LINK):
            if link_type(entity) in allowed and entity.floor:
                link_id = str(entity.attributes.get("link_id") or entity.entity_id)
                self._links.setdefault(link_id, {})[entity.floor] = entity.entity_id

    def has_unit(self, unit_code: str) -> bool:
        return unit_code in self._units

    def network(self, floor: str) -> FloorNetwork:
        if floor not in self._networks:
            self._networks[floor] = build_floor_network(self.model, floor)
        return self._networks[floor]

    def access_xy(self, unit_code: str) -> tuple[float, float]:
        """Entrance of a unit (where it meets the walkway), or its centroid without a walkway."""
        unit = self._units[unit_code]
        point = self.network(unit.floor or "").access_points.get(unit.entity_id) or unit.geometry.centroid
        return round(float(point.x), 2), round(float(point.y), 2)

    def _search(self, floor: str, entity_id: str) -> tuple[dict, dict] | None:
        key = (floor, entity_id)
        if key not in self._searches:
            nodes = self.network(floor).access_nodes.get(entity_id)
            self._searches[key] = (
                nx.single_source_dijkstra(self.network(floor).graph, nodes[0], weight="weight")
                if nodes
                else None
            )
        return self._searches[key]

    def _nearest(
        self, search: tuple[dict, dict] | None, floor: str, entity_id: str
    ) -> tuple[float, str] | None:
        if search is None:
            return None
        reached = [
            (search[0][n], n) for n in self.network(floor).access_nodes.get(entity_id, ()) if n in search[0]
        ]
        return min(reached) if reached else None

    def _leg(self, floor: str, nodes: list, length: float) -> RouteLeg:
        graph = self.network(floor).graph
        points = [tuple(graph.nodes[n]["pos"]) for n in nodes]
        return RouteLeg(floor, _simplify(points), round(float(length), 1))

    def _straight(self, a: Entity, b: Entity) -> Route:
        pa, pb = (
            self.access_xy(str(a.attributes["unit_code"])),
            self.access_xy(str(b.attributes["unit_code"])),
        )
        if a.floor == b.floor:
            return Route([RouteLeg(a.floor or "", [pa, pb], round(math.dist(pa, pb), 1))], straight=True)
        return Route([RouteLeg(a.floor or "", [pa], 0.0), RouteLeg(b.floor or "", [pb], 0.0)], straight=True)

    def route(self, unit_a: str, unit_b: str) -> Route:
        """Route from the entrance of ``unit_a`` to the entrance of ``unit_b``."""
        a, b = self._units[unit_a], self._units[unit_b]
        floor_a, floor_b = a.floor or "", b.floor or ""
        from_a = self._search(floor_a, a.entity_id)
        if floor_a == floor_b:
            target = self._nearest(from_a, floor_b, b.entity_id)
            if target is None:
                return self._straight(a, b)
            distance, node = target
            return Route([self._leg(floor_a, from_a[1][node], distance)])
        from_b = self._search(floor_b, b.entity_id)
        best: tuple[float, str, str, float, str, float] | None = None
        for link_id, by_floor in sorted(self._links.items()):
            if floor_a not in by_floor or floor_b not in by_floor:
                continue
            near_a = self._nearest(from_a, floor_a, by_floor[floor_a])
            near_b = self._nearest(from_b, floor_b, by_floor[floor_b])
            if near_a is None or near_b is None:
                continue
            total = near_a[0] + near_b[0]
            if best is None or total < best[0] - 1e-9:
                best = (total, link_id, near_a[1], near_a[0], near_b[1], near_b[0])
        if best is None:
            return self._straight(a, b)
        _, link_id, node_a, dist_a, node_b, dist_b = best
        leg_a = self._leg(floor_a, from_a[1][node_a], dist_a)
        leg_b = self._leg(floor_b, list(reversed(from_b[1][node_b])), dist_b)
        return Route([leg_a, leg_b], via=link_id)
