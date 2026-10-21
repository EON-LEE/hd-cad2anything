"""Core data contracts shared by every module.

Geometry uses a local engineering coordinate system in meters (SRID 0). The origin is the
south-west corner of the building and is shared by all floors, so floors stack vertically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry


class EntityKind(StrEnum):
    FLOOR = "floor"
    EXTERIOR = "exterior"
    ZONE = "zone"
    UNIT = "unit"
    FIXTURE = "fixture"
    WALKWAY = "walkway"
    VERTICAL_LINK = "vertical_link"
    OPENING = "opening"
    AMENITY = "amenity"
    WALL = "wall"


class RelationType(StrEnum):
    CONTAINS = "contains"
    ADJACENT_TO = "adjacent_to"
    CONNECTED_TO = "connected_to"
    FACES = "faces"
    NEAR = "near"
    ON_FLOOR = "on_floor"


class ChangeType(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MOVE = "move"
    RESIZE = "resize"
    REASSIGN = "reassign"
    SPLIT = "split"
    MERGE = "merge"
    FIXTURE_CHANGE = "fixture_change"
    WALKWAY_CHANGE = "walkway_change"


class RevisionKind(StrEnum):
    ACTUAL = "actual"
    HYPOTHETICAL = "hypothetical"


@dataclass
class Revision:
    rev_id: str
    kind: RevisionKind = RevisionKind.ACTUAL
    parent_rev_id: str | None = None
    effective_date: str | None = None
    source: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rev_id": self.rev_id,
            "kind": str(self.kind),
            "parent_rev_id": self.parent_rev_id,
            "effective_date": self.effective_date,
            "source": self.source,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Revision:
        return cls(
            rev_id=data["rev_id"],
            kind=RevisionKind(data.get("kind", "actual")),
            parent_rev_id=data.get("parent_rev_id"),
            effective_date=data.get("effective_date"),
            source=data.get("source"),
            notes=data.get("notes", ""),
        )


@dataclass
class Entity:
    """A spatial entity.

    ``entity_id`` is stable across revisions when the entity is the same physical space or object.
    Common attributes by kind:

    - unit: ``unit_code``, ``brand_id``, ``brand_name``, ``category``, ``area_m2``, ``frontage_m``,
      ``zone_code``, ``vacant``
    - fixture: ``fixture_id``, ``fixture_type``, ``unit_code``, ``width_m``, ``depth_m``
    - vertical_link: ``link_id`` (same across floors), ``link_type`` (escalator, elevator, stair)
    - opening: ``opening_id``, ``opening_type`` (entrance, door)
    - amenity: ``amenity_type``
    - zone: ``zone_code``, ``zone_name``, ``category``
    """

    entity_id: str
    kind: EntityKind
    floor: str | None
    geometry: BaseGeometry
    label: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": str(self.kind),
            "floor": self.floor,
            "geometry_wkt": self.geometry.wkt,
            "label": self.label,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        return cls(
            entity_id=data["entity_id"],
            kind=EntityKind(data["kind"]),
            floor=data.get("floor"),
            geometry=shapely_wkt.loads(data["geometry_wkt"]),
            label=data.get("label"),
            attributes=dict(data.get("attributes") or {}),
        )


@dataclass
class Relation:
    from_id: str
    to_id: str
    type: RelationType
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"from_id": self.from_id, "to_id": self.to_id, "type": str(self.type), "metrics": self.metrics}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Relation:
        return cls(
            from_id=data["from_id"],
            to_id=data["to_id"],
            type=RelationType(data["type"]),
            metrics=dict(data.get("metrics") or {}),
        )


@dataclass
class Floor:
    code: str
    ordinal: int
    name: str = ""
    elevation_m: float = 0.0
    height_m: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ordinal": self.ordinal,
            "name": self.name,
            "elevation_m": self.elevation_m,
            "height_m": self.height_m,
        }


@dataclass
class SpatialModel:
    revision: Revision
    floors: list[Floor] = field(default_factory=list)
    entities: dict[str, Entity] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add(self, entity: Entity) -> None:
        if entity.entity_id in self.entities:
            raise ValueError(f"duplicate entity_id: {entity.entity_id}")
        self.entities[entity.entity_id] = entity

    def by_kind(self, kind: EntityKind, floor: str | None = None) -> list[Entity]:
        return [
            e for e in self.entities.values() if e.kind == kind and (floor is None or e.floor == floor)
        ]

    def units(self, floor: str | None = None) -> list[Entity]:
        return self.by_kind(EntityKind.UNIT, floor)

    def unit_by_code(self, unit_code: str) -> Entity | None:
        for e in self.entities.values():
            if e.kind == EntityKind.UNIT and e.attributes.get("unit_code") == unit_code:
                return e
        return None

    def relations_of(self, entity_id: str, rel_type: RelationType | None = None) -> list[Relation]:
        return [
            r
            for r in self.relations
            if (r.from_id == entity_id or r.to_id == entity_id) and (rel_type is None or r.type == rel_type)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision.to_dict(),
            "floors": [f.to_dict() for f in self.floors],
            "entities": [e.to_dict() for e in self.entities.values()],
            "relations": [r.to_dict() for r in self.relations],
            "issues": self.issues,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpatialModel:
        model = cls(
            revision=Revision.from_dict(data["revision"]),
            floors=[Floor(**f) for f in data.get("floors", [])],
            relations=[Relation.from_dict(r) for r in data.get("relations", [])],
            issues=list(data.get("issues", [])),
        )
        for e in data.get("entities", []):
            model.add(Entity.from_dict(e))
        return model


@dataclass
class ChangeItem:
    change_type: ChangeType
    floor: str
    unit_codes_before: list[str] = field(default_factory=list)
    unit_codes_after: list[str] = field(default_factory=list)
    brand_ids_before: list[str] = field(default_factory=list)
    brand_ids_after: list[str] = field(default_factory=list)
    entity_ids_before: list[str] = field(default_factory=list)
    entity_ids_after: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": str(self.change_type),
            "floor": self.floor,
            "unit_codes_before": self.unit_codes_before,
            "unit_codes_after": self.unit_codes_after,
            "brand_ids_before": self.brand_ids_before,
            "brand_ids_after": self.brand_ids_after,
            "entity_ids_before": self.entity_ids_before,
            "entity_ids_after": self.entity_ids_after,
            "details": self.details,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeItem:
        return cls(
            change_type=ChangeType(data["change_type"]),
            floor=data["floor"],
            unit_codes_before=list(data.get("unit_codes_before", [])),
            unit_codes_after=list(data.get("unit_codes_after", [])),
            brand_ids_before=list(data.get("brand_ids_before", [])),
            brand_ids_after=list(data.get("brand_ids_after", [])),
            entity_ids_before=list(data.get("entity_ids_before", [])),
            entity_ids_after=list(data.get("entity_ids_after", [])),
            details=dict(data.get("details") or {}),
            summary=data.get("summary", ""),
        )


@dataclass
class ChangeSet:
    rev_from: str
    rev_to: str
    items: list[ChangeItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"rev_from": self.rev_from, "rev_to": self.rev_to, "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeSet:
        return cls(
            rev_from=data["rev_from"],
            rev_to=data["rev_to"],
            items=[ChangeItem.from_dict(i) for i in data.get("items", [])],
        )
