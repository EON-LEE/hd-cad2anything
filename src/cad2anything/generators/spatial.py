"""Spatial generators: fixture schedule, area schedule, and floor directory maps."""

from __future__ import annotations

import colorsys
import csv
from collections import Counter, defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from shapely.geometry import LineString, MultiLineString
from shapely.geometry.base import BaseGeometry

from cad2anything.catalog import AMENITY_TYPES, BUILDING_DEPTH_M, BUILDING_WIDTH_M, CATEGORIES, FIXTURE_TYPES
from cad2anything.catalog import floor_program as catalog_floor_program
from cad2anything.generators.registry import GeneratorContext, GeneratorResult, Issue, generator
from cad2anything.model import Entity, EntityKind

SVG_SCALE = 10.0
LEGEND_HEIGHT_PX = 120.0
LINK_COLORS = {"escalator": "#4a6fa5", "elevator": "#6a4c93", "stair": "#7f7f7f"}


def _floor_codes(ctx: GeneratorContext) -> list[str]:
    codes = [f.code for f in sorted(ctx.model.floors, key=lambda f: f.ordinal)]
    if not codes:
        codes = sorted({e.floor for e in ctx.model.entities.values() if e.floor})
    return [code for code in codes if ctx.floors is None or code in ctx.floors]


def _floor_name(ctx: GeneratorContext, code: str) -> str:
    for floor in ctx.model.floors:
        if floor.code == code and floor.name:
            return floor.name
    try:
        return catalog_floor_program(code).name_ko
    except KeyError:
        return ""


def _write_tables(
    xlsx_path: Path, csv_path: Path, sheets: dict[str, tuple[list[str], list[list]]], csv_sheet: str
) -> list[Path]:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, (header, rows) in sheets.items():
        sheet = workbook.create_sheet(title)
        sheet.append(header)
        for row in rows:
            sheet.append(row)
        sheet.freeze_panes = "A2"
    workbook.save(xlsx_path)
    header, rows = sheets[csv_sheet]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return [xlsx_path, csv_path]


def _unit_label(unit: Entity) -> tuple[str, str, str]:
    attrs = unit.attributes
    return (
        attrs.get("unit_code") or unit.entity_id,
        attrs.get("brand_name") or "",
        attrs.get("category") or "",
    )


@generator("fixture_schedule")
def fixture_schedule(ctx: GeneratorContext) -> GeneratorResult:
    floors = set(_floor_codes(ctx))
    units = {u.attributes.get("unit_code"): u for u in ctx.model.units() if u.floor in floors}
    fixture_codes = sorted(FIXTURE_TYPES)
    detail: list[list] = []
    issues: list[Issue] = []
    per_unit: dict[str, Counter] = defaultdict(Counter)
    footprint: dict[str, float] = defaultdict(float)
    for fixture in sorted(ctx.model.by_kind(EntityKind.FIXTURE), key=lambda e: e.entity_id):
        if fixture.floor not in floors:
            continue
        unit_code = fixture.attributes.get("unit_code")
        fixture_id = fixture.attributes.get("fixture_id", fixture.entity_id)
        unit = units.get(unit_code)
        if unit is None:
            issues.append(
                Issue("error", f"집기 {fixture_id}가 어느 매장에도 속하지 않습니다.", [fixture.entity_id])
            )
            continue
        fixture_type = fixture.attributes.get("fixture_type", "UNKNOWN")
        known = FIXTURE_TYPES.get(fixture_type)
        area = round(fixture.geometry.area, 2)
        _, brand, category = _unit_label(unit)
        detail.append(
            [
                fixture.floor,
                unit_code,
                brand,
                category,
                fixture_id,
                fixture_type,
                known.name_ko if known else fixture_type,
                area,
            ]
        )
        per_unit[unit_code][fixture_type] += 1
        footprint[unit_code] += area

    summary_rows: list[list] = []
    for unit_code in sorted(units):
        unit = units[unit_code]
        _, brand, category = _unit_label(unit)
        counts = per_unit.get(unit_code, Counter())
        if not unit.attributes.get("vacant") and not counts:
            issues.append(
                Issue("warning", f"영업 중인 매장 {unit_code}에 집기가 없습니다.", [unit.entity_id])
            )
        summary_rows.append(
            [
                unit.floor,
                unit_code,
                brand,
                category,
                sum(counts.values()),
                round(footprint.get(unit_code, 0.0), 2),
            ]
            + [counts.get(code, 0) for code in fixture_codes]
        )

    detail_header = ["층", "매장코드", "브랜드", "카테고리", "집기ID", "집기유형", "집기명", "점유면적(㎡)"]
    summary_header = ["층", "매장코드", "브랜드", "카테고리", "집기수", "점유면적(㎡)"] + [
        FIXTURE_TYPES[code].name_ko for code in fixture_codes
    ]
    files = _write_tables(
        ctx.out_dir / "fixture_schedule.xlsx",
        ctx.out_dir / "fixture_schedule.csv",
        {"집기목록": (detail_header, detail), "매장별요약": (summary_header, summary_rows)},
        csv_sheet="집기목록",
    )
    by_type = Counter(row[5] for row in detail)
    return GeneratorResult(
        files,
        {"fixtures": len(detail), "units": len(units), "by_type": dict(sorted(by_type.items()))},
        issues,
    )


