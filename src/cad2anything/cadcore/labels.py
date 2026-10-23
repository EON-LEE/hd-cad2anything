"""Text patterns and label assignment: unit codes and brands, zones, vertical links, amenities and openings.

Labels are matched to geometry by point-in-polygon on the estimated text center, falling back to the
nearest polygon within a small distance. Unit labels may be one combined text (``3F-C02 Brand`` or MTEXT
``3F-C02\\nBrand``) or a code text with a separate brand text near the centroid; ``(공실)``/``공실`` marks a
vacancy. Code patterns come from the active drawing profile (``cadcore.profile``; default: the synthetic
conventions), which also rewrites codes to the canonical form.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point, Polygon
from shapely.strtree import STRtree

from cad2anything.cadcore.profile import BARE_UNIT_CODE_RE, UNIT_CODE_IN_TEXT_RE, active_profile
from cad2anything.cadcore.profile import LINK_PREFIX_TYPES as LINK_PREFIX_TYPES
from cad2anything.cadcore.profile import LINK_RE as LINK_RE
from cad2anything.cadcore.profile import LINK_WORDS as LINK_WORDS
from cad2anything.cadcore.profile import OPENING_RE as OPENING_RE
from cad2anything.cadcore.profile import UNIT_CODE_RE as UNIT_CODE_RE
from cad2anything.cadcore.profile import VACANCY_RE as VACANCY_RE
from cad2anything.cadcore.profile import ZONE_RE as ZONE_RE
from cad2anything.cadcore.reader import Issue, Primitive

# Default-convention patterns, kept importable; parsing uses the active profile's patterns.
_UNIT_CODE_IN_TEXT = UNIT_CODE_IN_TEXT_RE
_BARE_UNIT_CODE = BARE_UNIT_CODE_RE
NUMERIC_RE = re.compile(r"^[\d\s.,+\-/xX×±@]+$")
_SEPARATORS = " \t\n-:/|·,"

LINK_NAMES_KO: dict[str, str] = {"escalator": "에스컬레이터", "elevator": "엘리베이터", "stair": "계단"}
AMENITY_TOKENS: dict[str, str] = {
    "WC": "RESTROOM",
    "TOILET": "RESTROOM",
    "RESTROOM": "RESTROOM",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "ATM": "ATM",
    "NURSING": "NURSING",
    "LOUNGE": "LOUNGE",
}
AMENITY_WORDS_KO: dict[str, str] = {
    "화장실": "RESTROOM",
    "안내데스크": "INFO",
    "안내": "INFO",
    "수유실": "NURSING",
    "수유": "NURSING",
    "라운지": "LOUNGE",
}
TITLE_WORDS: tuple[str, ...] = ("평면도", "도면", "배치도", "PLAN", "FLOOR", "LEVEL")
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z\uac00-\ud7a3]+")


# --- text classification -----------------------------------------------------------------------------


def find_unit_code(text: str, floor: str | None = None) -> tuple[str | None, str]:
    """``(unit_code, remainder)`` for the first unit code in ``text`` (remainder stripped).

    The code is canonical for the active profile; ``floor`` (the drawing's floor) fills a missing floor.
    """
    profile = active_profile()
    match = profile.unit_pattern.search(text)
    if not match:
        return None, text.strip()
    rest = (text[: match.start()] + " " + text[match.end() :]).strip(_SEPARATORS)
    return profile.unit_code(match, floor), " ".join(rest.split())


def link_info(text: str) -> tuple[str | None, str | None]:
    """``(link_id, link_type)`` from a vertical link label (ID or Korean/English name)."""
    profile = active_profile()
    upper = text.upper()
    match = profile.link_pattern.search(upper)
    link_id = profile.link_id(match) if match else None
    link_type = profile.link_type(link_id) if link_id else None
    if link_type is None:
        for word, kind in profile.link_words.items():
            if word in upper:
                link_type = kind
                break
    return link_id, link_type


def amenity_type(text: str) -> str | None:
    tokens = {t.upper() for t in _TOKEN_SPLIT.split(text) if t}
    for token, kind in AMENITY_TOKENS.items():
        if token in tokens:
            return kind
    for word, kind in AMENITY_WORDS_KO.items():
        if word in text:
            return kind
    return None


def text_kind(text: str) -> str:
    """Coarse role of a text: unit_code, unit_combined, zone_code, link, opening, amenity, vacancy,
    numeric, title or free."""
    value = text.strip()
    profile = active_profile()
    code, rest = find_unit_code(value)
    if code:
        return "unit_combined" if rest else "unit_code"
    if profile.zone_pattern.match(value):
        return "zone_code"
    if profile.opening_pattern.search(value.upper()):
        return "opening"
    link_id, _ = link_info(value)
    if link_id or "".join(value.upper().split()) in profile.link_words:
        return "link"
    if len(value) <= 24 and amenity_type(value):
        return "amenity"
    if profile.vacancy_pattern.match(value):
        return "vacancy"
    if NUMERIC_RE.match(value):
        return "numeric"
    if len(value) >= 10 and any(word in value.upper() for word in TITLE_WORDS):
        return "title"
    return "free"


# --- spatial helpers ---------------------------------------------------------------------------------


def assign_points(
    points: Sequence[tuple[float, float]], polygons: Sequence[Polygon], max_distance: float
) -> list[int | None]:
    """Index of the polygon containing each point (smallest when nested), else the nearest within
    ``max_distance``, else None."""
    if not polygons:
        return [None] * len(points)
    tree = STRtree(list(polygons))
    result: list[int | None] = []
    for x, y in points:
        pt = Point(x, y)
        hits = [int(i) for i in tree.query(pt, predicate="intersects")]
        if hits:
            result.append(min(hits, key=lambda i: polygons[i].area))
            continue
        nearest = tree.query_nearest(pt, max_distance=max_distance) if max_distance > 0 else []
        result.append(int(nearest[0]) if len(nearest) else None)
    return result


# --- units -------------------------------------------------------------------------------------------


@dataclass
class UnitLabel:
    unit_code: str | None = None
    brand_name: str | None = None
    vacant: bool | None = None
    method: str = "none"
    texts: list[str] = field(default_factory=list)


@dataclass
class _Token:
    code: str | None
    brand: str | None
    point: tuple[float, float]
    height: float
    text: str


def _tokens(texts: Sequence[Primitive], floor: str) -> list[_Token]:
    profile = active_profile()
    tokens: list[_Token] = []
    for prim in texts:
        value = prim.text.strip()
        point = prim.anchor or (prim.points[0] if prim.points else (0.0, 0.0))
        code, rest = find_unit_code(value, floor)
        if code is None:
            words = value.split()
            bare = profile.bare_unit_code(words[0], floor) if words else None
            if bare is not None:
                code, rest = bare, " ".join(words[1:])
        brand = " ".join(rest.split()) if rest else None
        if code is None and (brand is None or NUMERIC_RE.match(brand)):
            continue
        tokens.append(_Token(code, brand, point, prim.height or 0.5, prim.text))
    return tokens


def _brand_value(brand: str | None) -> tuple[str | None, bool | None]:
    if brand is None:
        return None, None
    if active_profile().vacancy_pattern.match(brand.strip()):
        return None, True
    return brand, False


def label_units(
    polygons: Sequence[Polygon], texts: Sequence[Primitive], floor: str, max_distance: float = 1.0
) -> tuple[list[UnitLabel], list[Issue]]:
    """Unit code, brand name and vacancy for each polygon (same order as ``polygons``)."""
    issues: list[Issue] = []
    tokens = _tokens(texts, floor)
    owners = assign_points([t.point for t in tokens], polygons, max_distance)
    per_polygon: dict[int, list[_Token]] = {}
    orphans: list[_Token] = []
    for token, owner in zip(tokens, owners, strict=True):
        if owner is None:
            orphans.append(token)
        else:
            per_polygon.setdefault(owner, []).append(token)
    labels: list[UnitLabel] = []
    for index, polygon in enumerate(polygons):
        own = per_polygon.get(index, [])
        codes = [t for t in own if t.code]
        label = UnitLabel(texts=[t.text for t in own])
        if not codes:
            labels.append(label)
            continue
        center = polygon.representative_point()
        codes.sort(key=lambda t: Point(t.point).distance(center))
        chosen = codes[0]
        if len({t.code for t in codes}) > 1:
            issues.append(
                Issue(
                    "warning",
                    "multiple_unit_codes",
                    f"한 매장 경계 안에 매장 코드가 여러 개 있다: {', '.join(sorted({str(t.code) for t in codes}))}",
                    floor,
                    data={"codes": sorted({str(t.code) for t in codes})},
                )
            )
        label.unit_code = chosen.code
        brand_text = chosen.brand
        label.method = "combined" if brand_text else "code_only"
        if brand_text is None:
            free = [t for t in own if t.code is None and t.brand]
            if not free:
                near = [
                    t
                    for t in orphans
                    if t.code is None and t.brand and math.dist(t.point, chosen.point) <= 3 * chosen.height
                ]
                free = near
            if free:
                free.sort(key=lambda t: (math.dist(t.point, chosen.point), -t.point[1]))
                brand_text = free[0].brand
                label.method = "separate"
        label.brand_name, label.vacant = _brand_value(brand_text)
        labels.append(label)
    for token in orphans:
        if token.code:
            issues.append(
                Issue(
                    "error",
                    "unit_label_without_polygon",
                    f"매장 경계를 찾지 못한 매장 라벨: {token.text!r}",
                    floor,
                    data={
                        "unit_code": token.code,
                        "x": round(token.point[0], 2),
                        "y": round(token.point[1], 2),
                    },
                )
            )
    return labels, issues


# --- zones -------------------------------------------------------------------------------------------


def parse_zone_label(text: str, floor: str) -> tuple[str, str] | None:
    """``(zone_code, zone_name)`` of a zone label (active profile), else None."""
    return active_profile().zone_label(text, floor)


def label_zones(
    polygons: Sequence[Polygon], texts: Sequence[Primitive], floor: str, max_distance: float = 3.0
) -> tuple[list[tuple[str, str] | None], list[Issue]]:
    """``(zone_code, zone_name)`` per zone polygon part (None when the part carries no label)."""
    issues: list[Issue] = []
    parsed = [(t, parse_zone_label(t.text, floor)) for t in texts]
    parsed = [(t, p) for t, p in parsed if p is not None]
    owners = assign_points([t.anchor or t.points[0] for t, _ in parsed], polygons, max_distance)
    result: list[tuple[str, str] | None] = [None] * len(polygons)
    for (text, label), owner in zip(parsed, owners, strict=True):
        if owner is None:
            issues.append(
                Issue(
                    "warning",
                    "zone_label_without_polygon",
                    f"존 경계를 찾지 못한 존 라벨: {text.text!r}",
                    floor,
                )
            )
        elif result[owner] is None:
            result[owner] = label
        elif result[owner] != label:
            issues.append(
                Issue(
                    "warning",
                    "multiple_zone_labels",
                    f"한 존 경계에 존 라벨이 여러 개 있다: {text.text!r}",
                    floor,
                )
            )
    return result, issues


# --- vertical links, amenities, openings ------------------------------------------------------------


def label_vertical_links(
    polygons: Sequence[Polygon],
    layer_types: Sequence[str | None],
    texts: Sequence[Primitive],
    floor: str,
    max_distance: float = 2.0,
) -> tuple[list[tuple[str | None, str | None, str]], list[Issue]]:
    """``(link_id, link_type, label)`` per vertical-link polygon; type falls back to the layer semantic."""
    issues: list[Issue] = []
    infos = [(t, *link_info(t.text)) for t in texts]
    infos = [(t, lid, ltype) for t, lid, ltype in infos if lid or ltype]
    owners = assign_points([t.anchor or t.points[0] for t, _, _ in infos], polygons, max_distance)
    result: list[tuple[str | None, str | None, str]] = [
        (None, layer_types[i], "") for i in range(len(polygons))
    ]
    for (text, link_id, link_type), owner in zip(infos, owners, strict=True):
        if owner is None:
            continue
        current_id, current_type, _ = result[owner]
        if current_id and link_id and current_id != link_id:
            issues.append(
                Issue("warning", "multiple_link_labels", f"수직동선 라벨이 겹친다: {text.text!r}", floor)
            )
            continue
        result[owner] = (link_id or current_id, link_type or current_type, text.text)
    return result, issues


def label_amenities(
    polygons: Sequence[Polygon], texts: Sequence[Primitive], max_distance: float = 2.0
) -> list[tuple[str | None, str]]:
    """``(amenity_type, label)`` per amenity polygon."""
    infos = [(t, amenity_type(t.text)) for t in texts]
    infos = [(t, k) for t, k in infos if k]
    owners = assign_points([t.anchor or t.points[0] for t, _ in infos], polygons, max_distance)
    result: list[tuple[str | None, str]] = [(None, "")] * len(polygons)
    for (text, kind), owner in zip(infos, owners, strict=True):
        if owner is not None and result[owner][0] is None:
            result[owner] = (kind, text.text)
    return result


def parse_opening_label(text: str) -> tuple[str | None, str]:
    profile = active_profile()
    match = profile.opening_pattern.search(text.upper())
    if not match:
        return None, text.strip()
    name = (text[: match.start()] + " " + text[match.end() :]).strip(_SEPARATORS)
    return profile.opening_id(match), " ".join(name.split())


def label_openings(
    lines: Sequence[LineString], texts: Sequence[Primitive], max_distance: float = 3.0
) -> list[tuple[str | None, str, str]]:
    """``(opening_id, name, raw label)`` per opening line (nearest label within ``max_distance``)."""
    result: list[tuple[str | None, str, str]] = [(None, "", "")] * len(lines)
    if not lines:
        return result
    tree = STRtree(list(lines))
    for text in texts:
        opening_id, name = parse_opening_label(text.text)
        if opening_id is None:
            continue
        pt = Point(text.anchor or text.points[0])
        nearest = tree.query_nearest(pt, max_distance=max_distance)
        if len(nearest):
            index = int(nearest[0])
            if result[index][0] is None:
                result[index] = (opening_id, name, text.text)
    return result
