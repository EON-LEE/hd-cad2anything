"""Drawing profiles: the default reproduces the synthetic conventions; a customer profile reads another notation."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import yaml
from shapely.geometry import box

from cad2anything.cadcore.classify import LayerFeatures, ManifestClassifier
from cad2anything.cadcore.labels import (
    find_unit_code,
    label_units,
    link_info,
    parse_opening_label,
    parse_zone_label,
    text_kind,
)
from cad2anything.cadcore.pipeline import parse_revision
from cad2anything.cadcore.profile import (
    DEFAULT_PROFILE,
    active_profile,
    load_profile,
    merge_layers,
    normalize_floor,
    profile_to_dict,
    use_profile,
    validate_profile,
)
from cad2anything.cadcore.reader import Primitive, read_revision
from cad2anything.mockgen import brands_to_rows
from cad2anything.mockgen.dxf_writer import manifest, write_clean_floor
from cad2anything.mockgen.upload_samples import (
    CUSTOMER_PROFILE_YAML,
    customer_unit_code,
    write_customer_floor,
    write_samples,
)
from cad2anything.model import EntityKind, SpatialModel

FLOOR = "6F"
SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples" / "uploads"
LABEL_TEXTS = (
    "3F-C05 (공실)",
    "6F-S02A",
    "3F-Z1 여성 컨템포러리",
    "Z3 잡화",
    "에스컬레이터 ESC-B",
    "엘리베이터 EV-2",
    "계단",
    "ENT-1 정문",
    "WC 화장실",
    "12000",
    "MD팀 확인 요망",
    "6층 C-05",
    "1구역 스포츠",
    "E/S-A",
)


@pytest.fixture(scope="module")
def r2(synthetic_store) -> SpatialModel:
    return synthetic_store["models"]["R2"]


@pytest.fixture(scope="module")
def brand_rows(synthetic_store) -> list[dict]:
    return brands_to_rows(synthetic_store["brands"])


@pytest.fixture(scope="module")
def customer_profile():
    profile, errors, warnings = validate_profile(yaml.safe_load(CUSTOMER_PROFILE_YAML))
    assert profile is not None and not errors and not warnings
    return profile


@pytest.fixture(scope="module")
def customer_dir(tmp_path_factory, r2) -> Path:
    folder = tmp_path_factory.mktemp("customer-style")
    write_customer_floor(r2, FLOOR, folder / f"{FLOOR}.dxf")
    levels = [{"floor": FLOOR, "file": f"{FLOOR}.dxf"}]
    (folder / "manifest.json").write_text(json.dumps({"revision": "R3", "levels": levels}), encoding="utf-8")
    return folder


def _label_results(texts: tuple[str, ...]) -> list[tuple]:
    return [
        (
            text_kind(t),
            find_unit_code(t, "6F"),
            parse_zone_label(t, "6F"),
            link_info(t),
            parse_opening_label(t),
        )
        for t in texts
    ]


# --- floors, activation, validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4층", "4F"),
        ("4F", "4F"),
        ("4f", "4F"),
        ("지하1층", "B1"),
        ("지하 2 층", "B2"),
        ("B1F", "B1"),
        ("B1", "B1"),
        ("b2", "B2"),
        ("10층", "10F"),
        ("지상1층", "1F"),
        ("R3", None),
        ("6", None),
        ("", None),
    ],
)
def test_floor_normalization(text: str, expected: str | None) -> None:
    assert normalize_floor(text) == expected


def test_floor_aliases_and_bare_numbers() -> None:
    assert normalize_floor("6", allow_bare=True) == "6F"
    assert normalize_floor("M 층", {"M층": "2F"}) == "2F"


def test_default_profile_is_active_and_round_trips() -> None:
    assert active_profile() is DEFAULT_PROFILE
    assert text_kind("3F-C05 (공실)") == "unit_combined" and find_unit_code("3F-C02 Brand") == (
        "3F-C02",
        "Brand",
    )
    assert find_unit_code("6층 C-05") == (None, "6층 C-05")
    reloaded = load_profile(profile_to_dict(DEFAULT_PROFILE))
    expected = _label_results(LABEL_TEXTS)
    with use_profile(reloaded):
        assert _label_results(LABEL_TEXTS) == expected
    assert reloaded.is_nonstandard("3F-C05X1") and not reloaded.is_nonstandard("3F-C05")


def test_use_profile_switches_label_parsing(customer_profile) -> None:
    with use_profile(customer_profile) as active:
        assert active is customer_profile and active_profile() is customer_profile
        assert find_unit_code("6층 C-05\nVesane Femme", "6F") == ("6F-C05", "Vesane Femme")
        assert find_unit_code("지하1층 S-02A 공실") == ("B1-S02A", "공실")
        assert text_kind("6층 C-05") == "unit_code" and text_kind("1구역 스포츠") == "zone_code"
        assert parse_zone_label("1구역 스포츠", "6F") == ("6F-Z1", "스포츠")
        assert link_info("E/S-A") == ("ESC-A", "escalator")
        assert link_info("E/S-C") == ("E/S-C", "escalator")
        assert link_info("계단실-2") == ("ST-2", "stair")
        assert parse_opening_label("출입구1 정문") == ("ENT-1", "정문")
        polygons = [box(0, 0, 6, 10), box(6, 0, 12, 10), box(12, 0, 18, 10)]

        def text(value: str, x: float) -> Primitive:
            return Primitive(
                "text", "T", value, "TEXT", points=[(x, 5)], text=value, height=0.5, anchor=(x, 5)
            )

        texts = [text("C-01 Brand A", 3), text("6층 C-02 빈 매장", 9), text("6층 C-03 공실", 15)]
        labels, issues = label_units(polygons, texts, "6F")
        assert [(lab.unit_code, lab.brand_name, lab.vacant) for lab in labels] == [
            ("6F-C01", "Brand A", False),
            ("6F-C02", None, True),
            ("6F-C03", None, True),
        ]
        assert not issues and not customer_profile.is_nonstandard("6F-C01")
    assert active_profile() is DEFAULT_PROFILE
    assert customer_unit_code("6F-S02A") == "6층 S-02A" and customer_unit_code("B1-C05") == "지하1층 C-05"


def test_validate_profile_reports_errors_in_korean() -> None:
    assert validate_profile(["not", "a", "mapping"])[1] == ["프로필은 키-값 객체(YAML 매핑)여야 합니다."]
    profile, errors, _ = validate_profile({"profile_id": "Bad ID"})
    assert profile is None and any("profile_id" in e for e in errors) and any("title" in e for e in errors)
    cases = {
        "정규식 오류": {"unit_code": {"pattern": "(?P<floor>"}},
        "이름 붙은 그룹": {"unit_code": {"pattern": r"(?P<band>[A-Z])-\d+", "format": "{floor}-{band}{num}"}},
        "빈 문자열": {"unit_code": {"pattern": r"\d*"}},
        "알 수 없습니다": {"layers": {"shop": ["매장"]}},
        "여러 의미": {"layers": {"unit": ["X"], "walkway": ["X"]}},
        "zone.pattern": {"zone": {"pattern": r"^(?P<num>\d+)구역"}},
        "수직동선 종류": {"vertical_link": {"pattern": "E/S-[A-Z]", "types": {"E/S": "moving_walk"}}},
        "units": {"units": "furlong"},
        "자리표시자": {"opening": {"pattern": r"출입구(?P<num>\d+)", "format": "ENT-{num:02d}"}},
    }
    for needle, extra in cases.items():
        profile, errors, _ = validate_profile({"profile_id": "p1", "title": "t", **extra})
        assert profile is None and any(needle in e for e in errors), (needle, errors)
    profile, errors, warnings = validate_profile({"profile_id": "p1", "title": "t", "colour": "blue"})
    assert profile is not None and not errors and warnings == ["알 수 없는 항목은 무시합니다: colour"]
    with pytest.raises(ValueError, match="도면 규약 프로필이 올바르지 않습니다"):
        load_profile({"title": "no id"})


def test_layers_by_floor_win_over_layers() -> None:
    classifier = ManifestClassifier({"layers": {"unit": ["A"]}, "layers_by_floor": {"6F": {"walkway": "A"}}})
    features = [LayerFeatures("6F", "A", {}), LayerFeatures("5F", "A", {}), LayerFeatures("6F", "B", {})]
    decisions = classifier.classify(features)
    assert [d.semantic if d else None for d in decisions] == ["walkway", "unit", None]
    assert "layers_by_floor" in decisions[0].evidence[0] and decisions[1].evidence == [
        "매니페스트 layers 매핑: unit"
    ]
    assert merge_layers({"unit": ["A", "B"]}, {"walkway": "B"}) == {"unit": ["A"], "walkway": ["B"]}


# --- parsing --------------------------------------------------------------------------------------------------


def test_default_profile_parses_clean_drawings_as_before(tmp_path, r2, brand_rows) -> None:
    write_clean_floor(r2, FLOOR, tmp_path / f"flagship_R2_{FLOOR}.dxf")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest(r2, "clean", [FLOOR])), encoding="utf-8")
    model, report = parse_revision(tmp_path, brand_rows, relations="basic")
    explicit, _ = parse_revision(tmp_path, brand_rows, relations="basic", profile=DEFAULT_PROFILE)
    assert model.to_dict() == explicit.to_dict() and report.stats["profile_id"] == "default"
    truth = {u.attributes["unit_code"]: u.attributes["brand_id"] for u in r2.units(FLOOR)}
    assert {u.attributes["unit_code"]: u.attributes["brand_id"] for u in model.units()} == truth
    assert not any(i.code == "unit_code_nonstandard" for i in report.issues)


def test_customer_profile_reads_customer_drawing(customer_dir, customer_profile, r2, brand_rows) -> None:
    model, report = parse_revision(customer_dir, brand_rows, relations="basic", profile=customer_profile)
    truth = {u.attributes["unit_code"]: u for u in r2.units(FLOOR)}
    parsed = {u.attributes["unit_code"]: u for u in model.units()}
    assert set(parsed) == set(truth)
    for code, unit in parsed.items():
        assert unit.geometry.area == pytest.approx(truth[code].geometry.area, rel=0.005)
        assert unit.attributes["brand_id"] == truth[code].attributes["brand_id"]
        assert bool(unit.attributes["vacant"]) == bool(truth[code].attributes.get("vacant"))
    zones = {z.attributes["zone_code"]: z.attributes["zone_name"] for z in model.by_kind(EntityKind.ZONE)}
    assert zones == {
        z.attributes["zone_code"]: z.attributes["zone_name"] for z in r2.by_kind(EntityKind.ZONE, FLOOR)
    }
    links = sorted(v.attributes["link_id"] for v in model.by_kind(EntityKind.VERTICAL_LINK))
    assert links == sorted(v.attributes["link_id"] for v in r2.by_kind(EntityKind.VERTICAL_LINK, FLOOR))
    assert report.stats["profile_id"] == "customer_style" and report.stats["scale_to_m"] == [0.001]
    assert not report.review_queue and not any(i.code == "unit_code_nonstandard" for i in report.issues)


def test_without_profile_units_are_missing(customer_dir, r2, brand_rows) -> None:
    model, report = parse_revision(customer_dir, brand_rows, relations="basic")
    truth_units = len(r2.units(FLOOR))
    assert len(model.units()) <= 0.2 * truth_units
    assert report.review_queue and any(i.code == "unit_polygon_without_label" for i in report.issues)


def test_manifest_inline_profile_and_profile_units(
    tmp_path, customer_dir, customer_profile, brand_rows
) -> None:
    data = json.loads((customer_dir / "manifest.json").read_text(encoding="utf-8"))
    data["drawing_profile"] = profile_to_dict(customer_profile)
    (tmp_path / f"{FLOOR}.dxf").write_bytes((customer_dir / f"{FLOOR}.dxf").read_bytes())
    (tmp_path / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    model, report = parse_revision(tmp_path, brand_rows, relations="basic")
    assert report.stats["profile_id"] == "customer_style" and len(model.units()) > 30
    drawing = read_revision(customer_dir, units="cm").floors[0]
    assert (drawing.scale, drawing.unit_source) == (0.01, "manifest")


# --- samples --------------------------------------------------------------------------------------------------


def test_sample_writer_is_reproducible(tmp_path, r2) -> None:
    first = write_samples(r2, tmp_path / "a", FLOOR)
    write_samples(r2, tmp_path / "b", FLOOR)
    assert set(first) == {f"customer_style_{FLOOR}.zip", "profiles/customer_style.yaml"}
    for name in first:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert not list((tmp_path / "a").glob("*.dxf*"))


def test_committed_samples_parse_with_committed_profile(tmp_path, r2, brand_rows) -> None:
    profile_text = (SAMPLES / "profiles" / "customer_style.yaml").read_text(encoding="utf-8")
    assert profile_text == CUSTOMER_PROFILE_YAML
    with zipfile.ZipFile(SAMPLES / f"customer_style_{FLOOR}.zip") as archive:
        assert archive.namelist() == [f"{FLOOR}.dxf"]
        (tmp_path / f"{FLOOR}.dxf").write_bytes(archive.read(f"{FLOOR}.dxf"))
    levels = [{"floor": FLOOR, "file": f"{FLOOR}.dxf"}]
    (tmp_path / "manifest.json").write_text(json.dumps({"levels": levels}), encoding="utf-8")
    model, _ = parse_revision(
        tmp_path, brand_rows, relations="basic", profile=load_profile(yaml.safe_load(profile_text))
    )
    assert {u.attributes["unit_code"] for u in model.units()} == {
        u.attributes["unit_code"] for u in r2.units(FLOOR)
    }