@generator("area_schedule")
def area_schedule(ctx: GeneratorContext) -> GeneratorResult:
    unit_rows: list[list] = []
    floor_rows: list[list] = []
    issues: list[Issue] = []
    totals = {"gross_m2": 0.0, "exclusive_m2": 0.0}
    for code in _floor_codes(ctx):
        gross = sum(e.geometry.area for e in ctx.model.by_kind(EntityKind.EXTERIOR, code))
        if gross <= 0:
            issues.append(Issue("error", f"{code}: 건물 외곽 폴리곤이 없어 연면적을 계산할 수 없습니다."))
            continue
        units = sorted(ctx.model.units(code), key=lambda u: u.attributes.get("unit_code", ""))
        exclusive = sum(u.geometry.area for u in units)
        walkway = sum(e.geometry.area for e in ctx.model.by_kind(EntityKind.WALKWAY, code))
        cores = sum(
            e.geometry.area
            for kind in (EntityKind.VERTICAL_LINK, EntityKind.AMENITY)
            for e in ctx.model.by_kind(kind, code)
        )
        common = gross - exclusive
        if common < -0.5:
            issues.append(Issue("error", f"{code}: 전용면적 합계가 연면적을 초과합니다."))
            continue
        lease_total = 0.0
        for unit in units:
            unit_code, brand, category = _unit_label(unit)
            area = unit.geometry.area
            allocated = common * area / exclusive if exclusive else 0.0
            lease = area + allocated
            lease_total += lease
            unit_rows.append(
                [
                    code,
                    unit_code,
                    brand,
                    category,
                    "Y" if unit.attributes.get("vacant") else "N",
                    round(area, 2),
                    round(allocated, 2),
                    round(lease, 2),
                    round(area / lease * 100, 1) if lease else 0.0,
                ]
            )
        if units and abs(lease_total - gross) / gross > 0.005:
            issues.append(Issue("error", f"{code}: 임대면적 합계가 연면적과 0.5% 이상 차이 납니다."))
        floor_rows.append(
            [
                code,
                _floor_name(ctx, code),
                len(units),
                round(gross, 1),
                round(exclusive, 1),
                round(walkway, 1),
                round(cores, 1),
                round(common - walkway - cores, 1),
                round(exclusive / gross * 100, 1),
            ]
        )
        totals["gross_m2"] += gross
        totals["exclusive_m2"] += exclusive

    unit_header = [
        "층",
        "매장코드",
        "브랜드",
        "카테고리",
        "공실",
        "전용면적(㎡)",
        "공용배분(㎡)",
        "임대면적(㎡)",
        "전용률(%)",
    ]
    floor_header = [
        "층",
        "층이름",
        "매장수",
        "연면적(㎡)",
        "전용면적합계(㎡)",
        "통로(㎡)",
        "코어·편의시설(㎡)",
        "기타공용(㎡)",
        "전용비율(%)",
    ]
    files = _write_tables(
        ctx.out_dir / "area_schedule.xlsx",
        ctx.out_dir / "area_schedule.csv",
        {"층별요약": (floor_header, floor_rows), "매장별면적": (unit_header, unit_rows)},
        csv_sheet="매장별면적",
    )
    gross_total = totals["gross_m2"]
    summary = {
        "floors": len(floor_rows),
        "units": len(unit_rows),
        "gross_m2": round(gross_total, 1),
        "exclusive_m2": round(totals["exclusive_m2"], 1),
        "exclusive_ratio_pct": round(totals["exclusive_m2"] / gross_total * 100, 1) if gross_total else 0.0,
    }
    return GeneratorResult(files, summary, issues)


