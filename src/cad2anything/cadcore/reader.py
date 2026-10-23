"""Read a revision folder (``manifest.json`` + one DXF per floor) into meter-scaled primitives.

The reader works at the file-format level only: DXF entity types, drawing units and text encodings. It knows
nothing about layer semantics. Coordinates are converted to meters from ``$INSUNITS`` (falling back to the
manifest ``units`` field and finally to the drawing extents). Texts are decoded (``\\U+xxxx`` escapes) and
checked for mojibake (CP949 bytes decoded with a western code page); repairable texts are fixed and flagged.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import path as ezpath
from ezdxf.lldxf.encoding import decode_dxf_unicode, has_dxf_unicode

INSUNITS_TO_M: dict[int, float] = {
    1: 0.0254,
    2: 0.3048,
    4: 0.001,
    5: 0.01,
    6: 1.0,
    7: 1000.0,
    10: 0.9144,
    14: 0.1,
}
UNIT_NAMES_TO_M: dict[str, float] = {
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "mm": 0.001,
    "millimeter": 0.001,
    "millimeters": 0.001,
    "cm": 0.01,
    "in": 0.0254,
    "inch": 0.0254,
    "ft": 0.3048,
    "feet": 0.3048,
}
HANGUL = re.compile(r"[\uac00-\ud7a3]")
_HIGH_RUN = re.compile(r"[\u0080-\u00ff\udc80-\udcff]{2,}")
_SUSPECT = re.compile(r"[\u0080-\u00ff]{3,}|[\ufffd\udc80-\udcff]")
_FLOOR_FILE = re.compile(r"_(B\d+F?|\d+F)\.dxf$", re.IGNORECASE)
_MTEXT_LINE_SPACING = 5.0 / 3.0


@dataclass
class Issue:
    """A QA finding; ``message`` is human-facing (Korean)."""

    severity: str
    code: str
    message: str
    floor: str | None = None
    layer: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "floor": self.floor,
            "layer": self.layer,
            "entity_ids": list(self.entity_ids),
        }
        if self.data:
            result["data"] = self.data
        return result


@dataclass(slots=True)
class Primitive:
    """One modelspace entity in meters. ``kind``: polyline, line, text, insert or other."""

    kind: str
    layer: str
    handle: str
    dxftype: str
    points: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False
    text: str = ""
    height: float = 0.0
    rotation: float = 0.0
    anchor: tuple[float, float] | None = None
    block: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    footprint: list[tuple[float, float]] | None = None
    text_status: str | None = None


@dataclass
class BlockInfo:
    """Block definition summary in drawing units (block coordinates)."""

    name: str
    footprint: list[tuple[float, float]] | None
    bbox: tuple[float, float, float, float] | None
    attdef_tags: list[str]
    entity_count: int


@dataclass
class FloorDrawing:
    floor: str
    path: Path
    dxfversion: str
    encoding: str
    codepage: str | None
    insunits: int
    scale: float
    unit_source: str
    layers: list[str]
    primitives: list[Primitive]
    blocks: dict[str, BlockInfo]
    issues: list[Issue]
    text_stats: dict[str, int]

    def by_layer(self) -> dict[str, list[Primitive]]:
        grouped: dict[str, list[Primitive]] = {}
        for prim in self.primitives:
            grouped.setdefault(prim.layer, []).append(prim)
        return grouped


@dataclass
class DrawingSet:
    rev_dir: Path
    manifest: dict[str, Any]
    levels: list[dict[str, Any]]
    floors: list[FloorDrawing]
    issues: list[Issue]


# --- text helpers ------------------------------------------------------------------------------------


def repair_text(text: str) -> tuple[str, str | None]:
    """Decode ``\\U+xxxx`` escapes and repair CP949 mojibake; status is None, ``fixed`` or ``suspect``."""
    if has_dxf_unicode(text):
        text = decode_dxf_unicode(text)
    if not _HIGH_RUN.search(text) and not _SUSPECT.search(text):
        return text, None
    for codec in ("cp1252", "latin-1"):
        try:
            candidate = text.encode(codec, errors="surrogateescape").decode("cp949")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if HANGUL.search(candidate) and not _SUSPECT.search(candidate):
            return candidate, "fixed"
    return (text, "suspect") if _SUSPECT.search(text) else (text, None)


def _char_units(text: str) -> float:
    return sum(1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.6 for ch in text)


def _rotate(dx: float, dy: float, degrees: float) -> tuple[float, float]:
    if not degrees:
        return dx, dy
    rad = math.radians(degrees)
    cos, sin = math.cos(rad), math.sin(rad)
    return dx * cos - dy * sin, dx * sin + dy * cos


def _text_center(entity: Any, text: str) -> tuple[tuple[float, float], float, float]:
    """Estimated center of a TEXT entity (drawing units), its height and rotation."""
    dxf = entity.dxf
    height = float(dxf.get("height", 1.0) or 1.0)
    width_factor = float(dxf.get("width", 1.0) or 1.0)
    rotation = float(dxf.get("rotation", 0.0) or 0.0)
    halign, valign = int(dxf.get("halign", 0) or 0), int(dxf.get("valign", 0) or 0)
    insert = dxf.get("insert")
    align = dxf.get("align_point")
    ix, iy = (float(insert.x), float(insert.y)) if insert is not None else (0.0, 0.0)
    if halign in (3, 5) and align is not None:
        angle = math.atan2(align.y - iy, align.x - ix)
        mx, my = (ix + align.x) / 2, (iy + align.y) / 2
        return (mx - math.sin(angle) * height / 2, my + math.cos(angle) * height / 2), height, rotation
    rx, ry = (float(align.x), float(align.y)) if (halign or valign) and align is not None else (ix, iy)
    width = _char_units(text) * height * width_factor
    dx = {0: width / 2, 1: 0.0, 2: -width / 2, 4: 0.0}.get(halign, 0.0)
    dy = (
        0.0 if halign == 4 else {0: height / 2, 1: height / 2, 2: 0.0, 3: -height / 2}.get(valign, height / 2)
    )
    dx, dy = _rotate(dx, dy, rotation)
    return (rx + dx, ry + dy), height, rotation


def _mtext_center(entity: Any, text: str) -> tuple[tuple[float, float], float, float]:
    dxf = entity.dxf
    height = float(dxf.get("char_height", 1.0) or 1.0)
    insert = dxf.get("insert")
    ix, iy = (float(insert.x), float(insert.y)) if insert is not None else (0.0, 0.0)
    lines = text.split("\n") or [""]
    width = float(dxf.get("width", 0.0) or 0.0)
    if width <= 0:
        width = max(_char_units(line) for line in lines) * height
    total = height * (1 + (len(lines) - 1) * _MTEXT_LINE_SPACING)
    point = int(dxf.get("attachment_point", 1) or 1)
    col, row = (point - 1) % 3, (point - 1) // 3
    dx = (width / 2, 0.0, -width / 2)[col]
    dy = (-total / 2, 0.0, total / 2)[row]
    try:
        rotation = float(entity.get_rotation())
    except Exception:
        rotation = 0.0
    dx, dy = _rotate(dx, dy, rotation)
    return (ix + dx, iy + dy), height, rotation


# --- units -------------------------------------------------------------------------------------------


def resolve_scale(insunits: int, manifest_units: str | None, extent: float | None) -> tuple[float, str]:
    """Meters per drawing unit and the source used (``header``, ``manifest`` or ``extent``)."""
    if insunits in INSUNITS_TO_M:
        return INSUNITS_TO_M[insunits], "header"
    if manifest_units and manifest_units.strip().lower() in UNIT_NAMES_TO_M:
        return UNIT_NAMES_TO_M[manifest_units.strip().lower()], "manifest"
    if extent is not None and extent > 5_000:
        return 0.001, "extent"
    return 1.0, "extent"


# --- blocks ------------------------------------------------------------------------------------------


def _shoelace(points: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, [*points[1:], points[0]], strict=True):
        total += x0 * y1 - x1 * y0
    return abs(total) / 2


def _block_info(doc: Any, name: str) -> BlockInfo:
    block = doc.blocks.get(name)
    if block is None:
        return BlockInfo(name, None, None, [], 0)
    xs: list[float] = []
    ys: list[float] = []
    footprint: list[tuple[float, float]] | None = None
    best = 0.0
    tags: list[str] = []
    count = 0
    for entity in block:
        count += 1
        kind = entity.dxftype()
        if kind == "LWPOLYLINE":
            pts = [(float(x), float(y)) for x, y in entity.get_points("xy")]
            xs.extend(p[0] for p in pts)
            ys.extend(p[1] for p in pts)
            area = _shoelace(pts) if len(pts) >= 3 else 0.0
            if entity.closed and area > best:
                best, footprint = area, pts
        elif kind == "LINE":
            for v in (entity.dxf.start, entity.dxf.end):
                xs.append(float(v.x))
                ys.append(float(v.y))
        elif kind in ("CIRCLE", "ARC"):
            c, r = entity.dxf.center, float(entity.dxf.radius)
            xs.extend((c.x - r, c.x + r))
            ys.extend((c.y - r, c.y + r))
        elif kind == "ATTDEF":
            tags.append(str(entity.dxf.tag))
    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
    if footprint is None and bbox is not None and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
        x0, y0, x1, y1 = bbox
        footprint = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return BlockInfo(name, footprint, bbox, tags, count)


# --- floor reader ------------------------------------------------------------------------------------


class _Names:
    """Cache of decoded/repaired names (layers, blocks, tags) with mojibake bookkeeping."""

    def __init__(self) -> None:
        self.cache: dict[str, str] = {}
        self.fixed = 0
        self.suspect = 0

    def __call__(self, raw: str) -> str:
        if raw not in self.cache:
            value, status = repair_text(raw)
            self.fixed += status == "fixed"
            self.suspect += status == "suspect"
            self.cache[raw] = value
        return self.cache[raw]


def _polyline_points(entity: Any, kind: str) -> list[tuple[float, float]]:
    if kind == "LWPOLYLINE":
        extrusion = entity.dxf.get("extrusion")
        if extrusion is not None and (abs(extrusion.x) > 1e-9 or abs(extrusion.y) > 1e-9 or extrusion.z < 0):
            return [(float(v.x), float(v.y)) for v in entity.vertices_in_wcs()]
        raw = list(entity.get_points("xyb"))
        if any(abs(float(b)) > 1e-12 for _, _, b in raw):
            return [(float(v.x), float(v.y)) for v in ezpath.make_path(entity).flattening(distance=0.01)]
        return [(float(x), float(y)) for x, y, _ in raw]
    return [(float(v.x), float(v.y)) for v in entity.points()]


def _scaled(points: list[tuple[float, float]], scale: float) -> list[tuple[float, float]]:
    return [(x * scale, y * scale) for x, y in points]


def _read_doc(path: Path) -> tuple[Any, list[Issue]]:
    try:
        return ezdxf.readfile(path), []
    except ezdxf.DXFStructureError:
        from ezdxf import recover

        doc, auditor = recover.readfile(path)
        message = f"DXF 구조 오류를 복구하여 읽었다 (오류 {len(auditor.errors)}건)."
        return doc, [Issue("warning", "dxf_recovered", message)]


def _extent(msp: Any) -> float | None:
    values: list[float] = []
    for entity in msp.query("LWPOLYLINE LINE"):
        if entity.dxftype() == "LINE":
            values.extend((abs(entity.dxf.start.x), abs(entity.dxf.end.x)))
        else:
            values.extend(abs(float(x)) for x, _ in entity.get_points("xy"))
        if len(values) > 200:
            break
    return max(values) if values else None


def _other_location(entity: Any, scale: float) -> list[tuple[float, float]]:
    for key in ("center", "insert", "defpoint", "location", "start"):
        value = entity.dxf.get(key)
        if value is not None:
            return [(float(value.x) * scale, float(value.y) * scale)]
    return []


def read_floor(path: Path, floor: str, manifest_units: str | None = None) -> FloorDrawing:
    """Read one floor DXF into meter-scaled primitives."""
    path = Path(path)
    doc, issues = _read_doc(path)
    msp = doc.modelspace()
    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    codepage = doc.header.get("$DWGCODEPAGE")
    scale, unit_source = resolve_scale(insunits, manifest_units, None)
    if unit_source != "header":
        scale, unit_source = resolve_scale(insunits, manifest_units, _extent(msp))
        issues.append(
            Issue(
                "warning",
                "units_inferred",
                f"$INSUNITS={insunits} 값을 해석할 수 없어 단위를 {unit_source} 기준으로 추정했다 "
                f"(1 도면단위 = {scale:g} m).",
                floor,
            )
        )
    names = _Names()
    blocks: dict[str, BlockInfo] = {}
    prims: list[Primitive] = []
    stats = {"texts": 0, "hangul_texts": 0, "mojibake_fixed": 0, "mojibake_suspect": 0}

    def note_text(value: str, status: str | None) -> None:
        stats["texts"] += 1
        stats["hangul_texts"] += bool(HANGUL.search(value))
        stats["mojibake_fixed"] += status == "fixed"
        stats["mojibake_suspect"] += status == "suspect"

    for entity in msp:
        kind = entity.dxftype()
        layer = names(str(entity.dxf.get("layer", "0")))
        handle = str(entity.dxf.get("handle", ""))
        if kind in ("LWPOLYLINE", "POLYLINE"):
            if kind == "POLYLINE" and not (entity.is_2d_polyline or entity.is_3d_polyline):
                prims.append(Primitive("other", layer, handle, kind))
                continue
            pts = _scaled(_polyline_points(entity, kind), scale)
            closed = bool(entity.closed if kind == "LWPOLYLINE" else entity.is_closed)
            prims.append(Primitive("polyline", layer, handle, kind, points=pts, closed=closed))
        elif kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            pts = _scaled([(float(start.x), float(start.y)), (float(end.x), float(end.y))], scale)
            prims.append(Primitive("line", layer, handle, kind, points=pts))
        elif kind in ("TEXT", "MTEXT"):
            raw = entity.plain_text() if kind == "TEXT" else entity.plain_text(split=False)
            value, status = repair_text(str(raw))
            value = value.strip()
            if not value:
                continue
            note_text(value, status)
            center, height, rotation = (_text_center if kind == "TEXT" else _mtext_center)(entity, value)
            anchor = (center[0] * scale, center[1] * scale)
            prims.append(
                Primitive(
                    "text",
                    layer,
                    handle,
                    kind,
                    points=[anchor],
                    text=value,
                    height=height * scale,
                    rotation=rotation,
                    anchor=anchor,
                    text_status=status,
                )
            )
        elif kind == "INSERT":
            raw_name = str(entity.dxf.name)
            if raw_name not in blocks:
                blocks[raw_name] = _block_info(doc, raw_name)
            info = blocks[raw_name]
            attributes: dict[str, str] = {}
            for attrib in entity.attribs:
                value, status = repair_text(str(attrib.dxf.text))
                attributes[names(str(attrib.dxf.tag)).upper()] = value.strip()
                if status:
                    note_text(value, status)
            footprint = None
            if info.footprint:
                local = [(x, y, 0.0) for x, y in info.footprint]
                world = entity.matrix44().transform_vertices(local)
                footprint = [(float(v.x) * scale, float(v.y) * scale) for v in world]
            insert = entity.dxf.insert
            point = (float(insert.x) * scale, float(insert.y) * scale)
            prims.append(
                Primitive(
                    "insert",
                    layer,
                    handle,
                    kind,
                    points=[point],
                    rotation=float(entity.dxf.get("rotation", 0.0) or 0.0),
                    anchor=point,
                    block=names(raw_name),
                    attributes=attributes,
                    footprint=footprint,
                )
            )
        else:
            prims.append(Primitive("other", layer, handle, kind, points=_other_location(entity, scale)))
    layers = [names(str(layer.dxf.name)) for layer in doc.layers]
    stats["mojibake_fixed"] += names.fixed
    stats["mojibake_suspect"] += names.suspect
    if stats["mojibake_fixed"]:
        issues.append(
            Issue(
                "warning",
                "mojibake_fixed",
                f"코드페이지가 맞지 않는 한글 문자열 {stats['mojibake_fixed']}건을 CP949로 다시 해석했다.",
                floor,
            )
        )
    if stats["mojibake_suspect"]:
        issues.append(
            Issue(
                "error",
                "mojibake_suspect",
                f"깨진 것으로 보이는 문자열 {stats['mojibake_suspect']}건을 복구하지 못했다.",
                floor,
            )
        )
    return FloorDrawing(
        floor=floor,
        path=path,
        dxfversion=str(doc.dxfversion),
        encoding=str(doc.encoding),
        codepage=str(codepage) if codepage else None,
        insunits=insunits,
        scale=scale,
        unit_source=unit_source,
        layers=layers,
        primitives=prims,
        blocks={names(name): info for name, info in blocks.items()},
        issues=issues,
        text_stats=stats,
    )


# --- revision reader ---------------------------------------------------------------------------------


def read_manifest(rev_dir: Path) -> dict[str, Any]:
    """``manifest.json`` of a revision folder, or an empty dict when missing."""
    path = Path(rev_dir) / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def discover_levels(rev_dir: Path) -> list[dict[str, Any]]:
    """Levels from DXF file names (``..._3F.dxf``, ``..._B1.dxf``) when the manifest has none."""
    levels = []
    for path in sorted(Path(rev_dir).glob("*.dxf")):
        match = _FLOOR_FILE.search(path.name)
        if match:
            code = match.group(1).upper()
            if code.startswith("B") and code.endswith("F"):
                code = code[:-1]
            levels.append({"floor": code, "file": path.name})
    return levels


def read_revision(rev_dir: Path, floors: Sequence[str] | None = None, units: str | None = None) -> DrawingSet:
    """Read the manifest and every floor DXF (optionally only ``floors``); ``units`` is the fallback when the
    manifest has no ``units`` (for example a drawing profile's units)."""
    rev_dir = Path(rev_dir)
    manifest = read_manifest(rev_dir)
    issues: list[Issue] = []
    levels = list(manifest.get("levels") or [])
    if not levels:
        levels = discover_levels(rev_dir)
        issues.append(
            Issue("warning", "manifest_missing", "매니페스트 levels가 없어 파일명에서 층을 추정했다.")
        )
    drawings: list[FloorDrawing] = []
    for level in levels:
        floor = str(level["floor"])
        if floors is not None and floor not in floors:
            continue
        path = rev_dir / str(level["file"])
        if not path.is_file():
            issues.append(Issue("error", "file_missing", f"{floor} 도면 파일이 없다: {path.name}", floor))
            continue
        drawings.append(read_floor(path, floor, manifest.get("units") or units))
    return DrawingSet(rev_dir, manifest, levels, drawings, issues)
