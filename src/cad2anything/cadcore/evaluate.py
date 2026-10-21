"""Parse quality against ground truth (synthetic drawings): units, brands, fixtures, links, amenities,
openings, walkways and zones."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from shapely.ops import unary_union
from shapely.strtree import STRtree

from cad2anything.cadcore.geometry import iou
from cad2anything.model import Entity, EntityKind, SpatialModel


def _as_model(model: SpatialModel | Mapping[str, Any]) -> SpatialModel:
    return model if isinstance(model, SpatialModel) else SpatialModel.from_dict(dict(model))


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _name(value: Any) -> str | None:
    return " ".join(str(value).split()) if value else None


def _units(model: SpatialModel) -> dict[str, Entity]:
    return {str(u.attributes.get("unit_code")): u for u in model.units()}


def _fixture_match(parsed: SpatialModel, truth: SpatialModel, tol: float) -> tuple[int, int, int]:
    """(truth fixtures, matched by type and centroid within ``tol``, matched with identical fixture_id)."""
    total = matched = same_id = 0
    floors = {e.floor for e in truth.by_kind(EntityKind.FIXTURE)}
    for floor in floors:
        truth_fx = truth.by_kind(EntityKind.FIXTURE, floor)
        parsed_fx = parsed.by_kind(EntityKind.FIXTURE, floor)
        total += len(truth_fx)
        if not parsed_fx:
            continue
        centroids = [f.geometry.centroid for f in parsed_fx]
        tree = STRtree(centroids)
        taken: set[int] = set()
        for fixture in truth_fx:
            center = fixture.geometry.centroid
            options = sorted(
                (centroids[int(i)].distance(center), int(i))
                for i in tree.query(center.buffer(tol))
                if int(i) not in taken
                and parsed_fx[int(i)].attributes.get("fixture_type") == fixture.attributes.get("fixture_type")
            )
            if options and options[0][0] <= tol:
                index = options[0][1]
                taken.add(index)
                matched += 1
                same_id += parsed_fx[index].attributes.get("fixture_id") == fixture.attributes.get(
                    "fixture_id"
                )
    return total, matched, same_id


def _geometry_recall(
    parsed: list[Entity], truth: list[Entity], key: str, min_iou: float = 0.9
) -> tuple[int, int]:
    """(truth count, matched) where a match has the same floor, the same ``key`` attribute and IoU >= min."""
    pool: dict[tuple[str | None, Any], list[Entity]] = defaultdict(list)
    for entity in parsed:
        pool[(entity.floor, entity.attributes.get(key))].append(entity)
    matched = 0
    for entity in truth:
        candidates = pool.get((entity.floor, entity.attributes.get(key)), [])
        best = max(candidates, key=lambda c: iou(c.geometry, entity.geometry), default=None)
        if best is not None and iou(best.geometry, entity.geometry) >= min_iou:
            candidates.remove(best)
            matched += 1
    return len(truth), matched


def compare_with_truth(
    parsed: SpatialModel | Mapping[str, Any],
    truth: SpatialModel | Mapping[str, Any],
    fixture_tol_m: float = 0.2,
) -> dict[str, Any]:
    """Quality metrics of a parsed model against the truth model of the same revision."""
    parsed, truth = _as_model(parsed), _as_model(truth)
    p_units, t_units = _units(parsed), _units(truth)
    codes = sorted(set(p_units) & set(t_units))
    area_errors = [
        abs(p_units[c].geometry.area - t_units[c].geometry.area) / t_units[c].geometry.area for c in codes
    ]
    ious = [iou(p_units[c].geometry, t_units[c].geometry) for c in codes]
    occupied = [c for c in codes if not t_units[c].attributes.get("vacant")]
    brand_ok = sum(
        _name(p_units[c].attributes.get("brand_name")) == _name(t_units[c].attributes.get("brand_name"))
        for c in occupied
    )
    vacancy_ok = sum(
        p_units[c].attributes.get("vacant") is not None
        and bool(p_units[c].attributes.get("vacant")) == bool(t_units[c].attributes.get("vacant"))
        for c in codes
    )
    with_ids = [c for c in occupied if p_units[c].attributes.get("brand_id")]
    brand_id_ok = sum(
        p_units[c].attributes.get("brand_id") == t_units[c].attributes.get("brand_id") for c in with_ids
    )
    category_ok = sum(
        p_units[c].attributes.get("category") == t_units[c].attributes.get("category") for c in codes
    )
    zone_ok = sum(
        p_units[c].attributes.get("zone_code") == t_units[c].attributes.get("zone_code") for c in codes
    )

    p_fx = Counter(str(f.attributes.get("unit_code")) for f in parsed.by_kind(EntityKind.FIXTURE))
    t_fx = Counter(str(f.attributes.get("unit_code")) for f in truth.by_kind(EntityKind.FIXTURE))
    count_errors = [abs(p_fx.get(c, 0) - t_fx.get(c, 0)) for c in codes]
    fx_total, fx_matched, fx_same_id = _fixture_match(parsed, truth, fixture_tol_m)

    def keyed(model: SpatialModel, kind: EntityKind, attr: str) -> set[tuple[str | None, Any]]:
        return {(e.floor, e.attributes.get(attr)) for e in model.by_kind(kind)}

    t_links, p_links = (
        keyed(truth, EntityKind.VERTICAL_LINK, "link_id"),
        keyed(parsed, EntityKind.VERTICAL_LINK, "link_id"),
    )
    links_per_floor = Counter(e.floor for e in parsed.by_kind(EntityKind.VERTICAL_LINK))
    link_total, link_geo = _geometry_recall(
        parsed.by_kind(EntityKind.VERTICAL_LINK), truth.by_kind(EntityKind.VERTICAL_LINK), "link_id"
    )
    amenity_total, amenity_matched = _geometry_recall(
        parsed.by_kind(EntityKind.AMENITY), truth.by_kind(EntityKind.AMENITY), "amenity_type"
    )
    t_open, p_open = (
        keyed(truth, EntityKind.OPENING, "opening_id"),
        keyed(parsed, EntityKind.OPENING, "opening_id"),
    )
    t_zones, p_zones = keyed(truth, EntityKind.ZONE, "zone_code"), keyed(parsed, EntityKind.ZONE, "zone_code")
    walkway_iou: dict[str, float] = {}
    for floor in sorted({e.floor for e in truth.by_kind(EntityKind.WALKWAY) if e.floor}):
        t_geom = unary_union([w.geometry for w in truth.by_kind(EntityKind.WALKWAY, floor)])
        p_geom = unary_union([w.geometry for w in parsed.by_kind(EntityKind.WALKWAY, floor)])
        walkway_iou[floor] = round(iou(t_geom, p_geom), 4) if not p_geom.is_empty else 0.0
    return {
        "units": {
            "truth": len(t_units),
            "parsed": len(p_units),
            "matched": len(codes),
            "precision": _ratio(len(codes), len(p_units)),
            "recall": _ratio(len(codes), len(t_units)),
            "missing": sorted(set(t_units) - set(p_units))[:20],
            "extra": sorted(set(p_units) - set(t_units))[:20],
            "area_rel_error_median": round(statistics.median(area_errors), 6) if area_errors else None,
            "area_rel_error_max": round(max(area_errors), 6) if area_errors else None,
            "iou_median": round(statistics.median(ious), 4) if ious else None,
            "iou_min": round(min(ious), 4) if ious else None,
        },
        "brands": {
            "brand_name_accuracy": _ratio(brand_ok, len(occupied)),
            "vacancy_accuracy": _ratio(vacancy_ok, len(codes)),
            "brand_id_coverage": _ratio(len(with_ids), len(occupied)),
            "brand_id_accuracy": _ratio(brand_id_ok, len(with_ids)),
            "category_accuracy": _ratio(category_ok, len(codes)),
            "zone_code_accuracy": _ratio(zone_ok, len(codes)),
        },
        "fixtures": {
            "truth": fx_total,
            "parsed": len(parsed.by_kind(EntityKind.FIXTURE)),
            "matched_rate": _ratio(fx_matched, fx_total),
            "fixture_id_accuracy": _ratio(fx_same_id, fx_matched),
            "count_abs_error_mean_per_unit": round(statistics.mean(count_errors), 4)
            if count_errors
            else None,
            "count_abs_error_max_per_unit": max(count_errors) if count_errors else None,
            "units_with_exact_count": _ratio(sum(e == 0 for e in count_errors), len(count_errors)),
        },
        "vertical_links": {
            "truth": len(t_links),
            "recall": _ratio(len(t_links & p_links), len(t_links)),
            "geometry_recall": _ratio(link_geo, link_total),
            "per_floor": dict(sorted(links_per_floor.items())),
        },
        "amenities": {"truth": amenity_total, "recall": _ratio(amenity_matched, amenity_total)},
        "openings": {"truth": len(t_open), "recall": _ratio(len(t_open & p_open), len(t_open))},
        "zones": {"truth": len(t_zones), "recall": _ratio(len(t_zones & p_zones), len(t_zones))},
        "walkways": {
            "union_iou_min": min(walkway_iou.values()) if walkway_iou else None,
            "union_iou_per_floor": walkway_iou,
        },
    }