def _category_color(category: str | None) -> str:
    if not category or category not in CATEGORIES:
        return "#d9d9d9"
    index = list(CATEGORIES).index(category)
    hue = (index * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hls_to_rgb(hue, 0.78, 0.55)
    return f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"


def _path_data(geometry: BaseGeometry, depth_m: float) -> str:
    polygons = list(getattr(geometry, "geoms", [geometry]))
    commands: list[str] = []
    for polygon in polygons:
        rings = [polygon.exterior, *polygon.interiors] if hasattr(polygon, "exterior") else []
        for ring in rings:
            points = [f"{x * SVG_SCALE:.1f},{(depth_m - y) * SVG_SCALE:.1f}" for x, y in ring.coords]
            commands.append("M " + " L ".join(points) + " Z")
    return " ".join(commands)


def _line_data(geometry: BaseGeometry, depth_m: float) -> str:
    lines = geometry.geoms if isinstance(geometry, MultiLineString) else [geometry]
    commands = []
    for line in lines:
        if isinstance(line, LineString):
            points = [f"{x * SVG_SCALE:.1f},{(depth_m - y) * SVG_SCALE:.1f}" for x, y in line.coords]
            commands.append("M " + " L ".join(points))
    return " ".join(commands)


def _text(x: float, y: float, value: str, size: float, weight: str = "normal", fill: str = "#222") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="middle" dominant-baseline="middle">{escape(value)}</text>'
    )


