"""Estimated walking routes between places along the walkway networks.

Used to draw a customer's purchase sequence and to answer "how do I get from A to B". Purchases tell where
and when a customer paid, not how they walked: a route is the shortest walkway path between two access
points (store entrances, building entrances, escalator landings...), and a floor change goes through the
vertical link of the requested type that minimizes the total walking distance on both floors.

Without a walkway path the route falls back to straight lines (``Route.straight``): on one floor straight
from access point to access point, between floors straight to the nearest vertical link of the requested
type on each floor. When no such link connects the two floors nothing can be measured and the legs keep
``length_m is None`` (unknown), so that a fallback is never reported as a 0 m walk.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import networkx as nx
from shapely.geometry import LineString

from cad2anything.model import Entity, EntityKind, SpatialModel
from cad2anything.spatial.features import (
    MIN_SHARED_M,
    FloorNetwork,
    access_points,
    build_floor_network,
    link_type,
    multi_source_dijkstra,
)

ROUTE_LINK_TYPES = ("escalator",)
VIA_TYPES = {"escalator": "escalator", "elevator": "elevator", "stairs": "stair", "stair": "stair"}
SIMPLIFY_M = 0.25


@dataclass
class RouteLeg:
    floor: str
    points: list[tuple[float, float]]
    length_m: float | None  # None when the distance cannot be measured


@dataclass
class Route:
    legs: list[RouteLeg] = field(default_factory=list)
    via: str | None = None
    straight: bool = False  # fallback when no walkway path exists

    @property
    def length_m(self) -> float | None:
        """Total walking distance, or None when any leg could not be measured."""
        if any(leg.length_m is None for leg in self.legs):
            return None
        return sum(float(leg.length_m) for leg in self.legs)


def _simplify(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) > 2:
        points = list(LineString(points).simplify(SIMPLIFY_M, preserve_topology=False).coords)
    return [(round(float(x), 2), round(float(y), 2)) for x, y in points]


class RoutePlanner:
    """Shortest walkway routes between places of one spatial model (floor networks are built lazily)."""

    def __init__(self, model: SpatialModel, link_types: Iterable[str] = ROUTE_LINK_TYPES):
        self.model = model
        self.default_via = next(iter(link_types), "escalator")
        self._networks: dict[str, FloorNetwork] = {}
        self._searches: dict[tuple[str, str], tuple[dict, dict] | None] = {}
        self._units: dict[str, Entity] = {
            str(u.attributes.get("unit_code")): u for u in model.units() if u.attributes.get("unit_code")
        }
        # link type -> link ID -> floor -> entity ID
        self._links_by_type: dict[str, dict[str, dict[str, str]]] = {}
        for entity in model.by_kind(EntityKind.VERTICAL_LINK):
            kind = link_type(entity)
            if kind and entity.floor:
                link_id = str(entity.attributes.get("link_id") or entity.entity_id)
                self._links_by_type.setdefault(kind, {}).setdefault(link_id, {})[entity.floor] = (
                    entity.entity_id
                )

    def has_unit(self, unit_code: str) -> bool:
        return unit_code in self._units

    def network(self, floor: str) -> FloorNetwork:
        if floor not in self._networks:
            self._networks[floor] = build_floor_network(self.model, floor)
        return self._networks[floor]

    def entity_xy(self, entity: Entity) -> tuple[float, float]:
        """Access point of an entity (where it meets the walkway), or its centroid without a walkway."""
        point = (
            self.network(entity.floor or "").access_points.get(entity.entity_id) or entity.geometry.centroid
        )
        return round(float(point.x), 2), round(float(point.y), 2)

    def access_xy(self, unit_code: str) -> tuple[float, float]:
        """Entrance of a unit (where it meets the walkway), or its centroid without a walkway."""
        return self.entity_xy(self._units[unit_code])

    def walk_distances(self, entity: Entity) -> dict[str, float]:
        """Walking distance from any access point of ``entity`` to every attached entity on its floor."""
        network = self.network(entity.floor or "")
        if entity.entity_id not in network.access_nodes:
            return {}
        return network.distances_from([entity.entity_id])[0]

    def _facade_nodes(self, network: FloorNetwork, entity: Entity) -> list[tuple[object, float]]:
        """Grid nodes next to every walkway-facing facade of an entity, with the snap distance."""
        if network.walkable is None:
            return []
        nodes = []
        for point in access_points(entity.geometry, network.walkable, min_length=MIN_SHARED_M):
            found = network.nearest_node(point)
            if found is not None:
                nodes.append(found)
        return nodes

    def facade_distances(self, source: Entity) -> dict[str, float]:
        """Walking distance from ``source`` to the nearest walkway-facing facade of each unit on its floor.

        Units count from any facade, not only their main entrance, so a store whose side faces an
        escalator landing is near it. Landmarks start from all their access points.
        """
        floor = source.floor or ""
        network = self.network(floor)
        if source.kind == EntityKind.UNIT:
            starts = [node for node, _ in self._facade_nodes(network, source)]
        else:
            starts = list(network.access_nodes.get(source.entity_id, ()))
        if not starts:
            return {}
        dist, _ = multi_source_dijkstra(network.graph, {node: "source" for node in starts})
        result: dict[str, float] = {}
        for unit in self.model.units(floor):
            reached = [dist[node] + snap for node, snap in self._facade_nodes(network, unit) if node in dist]
            if reached:
                result[unit.entity_id] = 0.0 if unit.entity_id == source.entity_id else min(reached)
        return result

    def link_floors(self, via: str | None = None) -> dict[str, dict[str, str]]:
        """Vertical links of a type: link ID -> floor -> entity ID."""
        kind = VIA_TYPES.get(via or self.default_via, via or self.default_via)
        return self._links_by_type.get(kind, {})

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

    def _straight(self, a: Entity, b: Entity, via: str | None = None) -> Route:
        """Straight-line fallback used when the walkways do not connect the two places.

        On one floor this is the straight distance between the access points. Between floors it walks
        straight to the nearest vertical link of the requested type on each floor; without such a link on
        both floors nothing can be measured and the legs keep an unknown length.
        """
        pa, pb = self.entity_xy(a), self.entity_xy(b)
        floor_a, floor_b = a.floor or "", b.floor or ""
        if floor_a == floor_b:
            return Route([RouteLeg(floor_a, [pa, pb], round(math.dist(pa, pb), 1))], straight=True)
        best: tuple[float, str, tuple[float, float], tuple[float, float]] | None = None
        for link_id, by_floor in sorted(self.link_floors(via).items()):
            if floor_a not in by_floor or floor_b not in by_floor:
                continue
            near_a = self._access_xy(floor_a, by_floor[floor_a], pa)
            near_b = self._access_xy(floor_b, by_floor[floor_b], pb)
            if near_a is None or near_b is None:
                continue
            total = math.dist(pa, near_a) + math.dist(near_b, pb)
            if best is None or total < best[0] - 1e-9:
                best = (total, link_id, near_a, near_b)
        if best is None:
            return Route([RouteLeg(floor_a, [pa], None), RouteLeg(floor_b, [pb], None)], straight=True)
        _, link_id, near_a, near_b = best
        return Route(
            [
                RouteLeg(floor_a, _simplify([pa, near_a]), round(math.dist(pa, near_a), 1)),
                RouteLeg(floor_b, _simplify([near_b, pb]), round(math.dist(near_b, pb), 1)),
            ],
            via=link_id,
            straight=True,
        )

    def _access_xy(
        self, floor: str, entity_id: str, towards: tuple[float, float]
    ) -> tuple[float, float] | None:
        """Access point of an entity that lies closest to ``towards`` (its centroid as a last resort)."""
        points = self.network(floor).all_access_points.get(entity_id)
        if not points:
            entity = self.model.entities.get(entity_id)
            if entity is None or entity.geometry.is_empty:
                return None
            centroid = entity.geometry.centroid
            return round(float(centroid.x), 2), round(float(centroid.y), 2)
        point = min(points, key=lambda p: math.dist((p.x, p.y), towards))
        return round(float(point.x), 2), round(float(point.y), 2)

    def route(self, unit_a: str, unit_b: str) -> Route:
        """Route from the entrance of ``unit_a`` to the entrance of ``unit_b``."""
        return self.route_between(self._units[unit_a], self._units[unit_b])

    def route_between(self, a: Entity, b: Entity, via: str | None = None) -> Route:
        """Route between the access points of two entities, changing floors by the ``via`` link type."""
        floor_a, floor_b = a.floor or "", b.floor or ""
        from_a = self._search(floor_a, a.entity_id)
        if floor_a == floor_b:
            target = self._nearest(from_a, floor_b, b.entity_id)
            if target is None:
                return self._straight(a, b, via)
            distance, node = target
            return Route([self._leg(floor_a, from_a[1][node], distance)])
        from_b = self._search(floor_b, b.entity_id)
        best: tuple[float, str, str, float, str, float] | None = None
        for link_id, by_floor in sorted(self.link_floors(via).items()):
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
            return self._straight(a, b, via)
        _, link_id, node_a, dist_a, node_b, dist_b = best
        leg_a = self._leg(floor_a, from_a[1][node_a], dist_a)
        leg_b = self._leg(floor_b, list(reversed(from_b[1][node_b])), dist_b)
        return Route([leg_a, leg_b], via=link_id)
