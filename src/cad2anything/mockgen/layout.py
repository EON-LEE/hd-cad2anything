"""R0 truth layout (``SpatialModel``) of the synthetic flagship store.

Floor template in meters (origin at the south-west corner; identical on every floor):

- Exterior 0..120 x 0..80. Tenant bands 12 m deep: S (x 18..102, y 0..12), N (x 18..102, y 68..80),
  W (x 0..12, y 18..62) and E (x 108..120, y 18..62).
- Walkway segments (simple polygons, connected union): a 6 m ring corridor (outer 12..108 x 12..68),
  the central corridor (y 37..43), cross corridors (x 42..48 and 72..78), the atrium (x 48..72, y 30..50)
  around escalators ESC-A (x 51..55) and ESC-B (x 65..69), and a lobby in each corner core.
- Islands (band ``C``): SW/SE/NW/NE 24 x 19 m and SC/NC 24 x 12 m, sliced as one full-depth row or as
  two rows so that every unit fronts a corridor.
- Corner cores: ST-1/EV-1 (SW) and ST-2/EV-2 (NE) with restrooms; NW/SE corners hold floor-specific
  amenities or back-of-house space. 1F adds entrance halls (ENT-1..3) and B1 a subway passage (ENT-B1).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from cad2anything.catalog import (
    AMENITY_TYPES,
    BUILDING_DEPTH_M,
    BUILDING_WIDTH_M,
    CATEGORIES,
    FIXTURE_TYPES,
    FLOOR_CODES,
    FLOOR_HEIGHT_M,
    FLOOR_PROGRAMS,
    VERTICAL_LINKS,
    floor_program,
)
from cad2anything.model import (
    Entity,
    EntityKind,
    Floor,
    Relation,
    RelationType,
    Revision,
    RevisionKind,
    SpatialModel,
)

if TYPE_CHECKING:
    from cad2anything.mockgen.brands import Brand

GRID_M = 0.1
MIN_UNIT_WIDTH_M = 3.0
FIXTURE_CLEARANCE_M = 1.0
FIXTURE_GAP_M = 1.2
_EPS = 1e-6

REVISION_DATES: dict[str, str | None] = {
    "R0": "2024-10-01",
    "R1": "2025-03-01",
    "R2": "2025-09-01",
    "H1": None,
}

# Planted low-efficiency zone: 4F east band stays low-exposure and unchanged in every revision.
PLANTED_LOW_EFFICIENCY: dict[str, str] = {
    "floor": "4F",
    "band": "E",
    "note": "4F 동측 코너 저노출 존(개정 간 불변)",
}

FIXTURE_DENSITY_M2: dict[str, float] = {
    "LUXURY": 20, "JEWELRY_WATCH": 14, "COSMETICS": 12, "GLOBAL_FASHION": 17, "ACCESSORIES": 13,
    "WOMEN_CONTEMPORARY": 16, "WOMEN_CASUAL": 15, "LINGERIE": 13, "MEN_FORMAL": 17, "MEN_CASUAL": 16,
    "SPORTS": 17, "OUTDOOR": 17, "KIDS": 14, "LIVING": 15, "ELECTRONICS": 18, "FOOD_FRESH": 12,
    "FOOD_DELI": 12, "BAKERY": 12, "WINE": 13, "FOOD_COURT": 12, "CAFE": 12, "RESTAURANT": 13, "CULTURE": 20,
}  # fmt: skip


def rect_poly(x0: float, y0: float, x1: float, y1: float) -> Polygon:
    return box(round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3))


def round_rect(rect: Iterable[float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (round(float(v), 3) for v in rect)
    return (x0, y0, x1, y1)


def rect_of(entity: Entity) -> tuple[float, float, float, float]:
    return round_rect(entity.geometry.bounds)


def stable_rng(*parts: Any) -> np.random.Generator:
    """Generator seeded from a stable hash of ``parts`` (independent of PYTHONHASHSEED)."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


# --- static template ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkwaySpec:
    key: str
    walkway_type: str
    name_ko: str
    geometry: Polygon


