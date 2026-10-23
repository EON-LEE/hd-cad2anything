"""Customer-style sample drawings and drawing profile for the upload pipeline (synthetic, fictional).

``write_customer_floor`` draws one floor of a model in conventions that differ from the Atlas default
(docs/specs/cad-conventions.md): no ``$INSUNITS`` (coordinates in millimeters), Korean layer names, unit
labels such as ``6층 C-05`` with the brand name on the next line (``6층 C-05 공실`` for vacancies), zone
labels ``1구역 스포츠``, vertical links ``E/S-A``/``E/V-1``/``계단실-1``, openings ``출입구1`` and fixture
blocks with Korean names and no attributes. ``CUSTOMER_PROFILE_YAML`` maps these conventions back to the
canonical codes (``6F-C05``, ``6F-Z1``, ``ESC-A``, ``ENT-1``).

Usage::

    uv run --no-sync python -m cad2anything.mockgen.upload_samples --root data/workspace --out data/samples/uploads
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry

from cad2anything.catalog import FACILITY_NAME, FIXTURE_TYPES
from cad2anything.mockgen.dxf_writer import (
    _MM,
    MESSY_BLOCK_NAMES,
    _inward_point,
    _label_point,
    _messy_blocks,
    _new_doc,
    _rings,
    _save,
    _scaled,
    _wall_rings,
)
from cad2anything.model import EntityKind, SpatialModel

CUSTOMER_PROFILE_ID = "customer_style"
CUSTOMER_LAYERS: dict[str, str] = {
    "exterior": "외곽선",
    "wall": "벽체",
    "unit": "매장_경계",
    "unit_label": "매장_명칭",
    "zone": "존_경계",
    "zone_label": "존_명칭",
    "walkway": "보행통로",
    "escalator": "에스컬레이터",
    "elevator": "승강기",
    "stair": "계단",
    "opening": "출입구",
    "amenity": "편의시설",
    "amenity_label": "편의시설_명칭",
    "fixture": "집기",
    "annotation": "도면_제목",
}
LINK_PREFIXES: dict[str, str] = {"ESC": "E/S", "EV": "E/V", "ST": "계단실"}
AMENITY_LABELS: dict[str, str] = {
    "RESTROOM": "화장실",
    "INFO": "안내데스크",
    "ATM": "ATM",
    "NURSING": "수유실",
    "LOUNGE": "고객라운지",
}
_UNIT_CODE = re.compile(r"^(?P<floor>B\d+|\d+F)-(?P<band>[A-Z])(?P<num>\d{2})(?P<suffix>[A-Z]?M?)$")
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)

CUSTOMER_PROFILE_YAML = r"""# 고객사 스타일 도면 규약 예시(합성). docs/specs/drawing-profile.md 참고.
# 이 규약의 표기를 Atlas 표준 코드(6F-C05, 6F-Z1, ESC-A, ENT-1)로 바꾼다.
profile_id: customer_style
title: 고객사 스타일 도면(예시)
description: 단위 미지정(mm) 도면, 한글 레이어 이름, "6층 C-05" 매장 번호와 다음 줄 브랜드명을 쓰는 규약 예시다.
# 도면 헤더에 단위가 없을 때 쓰는 단위다.
units: mm
# 의미 → 레이어 이름. 의미는 CAD 파이프라인의 레이어 의미 16개 중 하나다.
layers:
  exterior: [외곽선]
  wall: [벽체]
  unit: [매장_경계]
  unit_label: [매장_명칭]
  zone: [존_경계]
  zone_label: [존_명칭]
  walkway: [보행통로]
  escalator: [에스컬레이터]
  elevator: [승강기]
  stair: [계단]
  opening: [출입구]
  amenity: [편의시설]
  amenity_label: [편의시설_명칭]
  fixture: [집기]
  annotation: [도면_제목]
# 층 표기("6층", "지하1층")는 자동으로 6F, B1로 바뀐다. 특이한 층 이름만 floor_aliases에 적는다(예: "M층: 2F").
unit_code:
  # "6층 C-05", "지하1층 S-02A" -> 6F-C05, B1-S02A
  pattern: '(?P<floor>지하\s*\d+\s*층|\d+\s*층)\s*(?P<band>[NSEWCP])-(?P<num>\d{2})(?P<suffix>[A-Z]?M?)(?![A-Za-z0-9])'
  # 층 없이 "C-05"로 시작하는 라벨은 도면의 층을 붙인다.
  bare: '(?P<band>[NSEWCP])-(?P<num>\d{2})(?P<suffix>[A-Z]?M?)'
  format: '{floor}-{band}{num}{suffix}'