@generator("floor_directory_svg")
def floor_directory_svg(ctx: GeneratorContext) -> GeneratorResult:
    files: list[Path] = []
    issues: list[Issue] = []
    labeled = 0
    for code in _floor_codes(ctx):
        exteriors = ctx.model.by_kind(EntityKind.EXTERIOR, code)
        if exteriors:
            min_x, min_y, max_x, max_y = exteriors[0].geometry.bounds
            width_m, depth_m = max_x, max_y
        else:
            width_m, depth_m = BUILDING_WIDTH_M, BUILDING_DEPTH_M
        width_px = width_m * SVG_SCALE
        height_px = depth_m * SVG_SCALE
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -40 {width_px:.0f} {height_px + 40 + LEGEND_HEIGHT_PX:.0f}" '
            f'font-family="Malgun Gothic, Noto Sans KR, sans-serif">',
            _text(
                width_px / 2,
                -20,
                f"{code} {_floor_name(ctx, code)} 층별 안내도 ({ctx.model.revision.rev_id})",
                22,
                "bold",
            ),
        ]
        for exterior in exteriors:
            parts.append(
                f'<path d="{_path_data(exterior.geometry, depth_m)}" fill="#ffffff" stroke="#333" stroke-width="3"/>'
            )
        for walkway in ctx.model.by_kind(EntityKind.WALKWAY, code):
            parts.append(f'<path d="{_path_data(walkway.geometry, depth_m)}" fill="#f2f2f2" stroke="none"/>')
        categories_present: set[str] = set()
        for unit in sorted(ctx.model.units(code), key=lambda u: u.entity_id):
            unit_code, brand, category = _unit_label(unit)
            vacant = bool(unit.attributes.get("vacant"))
            fill = "#ececec" if vacant else _category_color(category)
            dash = ' stroke-dasharray="6 4"' if vacant else ""
            parts.append(
                f'<path d="{_path_data(unit.geometry, depth_m)}" fill="{fill}" stroke="#666" stroke-width="1"{dash}>'
                f"<title>{escape(unit_code)} {escape(brand)}</title></path>"
            )
            if category and not vacant:
                categories_present.add(category)
            name = "공실" if vacant else brand
            if not name:
                issues.append(
                    Issue("warning", f"{unit_code}: 영업 매장인데 브랜드명이 없습니다.", [unit.entity_id])
                )
                name = unit_code
            point = unit.geometry.representative_point()
            min_x, _, max_x, _ = unit.geometry.bounds
            size = max(6.0, min(14.0, (max_x - min_x) * SVG_SCALE / max(len(name), 1) / 0.95))
            px, py = point.x * SVG_SCALE, (depth_m - point.y) * SVG_SCALE
            parts.append(_text(px, py - 5, name, size, "bold" if not vacant else "normal"))
            parts.append(_text(px, py + size, unit_code, 7, fill="#555"))
            labeled += 1
        for link in ctx.model.by_kind(EntityKind.VERTICAL_LINK, code):
            link_type = link.attributes.get("link_type", "")
            point = link.geometry.representative_point()
            parts.append(
                f'<path d="{_path_data(link.geometry, depth_m)}" fill="{LINK_COLORS.get(link_type, "#999")}" stroke="#333"/>'
            )
            parts.append(
                _text(
                    point.x * SVG_SCALE,
                    (depth_m - point.y) * SVG_SCALE,
                    link.attributes.get("link_id", ""),
                    9,
                    "bold",
                    "#fff",
                )
            )
        for amenity in ctx.model.by_kind(EntityKind.AMENITY, code):
            amenity_type = amenity.attributes.get("amenity_type", "")
            point = amenity.geometry.representative_point()
            parts.append(
                f'<path d="{_path_data(amenity.geometry, depth_m)}" fill="#c8e6c9" stroke="#4caf50"/>'
            )
            parts.append(
                _text(
                    point.x * SVG_SCALE,
                    (depth_m - point.y) * SVG_SCALE,
                    AMENITY_TYPES.get(amenity_type, amenity_type),
                    9,
                )
            )
        for opening in ctx.model.by_kind(EntityKind.OPENING, code):
            if isinstance(opening.geometry, LineString | MultiLineString):
                parts.append(
                    f'<path d="{_line_data(opening.geometry, depth_m)}" stroke="#d9534f" stroke-width="6" fill="none"/>'
                )
        legend_y = height_px + 30
        for index, category in enumerate(sorted(categories_present)):
            x = 20 + (index % 6) * (width_px - 40) / 6
            y = legend_y + (index // 6) * 28
            parts.append(
                f'<rect x="{x:.1f}" y="{y - 9:.1f}" width="18" height="18" fill="{_category_color(category)}" stroke="#666"/>'
            )
            parts.append(
                f'<text x="{x + 26:.1f}" y="{y:.1f}" font-size="13" dominant-baseline="middle">'
                f"{escape(CATEGORIES[category].name_ko)}</text>"
            )
        parts.append("</svg>")
        path = ctx.out_dir / f"floor_directory_{code}.svg"
        path.write_text("\n".join(parts), encoding="utf-8")
        files.append(path)
    return GeneratorResult(files, {"floors": len(files), "labeled_units": labeled}, issues)