BASE_WALKWAYS: tuple[WalkwaySpec, ...] = (
    WalkwaySpec("S", "main", "남측 주통로", rect_poly(12, 12, 108, 18)),
    WalkwaySpec("N", "main", "북측 주통로", rect_poly(12, 62, 108, 68)),
    WalkwaySpec("W", "main", "서측 주통로", rect_poly(12, 18, 18, 62)),
    WalkwaySpec("E", "main", "동측 주통로", rect_poly(102, 18, 108, 62)),
    WalkwaySpec("CW", "central", "중앙 통로(서)", rect_poly(18, 37, 42, 43)),
    WalkwaySpec("CE", "central", "중앙 통로(동)", rect_poly(78, 37, 102, 43)),
    WalkwaySpec("XW", "cross", "서측 횡단 통로", rect_poly(42, 18, 48, 62)),
    WalkwaySpec("XE", "cross", "동측 횡단 통로", rect_poly(72, 18, 78, 62)),
    WalkwaySpec("AS", "atrium", "아트리움 남측", rect_poly(48, 30, 72, 33)),
    WalkwaySpec("AN", "atrium", "아트리움 북측", rect_poly(48, 47, 72, 50)),
    WalkwaySpec("AW", "atrium", "아트리움 서측", rect_poly(48, 33, 51, 47)),
    WalkwaySpec("AM", "atrium", "아트리움 중앙", rect_poly(55, 33, 65, 47)),
    WalkwaySpec("AE", "atrium", "아트리움 동측", rect_poly(69, 33, 72, 47)),
    WalkwaySpec("LSW", "lobby", "남서 코어 로비", rect_poly(6, 6, 12, 18)),
    WalkwaySpec("LNW", "lobby", "북서 코어 로비", rect_poly(6, 62, 12, 74)),
    WalkwaySpec("LNE", "lobby", "북동 코어 로비", rect_poly(108, 62, 114, 74)),
    WalkwaySpec("LSE", "lobby", "남동 코어 로비", rect_poly(108, 6, 114, 18)),
)
FLOOR_WALKWAYS: dict[str, tuple[WalkwaySpec, ...]] = {
    "1F": (
        WalkwaySpec(
            "ENT1", "entrance", "정문 홀", Polygon([(54, 0), (66, 0), (66, 9), (62, 9), (62, 12), (54, 12)])
        ),
        WalkwaySpec("ENT2", "entrance", "서측 출입 통로", rect_poly(0, 37, 12, 43)),
        WalkwaySpec("ENT3", "entrance", "동측 출입 통로", rect_poly(108, 37, 120, 43)),
    ),
    "B1": (WalkwaySpec("SUB", "entrance", "지하철 연결통로", rect_poly(0, 37, 12, 43)),),
}

VERTICAL_LINK_GEOMS: dict[str, Polygon] = {
    "ESC-A": rect_poly(51, 33, 55, 47),
    "ESC-B": rect_poly(65, 33, 69, 47),
    "EV-1": rect_poly(0, 6, 6, 18),
    "EV-2": rect_poly(114, 62, 120, 74),
    "ST-1": rect_poly(12, 0, 18, 12),
    "ST-2": rect_poly(102, 68, 108, 80),
}
LINK_TYPE_NAMES_KO: dict[str, str] = {"escalator": "에스컬레이터", "elevator": "엘리베이터", "stair": "계단"}
ESCALATORS = unary_union([VERTICAL_LINK_GEOMS["ESC-A"], VERTICAL_LINK_GEOMS["ESC-B"]])
# Points just across the atrium strip from each escalator's south landing ("directly in front").
ESCALATOR_FRONT_POINTS: dict[str, tuple[float, float]] = {"ESC-A": (53.0, 29.0), "ESC-B": (67.0, 29.0)}

AMENITY_LABEL_CODES: dict[str, str] = {
    "RESTROOM": "WC",
    "INFO": "INFO",
    "ATM": "ATM",
    "NURSING": "NURSING",
    "LOUNGE": "LOUNGE",
}
BASE_AMENITIES: tuple[tuple[str, Polygon], ...] = (
    ("RESTROOM", rect_poly(0, 0, 12, 6)),
    ("RESTROOM", rect_poly(108, 74, 120, 80)),
)
FLOOR_AMENITIES: dict[str, tuple[tuple[str, Polygon], ...]] = {
    "B1": (("ATM", rect_poly(12, 68, 18, 72)),),
    "1F": (("INFO", rect_poly(62, 9, 66, 12)), ("ATM", rect_poly(12, 68, 18, 72))),
    "2F": (("LOUNGE", rect_poly(12, 68, 18, 80)),),
    "6F": (("NURSING", rect_poly(12, 68, 18, 80)),),
}
FLOOR_OPENINGS: dict[str, tuple[tuple[str, str, tuple[tuple[float, float], ...]], ...]] = {
    "1F": (
        ("ENT-1", "정문", ((56.0, 0.0), (64.0, 0.0))),
        ("ENT-2", "서측 출입구", ((0.0, 38.0), (0.0, 42.0))),
        ("ENT-3", "동측 출입구", ((120.0, 38.0), (120.0, 42.0))),
    ),
    "B1": (("ENT-B1", "지하철 연결통로", ((0.0, 38.0), (0.0, 42.0))),),
}


def amenity_label(amenity_type: str) -> str:
    code, name = AMENITY_LABEL_CODES[amenity_type], AMENITY_TYPES[amenity_type]
    return code if code == name else f"{code} {name}"


# --- tenant strips and floor programs ------------------------------------------------------------


@dataclass(frozen=True)
class Strip:
    """A rectangle sliced into units along ``axis``; ``facing`` points to the storefront corridor."""

    key: str
    band: str
    rect: tuple[float, float, float, float]
    axis: str
    facing: str

    @property
    def length(self) -> float:
        x0, y0, x1, y1 = self.rect
        return x1 - x0 if self.axis == "x" else y1 - y0

    @property
    def depth(self) -> float:
        x0, y0, x1, y1 = self.rect
        return y1 - y0 if self.axis == "x" else x1 - x0

    def sub_rect(self, a: float, b: float) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.rect
        return round_rect((x0 + a, y0, x0 + b, y1) if self.axis == "x" else (x0, y0 + a, x1, y0 + b))