zone:
  # "1구역 스포츠" -> 6F-Z1, 존 이름 "스포츠"
  pattern: '^\s*(?P<num>\d+)\s*구역\s*(?P<name>.*)$'
  format: '{floor}-Z{num}'
vertical_link:
  pattern: '(?P<id>E/S-[A-Z0-9]{1,3}|E/V-[A-Z0-9]{1,3}|계단실-[A-Z0-9]{1,3})'
  # 수직동선 ID는 모든 층에서 같아야 하므로 표준 ID로 바꾼다.
  map:
    E/S-A: ESC-A
    E/S-B: ESC-B
    E/V-1: EV-1
    E/V-2: EV-2
    계단실-1: ST-1
    계단실-2: ST-2
  # 표에 없는 ID도 접두어로 종류를 안다.
  types:
    E/S: escalator
    E/V: elevator
    계단실: stair
opening:
  # "출입구1 정문" -> ENT-1
  pattern: '출입구\s*(?P<num>B?\d+)'
  format: 'ENT-{num}'
vacancy:
  # 기본 공실 표기(공실, VACANT, 임대중, 임대예정)에 더한다.
  words: [빈 매장]
"""


# --- customer notation ------------------------------------------------------------------------------------


def customer_floor(floor: str) -> str:
    """``6F`` -> ``6층``, ``B1`` -> ``지하1층``."""
    return f"지하{floor[1:]}층" if floor.startswith("B") else f"{floor.removesuffix('F')}층"


def customer_unit_code(unit_code: str) -> str:
    """``6F-C05`` -> ``6층 C-05``, ``6F-S02A`` -> ``6층 S-02A``."""
    match = _UNIT_CODE.match(unit_code)
    if not match:
        return unit_code
    return f"{customer_floor(match['floor'])} {match['band']}-{match['num']}{match['suffix']}"


def customer_zone_label(zone_code: str, zone_name: str) -> str:
    """``6F-Z1``, ``스포츠`` -> ``1구역 스포츠``."""
    return f"{zone_code.rsplit('Z', 1)[-1]}구역 {zone_name}".strip()


def customer_link_id(link_id: str) -> str:
    """``ESC-A`` -> ``E/S-A``, ``EV-1`` -> ``E/V-1``, ``ST-1`` -> ``계단실-1``."""
    prefix, _, rest = link_id.partition("-")
    return f"{LINK_PREFIXES[prefix]}-{rest}" if prefix in LINK_PREFIXES and rest else link_id


def customer_opening_label(opening_id: str, name: str) -> str:
    """``ENT-1``, ``정문`` -> ``출입구1 정문``."""
    prefix, _, rest = opening_id.partition("-")
    label = f"출입구{rest}" if prefix == "ENT" and rest else opening_id
    return f"{label} {name}".strip()


# --- writer -----------------------------------------------------------------------------------------------


def write_customer_floor(model: SpatialModel, floor: str, path: Path) -> None:
    """One floor of ``model`` as a customer-style DXF (R2013, no units, millimeters)."""
    doc = _new_doc("R2013")
    doc.header["$INSUNITS"] = 0
    doc.header["$MEASUREMENT"] = 1
    doc.styles.add("KR", font="malgun.ttf")
    for name in CUSTOMER_LAYERS.values():
        doc.layers.add(name)
    _messy_blocks(doc, set(FIXTURE_TYPES))
    msp = doc.modelspace()

    def poly(geometry: BaseGeometry, semantic: str) -> None:
        for ring in _rings(geometry):
            msp.add_lwpolyline(
                _scaled(ring, _MM), close=True, dxfattribs={"layer": CUSTOMER_LAYERS[semantic]}
            )

    def text(value: str, at: tuple[float, float], semantic: str, height_mm: float) -> None:
        entity = msp.add_text(
            value, dxfattribs={"layer": CUSTOMER_LAYERS[semantic], "height": height_mm, "style": "KR"}
        )
        entity.set_placement(_scaled([at], _MM)[0], align=TextEntityAlignment.MIDDLE_CENTER)

    for exterior in model.by_kind(EntityKind.EXTERIOR, floor):
        poly(exterior.geometry, "exterior")
    for ring in _wall_rings(model, floor):
        points = _scaled(ring, _MM)
        for start, end in zip(points, points[1:] + points[:1], strict=True):
            msp.add_line(start, end, dxfattribs={"layer": CUSTOMER_LAYERS["wall"]})
    for walkway in model.by_kind(EntityKind.WALKWAY, floor):
        poly(walkway.geometry, "walkway")
    for link in model.by_kind(EntityKind.VERTICAL_LINK, floor):
        semantic = str(link.attributes["link_type"])
        poly(link.geometry, semantic)
        text(customer_link_id(str(link.attributes["link_id"])), _label_point(link.geometry), semantic, 450)
    for amenity in model.by_kind(EntityKind.AMENITY, floor):
        poly(amenity.geometry, "amenity")
        kind = str(amenity.attributes.get("amenity_type"))
        text(AMENITY_LABELS.get(kind, kind), _label_point(amenity.geometry), "amenity_label", 450)
    for opening in model.by_kind(EntityKind.OPENING, floor):
        assert isinstance(opening.geometry, LineString)
        start, end = _scaled([opening.geometry.coords[0], opening.geometry.coords[-1]], _MM)
        msp.add_line(start, end, dxfattribs={"layer": CUSTOMER_LAYERS["opening"]})
        name = str(opening.attributes.get("name_ko") or "")
        label = customer_opening_label(str(opening.attributes["opening_id"]), name)
        text(label, _inward_point(opening.geometry), "opening", 600)
    for zone in model.by_kind(EntityKind.ZONE, floor):
        poly(zone.geometry, "zone")
        x, y = _label_point(zone.geometry)
        label = customer_zone_label(
            str(zone.attributes["zone_code"]), str(zone.attributes.get("zone_name") or "")
        )
        text(label, (x, round(y + 1.6, 3)), "zone_label", 800)
    for unit in model.by_kind(EntityKind.UNIT, floor):
        poly(unit.geometry, "unit")
        code = customer_unit_code(str(unit.attributes["unit_code"]))
        brand = unit.attributes.get("brand_name")
        point = _label_point(unit.geometry)
        if unit.attributes.get("vacant") or not brand:
            text(f"{code} 공실", point, "unit_label", 500)
            continue
        entity = msp.add_mtext(
            f"{code}\\P{brand}",
            dxfattribs={"layer": CUSTOMER_LAYERS["unit_label"], "char_height": 500, "style": "KR"},
        )
        entity.set_location(_scaled([point], _MM)[0], attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
    for fixture in model.by_kind(EntityKind.FIXTURE, floor):
        center = fixture.geometry.centroid
        msp.add_blockref(
            MESSY_BLOCK_NAMES[str(fixture.attributes["fixture_type"])],
            (round(center.x * _MM, 1), round(center.y * _MM, 1)),
            dxfattribs={"layer": CUSTOMER_LAYERS["fixture"], "rotation": fixture.attributes["rotation_deg"]},
        )
    text(f"{FACILITY_NAME} {customer_floor(floor)} 평면도", (60.0, -4.0), "annotation", 1500)
    _save(doc, path)


def customer_floor_bytes(model: SpatialModel, floor: str, scratch: Path) -> bytes:
    """DXF bytes of ``write_customer_floor`` (written through ``scratch``, which is removed afterwards)."""
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_customer_floor(model, floor, scratch)
        return scratch.read_bytes()
    finally:
        scratch.unlink(missing_ok=True)


def write_zip(path: Path, members: dict[str, bytes]) -> int:
    """Reproducible ZIP (fixed timestamps, deflate level 9); returns its size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, compresslevel=9)
    return path.stat().st_size


