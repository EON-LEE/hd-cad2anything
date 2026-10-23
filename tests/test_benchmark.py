"""Multi-store comparison, relayout performance review, store metadata uploads and their REST routes."""

from __future__ import annotations

import copy
import io
import statistics
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cad2anything.catalog import FACILITY_CODE, FACILITY_NAME
from cad2anything.mockgen.stores import (
    PEERS,
    build_store_benchmarks,
    ensure_store_benchmarks,
    flagship_baseline,
)
from cad2anything.model import Revision, RevisionKind, SpatialModel
from cad2anything.services.benchmark_api import build_benchmark_router
from cad2anything.services.benchmark_tools import BenchmarkTools, flagship_monthly, render_relayout_review
from cad2anything.services.errors import NotFoundToolError, ToolError
from cad2anything.services.tools import Toolkit

# The coordinator adds BenchmarkTools to Toolkit; until then the mixin goes first.
_BASES = (Toolkit,) if issubclass(Toolkit, BenchmarkTools) else (BenchmarkTools, Toolkit)
PEER_IDS = [spec.store_id for spec in PEERS]


class _Toolkit(*_BASES):  # type: ignore[misc]
    def __init__(self, workspace: Any) -> None:
        super().__init__(workspace, use_postgis=False)


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> _Toolkit:
    ws = populate(tmp_path_factory.mktemp("benchmark-ws"))
    toolkit = _Toolkit(ws)
    summary = ensure_store_benchmarks(ws, toolkit=toolkit)
    assert summary["written"] == ["business/stores.csv", "business/store_kpis.csv"], summary
    return toolkit


@pytest.fixture(scope="module")
def bare(tmp_path_factory, populate) -> _Toolkit:
    """A workspace without store metadata (missing-metadata errors)."""
    return _Toolkit(populate(tmp_path_factory.mktemp("benchmark-bare")))


@pytest.fixture()
def meta(tmp_path_factory, populate) -> _Toolkit:
    """A fresh workspace for metadata uploads."""
    return _Toolkit(populate(tmp_path_factory.mktemp("benchmark-meta")))