BAND_STRIPS: dict[str, Strip] = {
    s.key: s
    for s in (
        Strip("S", "S", (18, 0, 102, 12), "x", "N"),
        Strip("S1", "S", (18, 0, 54, 12), "x", "N"),
        Strip("S2", "S", (66, 0, 102, 12), "x", "N"),
        Strip("N", "N", (18, 68, 102, 80), "x", "S"),
        Strip("W", "W", (0, 18, 12, 62), "y", "E"),
        Strip("W1", "W", (0, 18, 12, 37), "y", "E"),
        Strip("W2", "W", (0, 43, 12, 62), "y", "E"),
        Strip("E", "E", (108, 18, 120, 62), "y", "W"),
        Strip("E1", "E", (108, 18, 120, 37), "y", "W"),
        Strip("E2", "E", (108, 43, 120, 62), "y", "W"),
    )
}
ISLANDS: dict[str, tuple[tuple[float, float, float, float], str]] = {
    "ISW": ((18, 18, 42, 37), "N"),
    "ISC": ((48, 18, 72, 30), "N"),
    "ISE": ((78, 18, 102, 37), "N"),
    "INW": ((18, 43, 42, 62), "S"),
    "INC": ((48, 50, 72, 62), "S"),
    "INE": ((78, 43, 102, 62), "S"),
}
STRIP_ORDER: tuple[str, ...] = (
    "S", "S1", "S2", "N", "W", "W1", "W2", "E", "E1", "E2", "ISW", "ISC", "ISE", "INW", "INC", "INE",
)  # fmt: skip


def island_strips(key: str, mode: str) -> list[Strip]:
    (x0, y0, x1, y1), facing = ISLANDS[key]
    if mode == "full":
        return [Strip(key, "C", (x0, y0, x1, y1), "x", facing)]
    ym = round((y0 + y1) / 2, 3)
    return [
        Strip(f"{key}.L", "C", (x0, y0, x1, ym), "x", "S"),
        Strip(f"{key}.U", "C", (x0, ym, x1, y1), "x", "N"),
    ]


Segment = tuple[str, float]
PlanEntry = tuple[str, str, tuple[Segment, ...]]


def _band(key: str, *segments: Segment) -> PlanEntry:
    return (key, "band", segments)


def _full(key: str, *segments: Segment) -> PlanEntry:
    return (key, "full", segments)


def _rows(key: str, *segments: Segment) -> PlanEntry:
    return (key, "rows", segments)