def write_samples(model: SpatialModel, out_dir: Path, floor: str = "6F") -> dict[str, int]:
    """``customer_style_{floor}.zip`` and ``profiles/customer_style.yaml`` under ``out_dir``.

    The folder's ``README.md`` is maintained by hand because it also covers other upload samples."""
    out_dir = Path(out_dir)
    data = customer_floor_bytes(model, floor, out_dir / f".{floor}.dxf.tmp")
    files = {
        f"customer_style_{floor}.zip": write_zip(
            out_dir / f"customer_style_{floor}.zip", {f"{floor}.dxf": data}
        )
    }
    path = out_dir / "profiles" / f"{CUSTOMER_PROFILE_ID}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CUSTOMER_PROFILE_YAML, encoding="utf-8", newline="\n")
    files[f"profiles/{CUSTOMER_PROFILE_ID}.yaml"] = path.stat().st_size
    return files


def load_model(root: Path, rev: str) -> SpatialModel:
    """Model of ``rev`` from a workspace folder (``models/{rev}.json``) without touching the workspace."""
    path = Path(root) / "models" / f"{rev}.json"
    return SpatialModel.from_dict(json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write customer-style upload samples (ZIP and drawing profile)."
    )
    parser.add_argument("--root", type=Path, default=Path("data/workspace"), help="workspace with models/")
    parser.add_argument("--out", type=Path, default=Path("data/samples/uploads"), help="output folder")
    parser.add_argument("--rev", default="R2", help="source revision")
    parser.add_argument("--floor", default="6F", help="floor to draw")
    args = parser.parse_args(argv)
    files = write_samples(load_model(args.root, args.rev), args.out, args.floor)
    print(json.dumps({"rev": args.rev, "floor": args.floor, "files": files}, ensure_ascii=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