def _csv(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


# --- synthetic peers --------------------------------------------------------------------------------------


def test_mock_peers_are_deterministic_and_plausible(toolkit, tmp_path) -> None:
    ws = toolkit.ws
    stores = pd.read_csv(ws.table_path("stores"), dtype=str)
    kpis = pd.read_csv(ws.table_path("store_kpis"), dtype={"store_id": str, "period": str})
    assert list(stores.columns) == ["store_id", "store_name", "region", "format", "gross_area_m2", "opened"]
    assert list(kpis.columns) == ["store_id", "period", "floor", "category", "area_m2", "sales_krw"]
    assert stores["store_id"].tolist() == PEER_IDS and stores["store_name"].str.startswith("가상 ").all()
    assert FACILITY_CODE not in set(stores["store_id"]) | set(kpis["store_id"])
    assert not kpis.duplicated(["store_id", "period", "floor", "category"]).any()
    assert (kpis["area_m2"] > 0).all() and (kpis["sales_krw"] >= 0).all()
    assert set(kpis.groupby("store_id")["period"].nunique()) == {24}
    assert (kpis["period"].min(), kpis["period"].max()) == ("2024-10", "2026-09")
    assert "8F" not in set(kpis.loc[kpis["store_id"] == "S05", "floor"])
    outlet = kpis[kpis["store_id"] == "S06"]
    assert not {"LUXURY", "JEWELRY_WATCH"} & set(outlet["category"]) and not {"7F", "8F"} & set(
        outlet["floor"]
    )

    baseline = flagship_baseline(flagship_monthly(toolkit), 86_400.0)
    first, second = build_store_benchmarks(baseline), build_store_benchmarks(baseline)
    pd.testing.assert_frame_equal(first[0], second[0])
    pd.testing.assert_frame_equal(first[1], second[1])
    pd.testing.assert_frame_equal(first[1], kpis, check_dtype=False)

    again = ensure_store_benchmarks(ws, toolkit=toolkit, samples_dir=tmp_path)
    assert again["written"] == [] and again["skipped"].startswith("already present")
    assert (tmp_path / "store_kpis.csv").read_bytes() == ws.table_path("store_kpis").read_bytes()

    whole = toolkit.analytics_store_compare(level="store")
    ratios = {sid: value / whole["rows"][0]["flagship"] for sid, value in whole["rows"][0]["values"].items()}
    assert all(0.4 < ratio < 1.6 for ratio in ratios.values()), ratios
    growth = toolkit.analytics_store_compare(level="store", metric="growth_pct")["rows"][0]["values"]
    assert all(value is not None for value in growth.values())
    assert growth["S06"] > growth["S04"] + 4


# --- store comparison -------------------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["store", "floor", "category"])
def test_store_compare_rows_are_consistent(toolkit, level) -> None:
    result = toolkit.analytics_store_compare(level=level)
    assert (result["level"], result["metric"]) == (level, "sales_per_m2")
    assert result["period"] == {"start": "2025-10-01", "end": "2026-09-30", "months": 12}
    assert result["stores"][0] == {
        "store_id": FACILITY_CODE,
        "store_name": FACILITY_NAME,
        "region": "",
        "format": "백화점",
        "flagship": True,
        "months": 12,
    }
    assert [store["store_id"] for store in result["stores"][1:]] == PEER_IDS
    assert result["rows"] and result["summary"] and result["caveat"] and not result["truncated"]
    for row in result["rows"]:
        values = row["values"]
        assert set(values) == {FACILITY_CODE, *PEER_IDS} and values[FACILITY_CODE] == row["flagship"]
        present = [value for value in values.values() if value is not None]
        assert row["n_stores"] == len(present)
        peers = [value for sid, value in values.items() if sid != FACILITY_CODE and value is not None]
        if row["flagship"] is None or not peers:
            continue
        assert row["rank"] == 1 + sum(value > row["flagship"] for value in present)
        median = statistics.median(peers)
        assert row["peer_median"] == pytest.approx(median, abs=1)
        assert row["gap_pct"] == pytest.approx((row["flagship"] / median - 1) * 100, abs=0.15)
    keys = [row["key"] for row in result["rows"]]
    if level == "store":
        assert keys == ["ALL"] and result["rows"][0]["label"] == "점포 전체"
    elif level == "floor":
        assert keys == ["B1", "1F", "2F", "3F", "4F", "5F", "6F", "7F", "8F"]
        eighth = result["rows"][-1]
        assert eighth["n_stores"] == 5 and eighth["values"]["S05"] is None and eighth["values"]["S06"] is None
        assert any("level=category" in note for note in result["notes"])
        assert any(note.startswith("8F:") for note in result["notes"])
    else:
        luxury = next(row for row in result["rows"] if row["key"] == "LUXURY")
        assert luxury["label"] == "명품" and luxury["values"]["S06"] is None


def test_store_compare_planted_signals_and_filters(toolkit) -> None:
    floors = {row["key"]: row for row in toolkit.analytics_store_compare(level="floor")["rows"]}
    assert floors["4F"]["gap_pct"] < -5
    b1 = floors["B1"]["values"]
    assert max(b1, key=lambda sid: b1[sid]) == "S03"

    cosmetics = toolkit.analytics_store_compare(level="category", category="화장품")
    (row,) = cosmetics["rows"]
    above = [sid for sid, value in row["values"].items() if value > row["flagship"]]
    assert (row["key"], row["rank"], above) == ("COSMETICS", 2, ["S01"]) and row["gap_pct"] > 0
    assert cosmetics["filters"] == {"floor": None, "category": ["COSMETICS"]}
    assert "2위" in cosmetics["summary"] and "높아요" in cosmetics["summary"]

    first = toolkit.analytics_store_compare(level="floor", floor="1층")
    assert [row["key"] for row in first["rows"]] == ["1F"]
    store_1f = toolkit.analytics_store_compare(level="store", floor="1F")
    assert store_1f["rows"][0]["label"] == "1F 합계"
    assert store_1f["rows"][0]["flagship"] == first["rows"][0]["flagship"]
    pyeong = toolkit.analytics_store_compare(level="floor", floor="1F", metric="평효율")
    assert pyeong["metric"] == "sales_per_pyeong"
    assert pyeong["rows"][0]["flagship"] == pytest.approx(first["rows"][0]["flagship"] * 3.3058, rel=1e-6)

    top = toolkit.analytics_store_compare(level="category", top=3)
    gaps = [abs(row["gap_pct"]) for row in top["rows"]]
    assert top["truncated"] and len(gaps) == 3 and gaps == sorted(gaps, reverse=True)

    earlier = toolkit.analytics_store_compare(level="store", start="2025-01", end="2025-06")
    assert earlier["period"] == {"start": "2025-01-01", "end": "2025-06-30", "months": 6}
    everything = toolkit.analytics_store_compare(level="store", floor="전체", category="all")
    assert everything["rows"][0]["label"] == "점포 전체"
    assert everything["filters"] == {"floor": None, "category": None}


def test_store_compare_errors(toolkit, bare) -> None:
    with pytest.raises(ToolError, match="개정 검토 → 메타데이터") as missing:
        bare.analytics_store_compare()
    assert "store_kpis" in str(missing.value) and "stores" in str(missing.value)
    with pytest.raises(ToolError, match="level"):
        toolkit.analytics_store_compare(level="zone")
    with pytest.raises(ToolError, match="metric"):
        toolkit.analytics_store_compare(metric="profit")
    with pytest.raises(ToolError, match="기간"):
        toolkit.analytics_store_compare(start="2030-01", end="2030-06")
    with pytest.raises(ToolError, match="실적"):
        toolkit.analytics_store_compare(floor="12F")


def test_store_compare_without_peer_rows(meta) -> None:
    path = meta.ws.root / "business" / "store_kpis.csv"
    header = "store_id,period,floor,category,area_m2,sales_krw\n"
    for body in ("", f"{FACILITY_CODE},2026-09,1F,COSMETICS,10,100\n", "S01,2026-13,1F,COSMETICS,10,100\n"):
        path.write_text(header + body, encoding="utf-8")
        with pytest.raises(ToolError, match="다른 점포 실적이 없어요"):
            meta.analytics_store_compare()


# --- metadata uploads -------------------------------------------------------------------------------------


def test_store_metadata_uploads(meta) -> None:
    ws = meta.ws
    stores = pd.DataFrame(
        {
            "store_id": ["S01", "S02"],
            "store_name": ["가상 도심점", "가상 신도시점"],
            "region": ["수도권 도심", "수도권 신도시"],
            "format": ["백화점", "백화점"],
            "gross_area_m2": ["98,000", "80000"],
            "opened": ["1998-09-01", ""],
        }
    )
    kpis = pd.DataFrame(
        {
            "store_id": ["S01", "S01", "S02", "S02"],
            "period": ["2026-08", "2026/9", "202608", "2026년 9월"],
            "floor": ["1층", "1F", "지하1층", "B1"],
            "category": ["화장품", "cosmetics", "신선식품", "FOOD_FRESH"],
            "area_m2": ["1,200.5", "1200.5", "800", "800"],
            "sales_krw": ["5,000,000,000", "5100000000", "900000000", "950000000"],
        }
    )
    early = meta.upload_metadata("store_kpis", "kpis.csv", _csv(kpis))
    assert early["status"] == "accepted" and any("stores 표가 없어" in w for w in early["warnings"]), early
    saved = pd.read_csv(ws.table_path("store_kpis"), dtype=str)
    assert saved["period"].tolist() == ["2026-08", "2026-09", "2026-08", "2026-09"]
    assert saved["floor"].tolist() == ["1F", "1F", "B1", "B1"]
    assert saved["category"].tolist() == ["COSMETICS", "COSMETICS", "FOOD_FRESH", "FOOD_FRESH"]
    assert saved["area_m2"].tolist()[0] == "1200.5" and saved["sales_krw"].tolist()[0] == "5000000000"

    accepted = meta.upload_metadata("stores", "stores.csv", _csv(stores))
    assert (accepted["status"], accepted["rows"], accepted["errors"]) == ("accepted", 2, [])
    buffer = io.BytesIO()
    stores.to_excel(buffer, index=False)
    again = meta.upload_metadata("stores", "stores.xlsx", buffer.getvalue())
    assert again["status"] == "accepted" and again["backup"].startswith("business/_backup/stores.")
    before = ws.table_path("store_kpis").read_bytes()
    rejected = {
        "period": kpis.assign(period=["2026-13", "2026-09", "2026-08", "2026-09"]),
        "area_m2": kpis.assign(area_m2=["-5", "1", "1", "1"]),
        "sales_krw": kpis.assign(sales_krw=["-1", "1", "1", "1"]),
        "floor": kpis.assign(floor=["옥상", "1F", "B1", "B1"]),
        "중복": kpis.assign(period="2026-08", floor="1F", category="COSMETICS", store_id="S01"),
        "stores 표에 없는": kpis.assign(store_id=["S01", "S01", "S02", "S99"]),
    }
    for needle, frame in rejected.items():
        outcome = meta.upload_metadata("store_kpis", "kpis.csv", _csv(frame))
        assert outcome["status"] == "rejected" and any(needle in e for e in outcome["errors"]), (
            needle,
            outcome,
        )
    assert ws.table_path("store_kpis").read_bytes() == before
    warned = meta.upload_metadata(
        "store_kpis", "kpis.csv", _csv(kpis.assign(category=["화장품", "X", "Y", "Z"]))
    )
    assert warned["status"] == "accepted" and any("카탈로그에 없는 카테고리" in w for w in warned["warnings"])
    own = meta.upload_metadata(
        "store_kpis", "kpis.csv", _csv(kpis.assign(store_id=["S01", "S01", "S02", FACILITY_CODE]))
    )
    assert own["status"] == "accepted" and any(FACILITY_CODE in w for w in own["warnings"])

    for needle, frame in {
        "store_id": pd.concat([stores, stores.head(1)]),
        "gross_area_m2": stores.assign(gross_area_m2=["0", "1"]),
        "필수 열": stores.drop(columns=["region"]),
    }.items():
        outcome = meta.upload_metadata("stores", "stores.csv", _csv(frame))
        assert outcome["status"] == "rejected" and any(needle in e for e in outcome["errors"]), (
            needle,
            outcome,
        )
    orphaned = meta.upload_metadata("stores", "stores.csv", _csv(stores.head(1)))
    assert orphaned["status"] == "accepted" and any("S02" in w for w in orphaned["warnings"])

    status = {row["kind"]: row for row in meta.metadata_status()}
    assert list(status)[-2:] == ["stores", "store_kpis"]
    assert status["stores"]["present"] and status["stores"]["rows"] == 1
    assert status["store_kpis"]["required_columns"] == [
        "store_id",
        "period",
        "floor",
        "category",
        "area_m2",
        "sales_krw",
    ]


# --- relayout review --------------------------------------------------------------------------------------


def test_relayout_review_r2(toolkit) -> None:
    review = toolkit.analytics_relayout_review("R2")
    assert (review["rev"], review["parent_rev"], review["effective_date"]) == ("R2", "R1", "2025-09-01")
    assert review["window"] == {
        "before": {"start": "2025-06-03", "end": "2025-08-31"},
        "after": {"start": "2025-09-01", "end": "2025-11-29"},
        "days": 90,
        "requested_days": 90,
    }
    changes = [
        i for i in toolkit.ws.change_set("R1", "R2").items if i.unit_codes_before or i.unit_codes_after
    ]
    expected = sorted(
        (str(i.change_type), tuple(i.unit_codes_before), tuple(i.unit_codes_after)) for i in changes
    )
    items = review["items"]
    assert (
        sorted((i["change_type"], tuple(i["units_before"]), tuple(i["units_after"])) for i in items)
        == expected
    )
    assert all(i["change_label"] and i["floor"] and i["unit_code"] for i in items)

    move = next(i for i in items if i["change_type"] == "move")
    assert move["units_label"] == "6F-E04 → 6F-S02B" and move["unit_code"] == "6F-S02B"
    assert move["brand_name_before"] == move["brand_name_after"] and move["comparable"]
    cafe = next(i for i in items if i["unit_code"] == "3F-S01")
    assert cafe["change_label"] == "교체" and cafe["brand_name_before"] != cafe["brand_name_after"]
    merge = next(i for i in items if i["change_type"] == "merge")
    assert merge["units_before"] == ["7F-S03", "7F-S04"] and merge["after_area_m2"] > 0

    assert review["control"]["store"]["units"] > 50 and review["control"]["floors"]
    for item in items:
        assert item["control_pct"] is not None
        if item["comparable"]:
            assert item["change_pct"] == pytest.approx(
                (item["after_daily_krw"] / item["before_daily_krw"] - 1) * 100, abs=0.05
            )
            ratio = (1 + item["change_pct"] / 100) / (1 + item["control_pct"] / 100)
            assert item["vs_control_pct"] == pytest.approx((ratio - 1) * 100, abs=0.2)
            assert item["before_sales_per_m2"] > 0 and item["after_sales_per_m2"] >= 0
    for field, groups in (("change_type", review["by_type"]), ("floor", review["by_floor"])):
        assert sum(group["items"] for group in groups) == len(items)
        for group in groups:
            members = [i for i in items if i[field] == group[field]]
            assert group["items"] == len(members)
            assert group["comparable"] == sum(i["comparable"] for i in members)
            assert group["before_daily_krw"] == sum(i["before_daily_krw"] or 0 for i in members)
            assert group["after_daily_krw"] == sum(i["after_daily_krw"] or 0 for i in members)
    assert [group["change_type"] for group in review["by_type"]][:2] == ["move", "reassign"]
    assert [group["floor"] for group in review["by_floor"]] == ["1F", "3F", "6F", "7F", "8F"]
    total = review["total"]
    assert total["comparable"] == sum(i["comparable"] for i in items) and total["vs_control_pct"] is not None
    summary = review["summary"]
    assert summary.startswith("R2 개편(R1→R2, 2025-09-01 적용)") and "비교군 대비" in summary
    assert "가장 좋아진 곳은" in summary and "가장 나빠진 곳은" in summary
    assert "인과" in review["caveat"] and len(review["caveats"]) == 4
    assert review["report_url"] == "/api/reports/relayout-review/R2?window_days=90"


def test_relayout_review_windows_and_errors(toolkit) -> None:
    clipped = toolkit.analytics_relayout_review("R2", window_days=400)
    assert clipped["window"]["days"] == 184 and clipped["window"]["before"]["start"] == "2025-03-01"
    assert any("184일" in note for note in clipped["notes"])
    r1 = toolkit.analytics_relayout_review("R1", window_days=365)
    assert r1["window"]["days"] == 151 and r1["window"]["before"]["start"] == "2024-10-01"
    assert any(i["change_type"] == "add" and not i["comparable"] for i in r1["items"])
    assert any("통로 변경" in note for note in r1["notes"])
    with pytest.raises(ToolError, match="14일"):
        toolkit.analytics_relayout_review("R2", window_days=7)
    with pytest.raises(NotFoundToolError):
        toolkit.analytics_relayout_review("R9")
    with pytest.raises(ToolError, match="가상 개정"):
        toolkit.analytics_relayout_review("H1")
    with pytest.raises(ToolError, match="첫 개정"):
        toolkit.analytics_relayout_review("R0")
    future = SpatialModel.from_dict(toolkit.ws.model("R2").to_dict())
    future.revision = Revision("R2F", RevisionKind.ACTUAL, parent_rev_id="R2", effective_date="2026-10-01")
    toolkit.ws.save_model(future, {"source": "uploaded"})
    try:
        with pytest.raises(ToolError, match="아직 적용 후 실적이 없어요"):
            toolkit.analytics_relayout_review("R2F")
    finally:
        toolkit.ws.delete_file("models/R2F.json")


def test_relayout_review_html_escapes_markup(toolkit) -> None:
    page = toolkit.relayout_review_html("R2", window_days=60)
    assert page.startswith("<!doctype html>") and "R2 개편 성과 추적 보고" in page
    assert "2. 변경 유형별" in page and "3. 층별" in page and "4. 변경 매장" in page and "가정과 한계" in page
    hostile = copy.deepcopy(toolkit.analytics_relayout_review("R2", window_days=60))
    hostile["rev"] = "<b>R2</b>"
    hostile["summary"] = "<img src=x onerror=alert(1)>"
    hostile["notes"] = ["<script>alert('note')</script>"]
    hostile["items"][0]["brand_name_after"] = "<script>alert(1)</script>"
    hostile["items"][0]["units_label"] = "<i>3F</i>"
    hostile["by_type"][0]["change_label"] = "<u>move</u>"
    text = render_relayout_review(hostile)
    for markup in ("<script", "<img", "<b>R2", "<i>3F", "<u>move"):
        assert markup not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text and "&lt;img src=x onerror=alert(1)&gt;" in text


# --- REST -------------------------------------------------------------------------------------------------


def test_benchmark_routes(toolkit, bare) -> None:
    app = FastAPI()
    app.include_router(build_benchmark_router(toolkit))
    client = TestClient(app)
    stores = client.get(
        "/api/analytics/stores",
        params={"level": "category", "metric": "sales_per_m2", "floor": "", "category": ""},
    )
    assert stores.status_code == 200 and stores.json()["level"] == "category"
    assert stores.json()["stores"][0]["store_id"] == FACILITY_CODE
    assert client.get("/api/analytics/stores", params={"level": "zone"}).status_code == 400
    review = client.get("/api/analytics/relayout-review", params={"rev": "R2", "window_days": 60})
    assert review.status_code == 200 and review.json()["window"]["days"] == 60
    assert client.get("/api/analytics/relayout-review", params={"rev": "R9"}).status_code == 404
    assert (
        client.get("/api/analytics/relayout-review", params={"rev": "R2", "window_days": 7}).status_code
        == 400
    )
    assert client.get("/api/analytics/relayout-review", params={"rev": "H1"}).status_code == 400
    page = client.get("/api/reports/relayout-review/R2", params={"window_days": 60})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "R2 개편 성과 추적 보고" in page.text
    assert client.get("/api/reports/relayout-review/R9").status_code == 404
    assert client.get("/api/reports/relayout-review/H1").status_code == 400

    other = FastAPI()
    other.include_router(build_benchmark_router(bare))
    missing = TestClient(other).get("/api/analytics/stores")
    assert missing.status_code == 400 and "store_kpis" in missing.json()["detail"]