# Area shares follow catalog.FLOOR_PROGRAMS within a few percent; see docs in the module docstring.
FLOOR_PLANS: dict[str, tuple[PlanEntry, ...]] = {
    "B1": (
        _band("S", ("FOOD_FRESH", 1.0)),
        _band("N", ("FOOD_FRESH", 0.6), ("FOOD_DELI", 0.4)),
        _band("W1", ("BAKERY", 1.0)),
        _band("W2", ("BAKERY", 1.0)),
        _band("E", ("WINE", 1.0)),
        _rows("ISW", ("FOOD_DELI", 1.0)),
        _rows("ISC", ("BAKERY", 1.0)),
        _rows("ISE", ("CAFE", 1.0)),
        _rows("INW", ("FOOD_DELI", 1.0)),
        _rows("INC", ("FOOD_COURT", 1.0)),
        _rows("INE", ("FOOD_COURT", 1.0)),
    ),
    "1F": (
        _band("S1", ("LUXURY", 1.0)),
        _band("S2", ("LUXURY", 1.0)),
        _band("N", ("LUXURY", 0.7), ("COSMETICS", 0.3)),
        _band("W1", ("COSMETICS", 1.0)),
        _band("W2", ("COSMETICS", 1.0)),
        _band("E1", ("JEWELRY_WATCH", 1.0)),
        _band("E2", ("COSMETICS", 1.0)),
        _rows("ISW", ("COSMETICS", 1.0)),
        _full("ISC", ("JEWELRY_WATCH", 1.0)),
        _rows("ISE", ("COSMETICS", 1.0)),
        _rows("INW", ("COSMETICS", 1.0)),
        _full("INC", ("JEWELRY_WATCH", 1.0)),
        _rows("INE", ("COSMETICS", 1.0)),
    ),
    "2F": (
        _band("S", ("GLOBAL_FASHION", 1.0)),
        _band("N", ("ACCESSORIES", 0.2), ("LUXURY", 0.3), ("GLOBAL_FASHION", 0.5)),
        _band("W", ("GLOBAL_FASHION", 1.0)),
        _band("E", ("GLOBAL_FASHION", 1.0)),
        _full("ISW", ("GLOBAL_FASHION", 1.0)),
        _full("ISC", ("LUXURY", 1.0)),
        _rows("ISE", ("ACCESSORIES", 1.0)),
        _rows("INW", ("ACCESSORIES", 1.0)),
        _full("INC", ("LUXURY", 1.0)),
        _rows("INE", ("ACCESSORIES", 1.0)),
    ),
    "3F": (
        _band("S", ("WOMEN_CONTEMPORARY", 1.0)),
        _band("N", ("WOMEN_CONTEMPORARY", 1.0)),
        _band("W", ("WOMEN_CONTEMPORARY", 1.0)),
        _band("E", ("WOMEN_CONTEMPORARY", 1.0)),
        _rows("ISW", ("WOMEN_CONTEMPORARY", 1.0)),
        _full("ISC", ("WOMEN_CONTEMPORARY", 1.0)),
        _rows("ISE", ("ACCESSORIES", 1.0)),
        _rows("INW", ("WOMEN_CONTEMPORARY", 1.0)),
        _full("INC", ("CAFE", 1.0)),
        _rows("INE", ("WOMEN_CONTEMPORARY", 1.0)),
    ),
    "4F": (
        _band("S", ("WOMEN_CASUAL", 1.0)),
        _band("N", ("WOMEN_CASUAL", 1.0)),
        _band("W", ("WOMEN_CASUAL", 1.0)),
        _band("E", ("LINGERIE", 1.0)),
        _rows("ISW", ("WOMEN_CASUAL", 1.0)),
        _full("ISC", ("ACCESSORIES", 1.0)),
        _rows("ISE", ("WOMEN_CASUAL", 1.0)),
        _rows("INW", ("WOMEN_CASUAL", 1.0)),
        _full("INC", ("ACCESSORIES", 1.0)),
        _rows("INE", ("LINGERIE", 1.0)),
    ),
    "5F": (
        _band("S", ("MEN_FORMAL", 1.0)),
        _band("N", ("MEN_CASUAL", 1.0)),
        _band("W", ("MEN_FORMAL", 1.0)),
        _band("E", ("MEN_CASUAL", 1.0)),
        _rows("ISW", ("MEN_FORMAL", 1.0)),
        _full("ISC", ("CAFE", 1.0)),
        _rows("ISE", ("MEN_CASUAL", 1.0)),
        _rows("INW", ("MEN_FORMAL", 1.0)),
        _full("INC", ("CAFE", 1.0)),
        _rows("INE", ("MEN_CASUAL", 1.0)),
    ),
    "6F": (
        _band("S", ("SPORTS", 1.0)),
        _band("N", ("OUTDOOR", 1.0)),
        _band("W", ("OUTDOOR", 1.0)),
        _band("E", ("KIDS", 1.0)),
        _full("ISW", ("SPORTS", 1.0)),
        _full("ISC", ("SPORTS", 1.0)),
        _full("ISE", ("SPORTS", 1.0)),
        _rows("INW", ("KIDS", 1.0)),
        _full("INC", ("KIDS", 1.0)),
        _rows("INE", ("KIDS", 1.0)),
    ),
    "7F": (
        _band("S", ("LIVING", 1.0)),
        _band("N", ("LIVING", 1.0)),
        _band("W", ("LIVING", 1.0)),
        _band("E", ("ELECTRONICS", 1.0)),
        _full("ISW", ("LIVING", 1.0)),
        _full("ISC", ("LIVING", 1.0)),
        _full("ISE", ("ELECTRONICS", 1.0)),
        _full("INW", ("LIVING", 1.0)),
        _full("INC", ("LIVING", 1.0)),
        _full("INE", ("ELECTRONICS", 1.0)),
    ),
    "8F": (
        _band("S", ("RESTAURANT", 1.0)),
        _band("N", ("RESTAURANT", 1.0)),
        _band("W", ("RESTAURANT", 1.0)),
        _band("E", ("RESTAURANT", 1.0)),
        _full("ISW", ("RESTAURANT", 1.0)),
        _full("ISC", ("CAFE", 1.0)),
        _rows("ISE", ("CAFE", 1.0)),
        _full("INW", ("CULTURE", 1.0)),
        _full("INC", ("CAFE", 1.0)),
        _full("INE", ("RESTAURANT", 1.0)),
    ),
}


@dataclass(frozen=True)
class UnitDraft:
    band: str
    rect: tuple[float, float, float, float]
    facing: str
    category: str
    sort_key: tuple[int, int, float]


def _snap_edges(length: float, fractions: list[float]) -> list[tuple[float, float]]:
    total = sum(fractions)
    edges = [0.0]
    acc = 0.0
    for frac in fractions[:-1]:
        acc += frac / total * length
        edges.append(round(round(acc / GRID_M) * GRID_M, 3))
    edges.append(round(length, 3))
    return list(zip(edges[:-1], edges[1:], strict=True))


def _partition(rng: np.random.Generator, length: float, wmin: float, wmax: float) -> list[float]:
    """Relative cut positions ``[0, ..., length]`` with widths in [wmin, wmax] when feasible."""
    wmin = min(wmin, length)
    wmax = max(wmax, wmin)
    n_lo = max(1, math.ceil(length / wmax - 1e-9))
    n_hi = max(1, math.floor(length / wmin + 1e-9))
    if n_lo > n_hi:
        n = n_hi
    else:
        mean_w = min(wmax, (wmin + wmax) / 2 * 1.1)
        n = int(np.clip(round(length / mean_w + rng.uniform(-0.6, 0.6)), n_lo, n_hi))
    if n == 1:
        return [0.0, round(length, 3)]
    weights = rng.uniform(0.3, 1.0, n)
    widths = wmin + (length - n * wmin) * weights / weights.sum()
    for _ in range(50):
        over = np.clip(widths - wmax, 0.0, None)
        if over.sum() < 1e-9:
            break
        widths = np.minimum(widths, wmax)
        room = wmax - widths
        widths = widths + over.sum() * room / room.sum()
    cuts = np.round(np.cumsum(widths)[:-1] / GRID_M) * GRID_M
    return [0.0, *(round(float(c), 3) for c in cuts), round(length, 3)]


