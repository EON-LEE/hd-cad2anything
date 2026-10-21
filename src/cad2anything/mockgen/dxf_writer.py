"""DXF and manifest output for truth models (docs/specs/cad-conventions.md).

- clean: DXF R2018, meters (``$INSUNITS=6``), canonical layers, MTEXT unit labels, fixture blocks
  ``FX_{TYPE}`` with ``FIXTURE_ID``/``TYPE``/``W``/``D`` attributes; the manifest maps semantics to layers.
- messy: DXF R2000 with ``$DWGCODEPAGE=ANSI_949``, millimeters (``$INSUNITS=4``), per-floor random
  layer names (including meaningless ones), partly open or exploded unit outlines, TEXT labels, Korean
  block names with some attribute-less blocks, and noise entities; the manifest has no layer mapping.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment
from ezdxf.layouts import BaseLayout
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from cad2anything.catalog import FACILITY_CODE, FACILITY_NAME, FIXTURE_TYPES, floor_program
from cad2anything.mockgen.layout import stable_rng
from cad2anything.model import EntityKind, SpatialModel

CLEAN_LAYERS: dict[str, str] = {
    "exterior": "A-EXTR",
    "wall": "A-WALL",
    "unit": "A-UNIT",
    "unit_label": "A-UNIT-IDEN",
    "zone": "A-ZONE",
    "zone_label": "A-ZONE-IDEN",
    "walkway": "A-CORR",
    "escalator": "A-VERT-ESCL",
    "elevator": "A-VERT-ELEV",
    "stair": "A-VERT-STAI",
    "opening": "A-DOOR",
    "amenity": "A-AMEN",
    "amenity_label": "A-AMEN-IDEN",
    "fixture": "A-FIXT",
    "annotation": "A-ANNO",
}
_LAYER_COLORS: dict[str, int] = {
    "exterior": 7, "wall": 8, "unit": 5, "unit_label": 5, "zone": 3, "zone_label": 3, "walkway": 9,
    "escalator": 1, "elevator": 1, "stair": 1, "opening": 2, "amenity": 6, "amenity_label": 6,
    "fixture": 4, "annotation": 7,
}  # fmt: skip

MESSY_LAYER_POOLS: dict[str, tuple[str, ...]] = {
    "exterior": ("외곽", "외벽선", "OUTLINE", "BLDG-OUTLINE", "건물외곽", "A-EXTR-OTLN"),
    "wall": ("벽", "WALL", "A-WALL-EXST", "Wall_구조", "벽체", "구조벽"),
    "unit": ("매장경계", "TENANT", "구획", "A-UNIT-BNDY", "임대구획", "SHOP_LINE"),
    "unit_label": ("매장명", "TENANT_TXT", "구획번호", "SHOP-NAME", "문자_매장", "TXT-UNIT"),
    "zone": ("존", "ZONE", "조닝", "MD구역", "A-AREA-ZONE"),
    "zone_label": ("존명", "ZONE-TXT", "조닝문자", "MD구역명"),
    "walkway": ("통로", "CORRIDOR", "동선", "복도", "A-CIRC", "공용통로"),
    "escalator": ("에스컬레이터", "ESC", "E-S", "승강설비_ES", "V-ESC"),
    "elevator": ("엘리베이터", "ELEV", "E-V", "승강기", "V-ELV"),
    "stair": ("계단", "STAIR", "STR", "피난계단", "V-STR"),
    "opening": ("출입구", "DOOR", "문", "OPENING", "A-DOOR-EXT"),
    "amenity": ("편의시설", "AMENITY", "공용시설", "FAC-AMEN"),
    "amenity_label": ("편의시설명", "AMEN-TXT", "시설명"),
    "fixture": ("집기", "FIXTURE", "FURN", "가구", "A-FURN", "집기배치"),
    "annotation": ("주석", "ANNO", "TITLE", "도면명", "NOTE"),
}
MEANINGLESS_LAYERS: tuple[str, ...] = ("0", "LAYER_17", "LAYER_3", "Layer1", "ABC")
MEANINGLESS_CANDIDATES: tuple[str, ...] = (
    "zone", "zone_label", "annotation", "amenity_label", "wall", "fixture", "unit_label", "walkway",
)  # fmt: skip
NOISE_LAYERS: tuple[str, ...] = ("치수", "DIM", "메모", "MEMO", "임시", "TEMP", "참조_구도면", "XREF-OLD")
MESSY_BLOCK_NAMES: dict[str, str] = {
    "SHOWCASE": "쇼케이스",
    "HANGER": "행거_01",
    "GONDOLA": "곤돌라_A",
    "COUNTER": "카운터",
    "TABLE": "진열테이블",
    "MANNEQUIN": "마네킹",
    "FITTING": "피팅룸",
    "DINING": "식탁_4인",
}
_MEMOS: tuple[str, ...] = (
    "MD팀 확인 요망",
    "2024.09 수정",
    "가벽 철거 예정",
    "소방 점검 필요",
    "도면 검토용",
    "치수 재확인",
    "임시 저장본",
    "전기 용량 확인",
    "공사 구간",
    "구도면 참조",
)
_MM = 1000.0


@contextmanager
def _reproducible() -> Iterator[None]:
    """Fixed ezdxf metadata (dates, GUIDs, markers) so reruns produce byte-identical files."""
    previous = ezdxf.options.write_fixed_meta_data_for_testing
    ezdxf.options.write_fixed_meta_data_for_testing = True
    try:
        yield
    finally:
        ezdxf.options.write_fixed_meta_data_for_testing = previous


def _new_doc(dxfversion: str) -> Any:
    with _reproducible():
        return ezdxf.new(dxfversion, setup=False)


def _save(doc: Any, path: Path) -> None:
    """Save with LF line endings, fixed metadata and a sorted CLASSES section (byte-identical reruns)."""
    with _reproducible():
        doc.commit_pending_changes()
        doc.update_all()
        # ezdxf registers in-use classes from a set, so their order follows per-process string hashing.
        doc.classes.classes = dict(sorted(doc.classes.classes.items()))
        with open(path, "w", encoding=doc.output_encoding, errors="dxfreplace", newline="\n") as fp:
            doc.write(fp)


def _rings(geometry: BaseGeometry) -> list[list[tuple[float, float]]]:
    polygons = list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]
    rings: list[list[tuple[float, float]]] = []
    for poly in polygons:
        if isinstance(poly, Polygon) and not poly.is_empty:
            rings.append([(x, y) for x, y in poly.exterior.coords[:-1]])
            rings.extend([(x, y) for x, y in hole.coords[:-1]] for hole in poly.interiors)
    return rings


def _scaled(points: list[tuple[float, float]], scale: float) -> list[tuple[float, float]]:
    return [(round(x * scale, 3), round(y * scale, 3)) for x, y in points]


def _label_point(geometry: BaseGeometry) -> tuple[float, float]:
    target = max(geometry.geoms, key=lambda g: g.area) if isinstance(geometry, MultiPolygon) else geometry
    point = target.centroid if target.contains(target.centroid) else target.representative_point()
    return (round(point.x, 3), round(point.y, 3))


def _title(model: SpatialModel, floor: str) -> str:
    rev = model.revision
    when = rev.effective_date or "가상안"
    return f"{FACILITY_NAME} {floor} {floor_program(floor).name_ko} 평면도 - {rev.rev_id} ({when})"


def _wall_rings(model: SpatialModel, floor: str) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    for link in model.by_kind(EntityKind.VERTICAL_LINK, floor):
        if link.attributes["link_type"] in ("stair", "elevator"):
            rings.extend(_rings(link.geometry))
    for amenity in model.by_kind(EntityKind.AMENITY, floor):
        if amenity.attributes["amenity_type"] == "RESTROOM":
            rings.extend(_rings(amenity.geometry))
    return rings


# --- clean -----------------------------------------------------------------------------------------


def _clean_blocks(doc: Any) -> None:
    for code, ft in FIXTURE_TYPES.items():
        block = doc.blocks.new(f"FX_{code}")
        w, d = ft.width_m / 2, ft.depth_m / 2
        block.add_lwpolyline([(-w, -d), (w, -d), (w, d), (-w, d)], close=True)
        for i, tag in enumerate(("FIXTURE_ID", "TYPE", "W", "D")):
            block.add_attdef(tag, (-w, d + 0.05 + i * 0.12), dxfattribs={"height": 0.1, "flags": 1})


def write_clean_floor(model: SpatialModel, floor: str, path: Path) -> None:
    doc = _new_doc("R2018")
    doc.header["$INSUNITS"] = 6
    doc.header["$MEASUREMENT"] = 1
    doc.styles.add("KR", font="malgun.ttf")
    for semantic, name in CLEAN_LAYERS.items():
        doc.layers.add(name, color=_LAYER_COLORS[semantic])
    _clean_blocks(doc)
    msp = doc.modelspace()

    def poly(ring: list[tuple[float, float]], semantic: str) -> None:
        msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": CLEAN_LAYERS[semantic]})

    def text(value: str, at: tuple[float, float], semantic: str, height: float = 0.5) -> None:
        entity = msp.add_text(
            value, dxfattribs={"layer": CLEAN_LAYERS[semantic], "height": height, "style": "KR"}
        )
        entity.set_placement(at, align=TextEntityAlignment.MIDDLE_CENTER)

    def mtext(value: str, at: tuple[float, float], semantic: str, height: float) -> None:
        entity = msp.add_mtext(
            value, dxfattribs={"layer": CLEAN_LAYERS[semantic], "char_height": height, "style": "KR"}
        )
        entity.set_location(at, attachment_point=MTextEntityAlignment.MIDDLE_CENTER)

    for exterior in model.by_kind(EntityKind.EXTERIOR, floor):
        for ring in _rings(exterior.geometry):
            poly(ring, "exterior")
    for ring in _wall_rings(model, floor):
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
            msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": CLEAN_LAYERS["wall"]})
    for walkway in model.by_kind(EntityKind.WALKWAY, floor):
        for ring in _rings(walkway.geometry):
            poly(ring, "walkway")
    for link in model.by_kind(EntityKind.VERTICAL_LINK, floor):
        semantic = str(link.attributes["link_type"])
        for ring in _rings(link.geometry):
            poly(ring, semantic)
        text(link.label or "", _label_point(link.geometry), semantic, 0.5)
    for amenity in model.by_kind(EntityKind.AMENITY, floor):
        for ring in _rings(amenity.geometry):
            poly(ring, "amenity")
        text(amenity.label or "", _label_point(amenity.geometry), "amenity_label", 0.5)
    for opening in model.by_kind(EntityKind.OPENING, floor):
        assert isinstance(opening.geometry, LineString)
        (x0, y0), (x1, y1) = opening.geometry.coords[0], opening.geometry.coords[-1]
        msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": CLEAN_LAYERS["opening"]})
        text(opening.label or "", _inward_point(opening.geometry), "opening", 0.6)
    for zone in model.by_kind(EntityKind.ZONE, floor):
        for ring in _rings(zone.geometry):
            poly(ring, "zone")
        x, y = _label_point(zone.geometry)
        mtext(zone.label or "", (x, round(y + 1.6, 3)), "zone_label", 0.9)
    for unit in model.by_kind(EntityKind.UNIT, floor):
        for ring in _rings(unit.geometry):
            poly(ring, "unit")
        brand = unit.attributes.get("brand_name") or "(공실)"
        mtext(f"{unit.attributes['unit_code']}\\P{brand}", _label_point(unit.geometry), "unit_label", 0.6)
    for fixture in model.by_kind(EntityKind.FIXTURE, floor):
        attrs = fixture.attributes
        center = fixture.geometry.centroid
        ref = msp.add_blockref(
            f"FX_{attrs['fixture_type']}",
            (round(center.x, 3), round(center.y, 3)),
            dxfattribs={"layer": CLEAN_LAYERS["fixture"], "rotation": attrs["rotation_deg"]},
        )
        ref.add_auto_attribs(
            {
                "FIXTURE_ID": attrs["fixture_id"],
                "TYPE": attrs["fixture_name"],
                "W": f"{attrs['width_m']:g}",
                "D": f"{attrs['depth_m']:g}",
            }
        )
    text(_title(model, floor), (60.0, -4.0), "annotation", 1.5)
    _save(doc, path)


def _inward_point(line: LineString, offset: float = 1.5) -> tuple[float, float]:
    """Point ``offset`` meters inside the building from the middle of an exterior line."""
    mid = line.interpolate(0.5, normalized=True)
    x, y = mid.x, mid.y
    if y <= 1e-6:
        y += offset
    elif x <= 1e-6:
        x += offset
    elif x >= 120 - 1e-6:
        x -= offset
    else:
        y -= offset
    return (round(x, 3), round(y, 3))


# --- messy -----------------------------------------------------------------------------------------


def messy_layer_names(floor: str, seed: int) -> dict[str, str]:
    """Per-floor random layer names from synonym pools; some semantics get meaningless names."""
    rng = stable_rng(seed, "messy-layers", floor)
    names = {semantic: str(rng.choice(pool)) for semantic, pool in MESSY_LAYER_POOLS.items()}
    if rng.random() < 0.7:
        count = int(rng.integers(1, 3))
        semantics = [str(s) for s in rng.choice(MEANINGLESS_CANDIDATES, size=count, replace=False)]
        meaningless = [str(m) for m in rng.choice(MEANINGLESS_LAYERS, size=count, replace=False)]
        names.update(zip(semantics, meaningless, strict=True))
    return names


def _messy_blocks(doc: Any, missing: set[str]) -> None:
    for code, ft in FIXTURE_TYPES.items():
        block = doc.blocks.new(MESSY_BLOCK_NAMES[code])
        w, d = ft.width_m * _MM / 2, ft.depth_m * _MM / 2
        block.add_lwpolyline([(-w, -d), (w, -d), (w, d), (-w, d)], close=True)
        block.add_line((-w, -d), (w, d))
        if code not in missing:
            for i, tag in enumerate(("FIXTURE_ID", "TYPE", "W", "D")):
                block.add_attdef(tag, (-w, d + 50 + i * 120), dxfattribs={"height": 100, "flags": 1})


def _messy_outline(
    msp: BaseLayout, ring: list[tuple[float, float]], layer: str, style: float, gap: float
) -> None:
    """Closed polyline (~70%), open polyline with a small end gap (~20%) or four LINEs (~10%)."""
    if style < 0.7:
        msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": layer})
    elif style < 0.9:
        (x0, y0), (xl, yl) = ring[0], ring[-1]
        length = max(((xl - x0) ** 2 + (yl - y0) ** 2) ** 0.5, 1e-9)
        end = (round(x0 + (xl - x0) / length * gap, 3), round(y0 + (yl - y0) / length * gap, 3))
        msp.add_lwpolyline([*ring, end], close=False, dxfattribs={"layer": layer})
    else:
        for a, b in zip(ring, ring[1:] + ring[:1], strict=True):
            msp.add_line(a, b, dxfattribs={"layer": layer})


def _messy_text(msp: BaseLayout, value: str, at: tuple[float, float], layer: str, height: float) -> None:
    entity = msp.add_text(value, dxfattribs={"layer": layer, "height": height})
    entity.set_placement(at, align=TextEntityAlignment.MIDDLE_CENTER)


def write_messy_floor(model: SpatialModel, floor: str, path: Path, seed: int) -> None:
    layers = messy_layer_names(floor, seed)
    rng = stable_rng(seed, "messy", model.revision.rev_id, floor)
    doc = _new_doc("R2000")
    doc.encoding = "cp949"
    doc.header["$DWGCODEPAGE"] = "ANSI_949"
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    noise_layers = [str(n) for n in rng.choice(NOISE_LAYERS, size=3, replace=False)]
    for name in sorted(set(layers.values()) | set(noise_layers)):
        if name not in doc.layers:
            doc.layers.add(name, color=int(rng.integers(1, 10)))
    used_types = sorted({f.attributes["fixture_type"] for f in model.by_kind(EntityKind.FIXTURE, floor)})
    n_missing = max(1, round(0.2 * len(used_types))) if used_types else 0
    missing = {str(t) for t in rng.choice(used_types, size=n_missing, replace=False)} if n_missing else set()
    _messy_blocks(doc, missing)
    msp = doc.modelspace()

    for exterior in model.by_kind(EntityKind.EXTERIOR, floor):
        for ring in _rings(exterior.geometry):
            msp.add_lwpolyline(_scaled(ring, _MM), close=True, dxfattribs={"layer": layers["exterior"]})
    for ring in _wall_rings(model, floor):
        pts = _scaled(ring, _MM)
        for a, b in zip(pts, pts[1:] + pts[:1], strict=True):
            msp.add_line(a, b, dxfattribs={"layer": layers["wall"]})
    for walkway in model.by_kind(EntityKind.WALKWAY, floor):
        for ring in _rings(walkway.geometry):
            msp.add_lwpolyline(_scaled(ring, _MM), close=True, dxfattribs={"layer": layers["walkway"]})
    for link in model.by_kind(EntityKind.VERTICAL_LINK, floor):
        semantic = str(link.attributes["link_type"])
        for ring in _rings(link.geometry):
            msp.add_lwpolyline(_scaled(ring, _MM), close=True, dxfattribs={"layer": layers[semantic]})
        link_id = link.attributes["link_id"]
        label = str(rng.choice([link.label or link_id, link_id, f"{link.attributes['name_ko']} {link_id}"]))
        _messy_text(msp, label, _scaled([_label_point(link.geometry)], _MM)[0], layers[semantic], 450)
    for amenity in model.by_kind(EntityKind.AMENITY, floor):
        for ring in _rings(amenity.geometry):
            msp.add_lwpolyline(_scaled(ring, _MM), close=True, dxfattribs={"layer": layers["amenity"]})
        point = _scaled([_label_point(amenity.geometry)], _MM)[0]
        _messy_text(msp, amenity.label or "", point, layers["amenity_label"], 450)
    for opening in model.by_kind(EntityKind.OPENING, floor):
        coords = _scaled([opening.geometry.coords[0], opening.geometry.coords[-1]], _MM)
        msp.add_line(coords[0], coords[1], dxfattribs={"layer": layers["opening"]})
        point = _scaled([_inward_point(opening.geometry)], _MM)[0]
        _messy_text(msp, opening.label or "", point, layers["opening"], 600)
    for zone in model.by_kind(EntityKind.ZONE, floor):
        for ring in _rings(zone.geometry):
            msp.add_lwpolyline(_scaled(ring, _MM), close=True, dxfattribs={"layer": layers["zone"]})
        x, y = _label_point(zone.geometry)
        _messy_text(msp, zone.label or "", (x * _MM, (y + 1.6) * _MM), layers["zone_label"], 800)
    for unit in model.by_kind(EntityKind.UNIT, floor):
        code = unit.attributes["unit_code"]
        unit_rng = stable_rng(seed, "messy-unit", floor, code)
        ring = _rings(unit.geometry)[0]
        start = int(unit_rng.integers(len(ring)))
        ring = ring[start:] + ring[:start]
        if unit_rng.random() < 0.5:
            ring = ring[::-1]
        style, gap = float(unit_rng.random()), float(unit_rng.uniform(10, 40))
        _messy_outline(msp, _scaled(ring, _MM), layers["unit"], style, gap)
        brand = unit.attributes.get("brand_name") or str(unit_rng.choice(["(공실)", "공실"]))
        x, y = _label_point(unit.geometry)
        height = float(unit_rng.choice([350.0, 400.0, 450.0, 500.0]))
        if unit_rng.random() < 0.5:
            _messy_text(msp, f"{code} {brand}", (x * _MM, y * _MM), layers["unit_label"], height)
        else:
            _messy_text(msp, code, (x * _MM, y * _MM + height * 0.9), layers["unit_label"], height)
            _messy_text(msp, brand, (x * _MM, y * _MM - height * 0.9), layers["unit_label"], height)
    for fixture in model.by_kind(EntityKind.FIXTURE, floor):
        attrs = fixture.attributes
        center = fixture.geometry.centroid
        ref = msp.add_blockref(
            MESSY_BLOCK_NAMES[attrs["fixture_type"]],
            (round(center.x * _MM, 1), round(center.y * _MM, 1)),
            dxfattribs={"layer": layers["fixture"], "rotation": attrs["rotation_deg"]},
        )
        if attrs["fixture_type"] not in missing:
            ref.add_auto_attribs(
                {
                    "FIXTURE_ID": attrs["fixture_id"],
                    "TYPE": attrs["fixture_name"],
                    "W": f"{attrs['width_m'] * _MM:.0f}",
                    "D": f"{attrs['depth_m'] * _MM:.0f}",
                }
            )
    _messy_text(msp, _title(model, floor), (60_000.0, -5_000.0), layers["annotation"], 1500)
    _messy_noise(msp, rng, noise_layers)
    _save(doc, path)


def _messy_noise(msp: BaseLayout, rng: Any, noise_layers: list[str]) -> None:
    """Dimension-like lines with values, memo TEXTs and stray construction lines on unrelated layers."""
    dim_layer, memo_layer, temp_layer = noise_layers
    for _ in range(int(rng.integers(6, 12))):
        horizontal = bool(rng.random() < 0.6)
        a = float(rng.integers(0, 100)) * 1000
        length = float(rng.integers(6, 24)) * 1000
        offset = -float(rng.integers(2, 4)) * 1000
        if horizontal:
            p0, p1 = (a, offset), (a + length, offset)
            ticks = [
                ((a, offset - 300), (a, offset + 300)),
                ((a + length, offset - 300), (a + length, offset + 300)),
            ]
            at = (a + length / 2, offset + 400)
        else:
            x = 120_000 - offset
            b = min(a, 60_000)
            p0, p1 = (x, b), (x, b + length)
            ticks = [((x - 300, b), (x + 300, b)), ((x - 300, b + length), (x + 300, b + length))]
            at = (x + 600, b + length / 2)
        msp.add_line(p0, p1, dxfattribs={"layer": dim_layer})
        for t0, t1 in ticks:
            msp.add_line(t0, t1, dxfattribs={"layer": dim_layer})
        _messy_text(msp, f"{length:.0f}", at, dim_layer, 300)
    for _ in range(int(rng.integers(4, 9))):
        at = (float(rng.uniform(-8_000, 128_000)), float(rng.uniform(-8_000, 88_000)))
        _messy_text(msp, str(rng.choice(_MEMOS)), at, memo_layer, 500)
    for _ in range(int(rng.integers(2, 5))):
        p0 = (float(rng.uniform(0, 120_000)), float(rng.uniform(0, 80_000)))
        p1 = (p0[0] + float(rng.uniform(-15_000, 15_000)), p0[1] + float(rng.uniform(-15_000, 15_000)))
        msp.add_line(p0, p1, dxfattribs={"layer": temp_layer})


# --- manifests -------------------------------------------------------------------------------------


def dxf_file_name(rev_id: str, floor: str) -> str:
    return f"flagship_{rev_id}_{floor}.dxf"


def manifest(model: SpatialModel, variant: str, floors: list[str] | None = None) -> dict[str, Any]:
    rev = model.revision
    selected = [f for f in model.floors if floors is None or f.code in floors]
    data: dict[str, Any] = {
        "facility": FACILITY_CODE,
        "facility_name": FACILITY_NAME,
        "revision": rev.rev_id,
        "kind": str(rev.kind),
        "parent_revision": rev.parent_rev_id,
        "effective_date": rev.effective_date,
        "variant": variant,
        "units": "m" if variant == "clean" else "mm",
        "levels": [
            {
                "floor": f.code,
                "ordinal": f.ordinal,
                "name": f.name,
                "file": dxf_file_name(rev.rev_id, f.code),
                "elevation_m": f.elevation_m,
                "height_m": f.height_m,
            }
            for f in selected
        ],
    }
    if variant == "clean":
        data["layers"] = dict(CLEAN_LAYERS)
    return data


def write_revision_drawings(
    model: SpatialModel, folder: Path, variant: str, seed: int, floors: list[str] | None = None
) -> dict[str, int]:
    """Write one DXF per floor plus ``manifest.json``; returns ``{file name: size in bytes}``."""
    folder.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for floor in [f.code for f in model.floors if floors is None or f.code in floors]:
        path = folder / dxf_file_name(model.revision.rev_id, floor)
        if variant == "clean":
            write_clean_floor(model, floor, path)
        elif variant == "messy":
            write_messy_floor(model, floor, path, seed)
        else:
            raise ValueError(f"unknown variant: {variant}")
        sizes[path.name] = path.stat().st_size
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest(model, variant, floors), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    sizes[manifest_path.name] = manifest_path.stat().st_size
    return sizes