def plan_floor_units(floor: str, seed: int) -> list[UnitDraft]:
    """Slice the floor's strips into unit rectangles following ``FLOOR_PLANS``."""
    rng = stable_rng(seed, "units", floor)
    drafts: list[UnitDraft] = []
    for strip_key, mode, segments in FLOOR_PLANS[floor]:
        strips = [BAND_STRIPS[strip_key]] if mode == "band" else island_strips(strip_key, mode)
        for row, strip in enumerate(strips):
            edges = _snap_edges(strip.length, [frac for _, frac in segments])
            for (category, _), (a, b) in zip(segments, edges, strict=True):
                lo, hi = CATEGORIES[category].unit_area_range_m2
                wmin = max(lo / strip.depth, MIN_UNIT_WIDTH_M)
                wmax = max(hi / strip.depth, wmin)
                cuts = _partition(rng, b - a, wmin, wmax)
                for c0, c1 in zip(cuts[:-1], cuts[1:], strict=True):
                    start, end = round(a + c0, 3), round(a + c1, 3)
                    drafts.append(
                        UnitDraft(
                            strip.band,
                            strip.sub_rect(start, end),
                            strip.facing,
                            category,
                            (STRIP_ORDER.index(strip_key), row, start),
                        )
                    )
    return drafts


def code_units(floor: str, drafts: list[UnitDraft]) -> list[tuple[str, UnitDraft]]:
    """Number units per band (``{floor}-{band}{nn}``) in canonical spatial order."""
    by_band: dict[str, list[UnitDraft]] = defaultdict(list)
    for draft in drafts:
        by_band[draft.band].append(draft)
    coded: list[tuple[str, UnitDraft]] = []
    for band in "SNWEC":
        for n, draft in enumerate(sorted(by_band.get(band, []), key=lambda d: d.sort_key), 1):
            coded.append((f"{floor}-{band}{n:02d}", draft))
    return coded


# --- fixtures -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FixturePlacement:
    fixture_type: str
    bounds: tuple[float, float, float, float]
    rotation_deg: float


def _local_dims(rect: tuple[float, float, float, float], facing: str) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    return (x1 - x0, y1 - y0) if facing in ("N", "S") else (y1 - y0, x1 - x0)


def _to_world(
    rect: tuple[float, float, float, float], facing: str, local: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Map a local rectangle (u along the storefront, v from the storefront inward) to world."""
    x0, y0, x1, y1 = rect
    u0, v0, u1, v1 = local
    if facing == "N":
        return round_rect((x0 + u0, y1 - v1, x0 + u1, y1 - v0))
    if facing == "S":
        return round_rect((x0 + u0, y0 + v0, x0 + u1, y0 + v1))
    if facing == "E":
        return round_rect((x1 - v1, y0 + u0, x1 - v0, y0 + u1))
    return round_rect((x0 + v0, y0 + u0, x0 + v1, y0 + u1))


def aabb_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _fixture_dims(fixture_type: str, rotated: bool) -> tuple[float, float]:
    ft = FIXTURE_TYPES[fixture_type]
    return (ft.depth_m, ft.width_m) if rotated else (ft.width_m, ft.depth_m)


def _world_rotation(facing: str, rotated: bool) -> float:
    width_along_x = (facing in ("N", "S")) != rotated
    return 0.0 if width_along_x else 90.0


def plan_fixtures(
    rect: tuple[float, float, float, float], facing: str, category: str, key: str
) -> list[FixturePlacement]:
    """Rule-based fixture plan: counter near the storefront, fitting rooms at the back, grid elsewhere.

    Every fixture keeps ``FIXTURE_CLEARANCE_M`` from the unit edges and ``FIXTURE_GAP_M`` between fixtures.
    """
    types = CATEGORIES[category].fixture_types
    width, depth = _local_dims(rect, facing)
    umin, vmin = FIXTURE_CLEARANCE_M, FIXTURE_CLEARANCE_M
    umax, vmax = width - FIXTURE_CLEARANCE_M, depth - FIXTURE_CLEARANCE_M
    if umax - umin <= 0 or vmax - vmin <= 0:
        return []
    rng = stable_rng(key)
    area = width * depth
    target = max(1, round(area / FIXTURE_DENSITY_M2[category]))
    placed: list[tuple[str, tuple[float, float, float, float], bool]] = []

    def try_place(fixture_type: str, local: tuple[float, float, float, float], rotated: bool) -> bool:
        u0, v0, u1, v1 = local
        if u0 < umin - _EPS or v0 < vmin - _EPS or u1 > umax + _EPS or v1 > vmax + _EPS:
            return False
        if any(aabb_gap(local, other) < FIXTURE_GAP_M - _EPS for _, other, _ in placed):
            return False
        placed.append((fixture_type, local, rotated))
        return True

    if "COUNTER" in types:
        left = bool(rng.random() < 0.5)
        for rotated in (False, True):
            w, d = _fixture_dims("COUNTER", rotated)
            u0 = umin if left else umax - w
            if try_place("COUNTER", (u0, vmin, u0 + w, vmin + d), rotated):
                break
    if "FITTING" in types:
        w, d = _fixture_dims("FITTING", False)
        u1 = umax
        for _ in range(1 if area < 120 else 2 if area < 220 else 3):
            try_place("FITTING", (u1 - w, vmax - d, u1, vmax), False)
            u1 -= w + FIXTURE_GAP_M
    grid_types = [t for t in types if t not in ("COUNTER", "FITTING")]
    remaining = target - len(placed)
    if grid_types and remaining > 0:
        max_w = max(FIXTURE_TYPES[t].width_m for t in grid_types)
        max_d = max(FIXTURE_TYPES[t].depth_m for t in grid_types)
        span_u, span_v = umax - umin, vmax - vmin
        cols_max = max(1, int((span_u + _EPS) // (max_w + FIXTURE_GAP_M)))
        rows_max = max(1, int((span_v + _EPS) // (max_d + FIXTURE_GAP_M)))
        cell = math.sqrt(span_u * span_v / remaining)
        cols = min(cols_max, max(1, round(span_u / cell)))
        rows = min(rows_max, max(1, math.ceil(remaining / cols) + 1))
        pitch_u, pitch_v = span_u / cols, span_v / rows
        offset = int(rng.integers(len(grid_types)))
        count = 0
        for j in range(rows):
            for i in range(cols):
                if count >= remaining:
                    break
                cu, cv = umin + (i + 0.5) * pitch_u, vmin + (j + 0.5) * pitch_v
                done = False
                for k in range(len(grid_types)):
                    fixture_type = grid_types[(i + j + offset + k) % len(grid_types)]
                    for rotated in (False, True):
                        w, d = _fixture_dims(fixture_type, rotated)
                        local = (cu - w / 2, cv - d / 2, cu + w / 2, cv + d / 2)
                        if try_place(fixture_type, local, rotated):
                            done = True
                            break
                    if done:
                        count += 1
                        break
    return [
        FixturePlacement(t, _to_world(rect, facing, local), _world_rotation(facing, rotated))
        for t, local, rotated in placed
    ]


def find_fixture_spot(
    rect: tuple[float, float, float, float],
    facing: str,
    fixture_type: str,
    existing: list[tuple[float, float, float, float]],
) -> FixturePlacement | None:
    """First free spot (front-to-back scan on a 0.2 m grid) for one extra fixture, or None."""
    width, depth = _local_dims(rect, facing)
    for rotated in (False, True):
        w, d = _fixture_dims(fixture_type, rotated)
        v = FIXTURE_CLEARANCE_M
        while v + d <= depth - FIXTURE_CLEARANCE_M + _EPS:
            u = FIXTURE_CLEARANCE_M
            while u + w <= width - FIXTURE_CLEARANCE_M + _EPS:
                world = _to_world(rect, facing, (u, v, u + w, v + d))
                if all(aabb_gap(world, other) >= FIXTURE_GAP_M - _EPS for other in existing):
                    return FixturePlacement(fixture_type, world, _world_rotation(facing, rotated))
                u += 0.2
            v += 0.2
    return None


# --- entity helpers -------------------------------------------------------------------------------


def make_floors() -> list[Floor]:
    return [
        Floor(p.code, p.ordinal, p.name_ko, elevation_m=p.ordinal * FLOOR_HEIGHT_M, height_m=FLOOR_HEIGHT_M)
        for p in FLOOR_PROGRAMS
    ]


def make_unit(
    floor: str,
    unit_code: str,
    rect: tuple[float, float, float, float],
    facing: str,
    band: str,
    category: str,
    brand: Brand | None = None,
) -> Entity:
    geometry = rect_poly(*rect)
    unit = Entity(
        f"U-{unit_code}",
        EntityKind.UNIT,
        floor,
        geometry,
        label=unit_code,
        attributes={
            "unit_code": unit_code,
            "band": band,
            "facing": facing,
            "brand_id": None,
            "brand_name": None,
            "category": category,
            "category_name": CATEGORIES[category].name_ko,
            "area_m2": round(geometry.area, 2),
            "frontage_m": 0.0,
            "vacant": True,
            "zone_code": None,
        },
    )
    set_unit_brand(unit, brand)
    return unit


def set_unit_brand(unit: Entity, brand: Brand | None) -> None:
    attrs = unit.attributes
    attrs["brand_id"] = brand.brand_id if brand else None
    attrs["brand_name"] = brand.brand_name if brand else None
    attrs["vacant"] = brand is None
    if brand is not None:
        attrs["category"] = brand.category
        attrs["category_name"] = CATEGORIES[brand.category].name_ko


def set_unit_rect(unit: Entity, rect: tuple[float, float, float, float]) -> None:
    unit.geometry = rect_poly(*rect)
    unit.attributes["area_m2"] = round(unit.geometry.area, 2)


def fixtures_of(model: SpatialModel, unit_code: str) -> list[Entity]:
    return [
        e
        for e in model.entities.values()
        if e.kind == EntityKind.FIXTURE and e.attributes.get("unit_code") == unit_code
    ]


def fixture_entity(floor: str, unit_code: str, index: int, placement: FixturePlacement) -> Entity:
    fixture_id = f"{unit_code}-F{index:03d}"
    ft = FIXTURE_TYPES[placement.fixture_type]
    return Entity(
        f"F-{fixture_id}",
        EntityKind.FIXTURE,
        floor,
        rect_poly(*placement.bounds),
        label=fixture_id,
        attributes={
            "fixture_id": fixture_id,
            "fixture_type": placement.fixture_type,
            "fixture_name": ft.name_ko,
            "unit_code": unit_code,
            "width_m": ft.width_m,
            "depth_m": ft.depth_m,
            "rotation_deg": placement.rotation_deg,
        },
    )


def refresh_unit_fixtures(model: SpatialModel, unit: Entity, seed: int) -> None:
    """Replace the unit's fixtures with a fresh deterministic plan (none when vacant)."""
    code = unit.attributes["unit_code"]
    for fixture in fixtures_of(model, code):
        del model.entities[fixture.entity_id]
    if unit.attributes["vacant"]:
        return
    rect = rect_of(unit)
    key = f"{seed}|{code}|{unit.attributes['brand_id']}|{rect}"
    for index, placement in enumerate(
        plan_fixtures(rect, unit.attributes["facing"], unit.attributes["category"], key), 1
    ):
        model.add(fixture_entity(unit.floor or "", code, index, placement))


def walkway_entity(floor: str, number: int, spec: WalkwaySpec) -> Entity:
    return Entity(
        f"W-{floor}-{number}",
        EntityKind.WALKWAY,
        floor,
        spec.geometry,
        label=spec.name_ko,
        attributes={"segment": spec.key, "walkway_type": spec.walkway_type, "name_ko": spec.name_ko},
    )


def amenity_entity(floor: str, amenity_type: str, number: int, geometry: Polygon) -> Entity:
    return Entity(
        f"A-{floor}-{amenity_type}-{number}",
        EntityKind.AMENITY,
        floor,
        geometry,
        label=amenity_label(amenity_type),
        attributes={"amenity_type": amenity_type, "name_ko": AMENITY_TYPES[amenity_type]},
    )


def escalator_distance(geometry: BaseGeometry) -> float:
    """Distance (m) from the geometry's centroid to the nearest escalator footprint."""
    return round(float(geometry.centroid.distance(ESCALATORS)), 2)


def unit_at(model: SpatialModel, floor: str, point: tuple[float, float]) -> Entity | None:
    pt = Point(point)
    for unit in model.units(floor):
        if unit.geometry.covers(pt):
            return unit
    return None


def _natural_key(text: str) -> list[Any]:
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", text)]


_KIND_ORDER = {
    k: i
    for i, k in enumerate(
        [
            EntityKind.FLOOR,
            EntityKind.EXTERIOR,
            EntityKind.WALL,
            EntityKind.WALKWAY,
            EntityKind.VERTICAL_LINK,
            EntityKind.AMENITY,
            EntityKind.OPENING,
            EntityKind.ZONE,
            EntityKind.UNIT,
            EntityKind.FIXTURE,
        ]
    )
}
_FLOOR_ORDER = {code: i for i, code in enumerate(FLOOR_CODES)}


def finalize_model(model: SpatialModel) -> None:
    """Recompute zones, derived unit attributes, canonical entity order and base relations."""
    for zone_id in [e.entity_id for e in model.entities.values() if e.kind == EntityKind.ZONE]:
        del model.entities[zone_id]
    zones: list[Entity] = []
    for floor in FLOOR_CODES:
        units = model.units(floor)
        walk = unary_union([w.geometry for w in model.by_kind(EntityKind.WALKWAY, floor)]).buffer(_EPS)
        for unit in units:
            unit.attributes["area_m2"] = round(unit.geometry.area, 2)
            unit.attributes["frontage_m"] = round(unit.geometry.exterior.intersection(walk).length, 2)
        program = list(floor_program(floor).category_mix)
        extra = sorted({u.attributes["category"] for u in units} - set(program))
        for number, category in enumerate(program + extra, 1):
            members = [u for u in units if u.attributes["category"] == category]
            if not members:
                continue
            zone_code = f"{floor}-Z{number}"
            geometry = shapely.set_precision(unary_union([m.geometry for m in members]), 0.001)
            name = CATEGORIES[category].name_ko
            zones.append(
                Entity(
                    f"Z-{zone_code}",
                    EntityKind.ZONE,
                    floor,
                    geometry,
                    label=f"{zone_code} {name}",
                    attributes={
                        "zone_code": zone_code,
                        "zone_name": name,
                        "category": category,
                        "unit_count": len(members),
                        "area_m2": round(geometry.area, 2),
                    },
                )
            )
            for member in members:
                member.attributes["zone_code"] = zone_code
    for zone in zones:
        model.add(zone)
    ordered = sorted(
        model.entities.values(),
        key=lambda e: (_FLOOR_ORDER.get(e.floor or "", -1), _KIND_ORDER[e.kind], _natural_key(e.entity_id)),
    )
    model.entities = {e.entity_id: e for e in ordered}
    relations: list[Relation] = []
    unit_ids = {e.attributes["unit_code"]: e.entity_id for e in ordered if e.kind == EntityKind.UNIT}
    for entity in ordered:
        if entity.kind == EntityKind.UNIT and entity.attributes.get("zone_code"):
            relations.append(
                Relation(f"Z-{entity.attributes['zone_code']}", entity.entity_id, RelationType.CONTAINS)
            )
        elif entity.kind == EntityKind.FIXTURE:
            relations.append(
                Relation(unit_ids[entity.attributes["unit_code"]], entity.entity_id, RelationType.CONTAINS)
            )
    relations.extend(Relation(e.entity_id, e.floor, RelationType.ON_FLOOR) for e in ordered if e.floor)
    model.relations = relations


# --- R0 builder -----------------------------------------------------------------------------------


def _add_static_entities(model: SpatialModel, floor: str) -> None:
    model.add(
        Entity(
            f"X-{floor}",
            EntityKind.EXTERIOR,
            floor,
            rect_poly(0, 0, BUILDING_WIDTH_M, BUILDING_DEPTH_M),
            label="건물 외곽",
            attributes={"width_m": BUILDING_WIDTH_M, "depth_m": BUILDING_DEPTH_M},
        )
    )
    for number, spec in enumerate(BASE_WALKWAYS + FLOOR_WALKWAYS.get(floor, ()), 1):
        model.add(walkway_entity(floor, number, spec))
    for link_id, link_type in VERTICAL_LINKS.items():
        name = LINK_TYPE_NAMES_KO[link_type]
        model.add(
            Entity(
                f"V-{floor}-{link_id}",
                EntityKind.VERTICAL_LINK,
                floor,
                VERTICAL_LINK_GEOMS[link_id],
                label=f"{link_id} {name}",
                attributes={"link_id": link_id, "link_type": link_type, "name_ko": name},
            )
        )
    counters: dict[str, int] = defaultdict(int)
    for amenity_type, geometry in BASE_AMENITIES + FLOOR_AMENITIES.get(floor, ()):
        counters[amenity_type] += 1
        model.add(amenity_entity(floor, amenity_type, counters[amenity_type], geometry))
    for opening_id, name, coords in FLOOR_OPENINGS.get(floor, ()):
        model.add(
            Entity(
                f"O-{floor}-{opening_id}",
                EntityKind.OPENING,
                floor,
                LineString(coords),
                label=f"{opening_id} {name}",
                attributes={"opening_id": opening_id, "opening_type": "entrance", "name_ko": name},
            )
        )


def _plant_vacancies(model: SpatialModel, seed: int) -> set[str]:
    """3F unit in front of ESC-A (R1 move target) plus one random unit on a few other floors."""
    vacant: set[str] = set()
    front = unit_at(model, "3F", ESCALATOR_FRONT_POINTS["ESC-A"])
    if front is None:
        raise RuntimeError("no 3F unit in front of ESC-A")
    vacant.add(front.attributes["unit_code"])
    rng = stable_rng(seed, "vacancies")
    for floor in ("B1", "2F", "5F", "7F", "8F"):
        units = model.units(floor)
        per_category: dict[str, int] = defaultdict(int)
        for unit in units:
            per_category[unit.attributes["category"]] += 1
        candidates = [u for u in units if per_category[u.attributes["category"]] > 2]
        vacant.add(candidates[int(rng.integers(len(candidates)))].attributes["unit_code"])
    return vacant


def _assign_brands(model: SpatialModel, brands: list[Brand], vacant: set[str], seed: int) -> None:
    """Stronger brands get more exposed and larger units (with noise); vacancies stay empty."""
    rng = stable_rng(seed, "brand-assignment")
    pools: dict[str, list[Brand]] = defaultdict(list)
    for brand in brands:
        pools[brand.category].append(brand)
    by_category: dict[str, list[Entity]] = defaultdict(list)
    for unit in model.units():
        if unit.attributes["unit_code"] not in vacant:
            by_category[unit.attributes["category"]].append(unit)
    for category in sorted(by_category):
        units = by_category[category]
        max_area = max(u.geometry.area for u in units)
        scores = {
            u.entity_id: 0.6 / (1 + escalator_distance(u.geometry) / 20) + 0.4 * u.geometry.area / max_area
            for u in units
        }
        noise = rng.normal(0.0, 0.08, len(units))
        ranked = sorted(
            zip(units, noise, strict=True), key=lambda p: (-(scores[p[0].entity_id] + p[1]), p[0].entity_id)
        )
        pool = sorted(pools[category], key=lambda b: (-b.attractiveness, b.brand_id))
        if len(pool) < len(ranked):
            raise ValueError(f"not enough {category} brands: {len(pool)} < {len(ranked)}")
        for (unit, _), brand in zip(ranked, pool, strict=False):
            set_unit_brand(unit, brand)


def build_r0(brands: list[Brand], seed: int = 42) -> SpatialModel:
    """Build the R0 truth model (2024-10-01) from the catalog and the brand list."""
    model = SpatialModel(
        revision=Revision(
            "R0",
            RevisionKind.ACTUAL,
            None,
            REVISION_DATES["R0"],
            source=f"cad2anything.mockgen seed={seed}",
            notes="기준 배치",
        ),
        floors=make_floors(),
    )
    for floor in FLOOR_CODES:
        _add_static_entities(model, floor)
        for code, draft in code_units(floor, plan_floor_units(floor, seed)):
            model.add(make_unit(floor, code, draft.rect, draft.facing, draft.band, draft.category))
    vacant = _plant_vacancies(model, seed)
    _assign_brands(model, brands, vacant, seed)
    for unit in model.units():
        refresh_unit_fixtures(model, unit, seed)
    finalize_model(model)
    return model
