import pandas as pd
import pytest
from fastapi.testclient import TestClient

from cad2anything.services.atlas_api import create_api
from cad2anything.services.tools import ToolError, Toolkit
from cad2anything.spatial.routes import RoutePlanner


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> Toolkit:
    return Toolkit(populate(tmp_path_factory.mktemp("journey-ws")), use_postgis=False)


def test_routes_follow_walkways_and_change_floors_by_escalator(synthetic_store):
    planner = RoutePlanner(synthetic_store["models"]["R2"])
    same = planner.route("3F-W02", "3F-E01")
    assert same.via is None and not same.straight and len(same.legs) == 1
    leg = same.legs[0]
    assert leg.points[0] == planner.access_xy("3F-W02") and leg.points[-1] == planner.access_xy("3F-E01")
    straight = (
        (leg.points[0][0] - leg.points[-1][0]) ** 2 + (leg.points[0][1] - leg.points[-1][1]) ** 2
    ) ** 0.5
    assert same.length_m >= straight > 0

    across = planner.route("B1-C05", "3F-C17")
    assert across.via in {"ESC-A", "ESC-B"} and [leg.floor for leg in across.legs] == ["B1", "3F"]
    assert across.legs[1].points[-1] == planner.access_xy("3F-C17")


def test_journey_picks_a_multi_floor_customer_day_with_routes(toolkit):
    journey = toolkit.customer_journey(segment="tier:VIP|VVIP")
    stops = journey["stops"]
    assert len(stops) >= 3 and journey["summary"]["stops"] == len(stops)
    assert [s["order"] for s in stops] == list(range(1, len(stops) + 1))
    assert stops[0]["gap_min"] is None and all(s["gap_min"] >= 0 for s in stops[1:])
    assert all("x" in s and "y" in s for s in stops)
    assert journey["customer"]["tier"] in {"VIP", "VVIP"} and journey["rev"] in {"R0", "R1", "R2"}
    floors = {s["floor"] for s in stops}
    assert {seg["floor"] for seg in journey["segments"]} <= floors
    changes = sum(1 for a, b in zip(stops, stops[1:], strict=False) if a["floor"] != b["floor"])
    assert len(journey["transitions"]) == changes and journey["summary"]["walk_m"] > 0
    assert "실제로 걸어간 경로는 알 수 없고" in journey["caveat"]

    second = toolkit.customer_journey(segment="tier:VIP|VVIP", rank=2)
    assert (second["customer"]["id"], second["date"]) != (journey["customer"]["id"], journey["date"])
    again = toolkit.analytics_customer_journey(customer_id=journey["customer"]["id"], date=journey["date"])
    assert [s["unit_code"] for s in again["stops"]] == [
        s["unit_code"] for s in stops
    ] and "segments" not in again
    with pytest.raises(ToolError):
        toolkit.customer_journey(customer_id="C9999999")
    with pytest.raises(ToolError):
        toolkit.customer_journey(segment="tier:VIP", rank=100000)


def test_journey_rev_limits_candidates_to_that_layout_period(toolkit):
    for rev in ("R0", "R1", "R2"):
        start, end, _ = toolkit.revision_period(rev)
        journey = toolkit.customer_journey(segment="tier:VIP|VVIP", rev=rev, with_routes=False)
        assert start <= journey["date"] <= end and journey["rev"] == rev
        assert journey["query"]["rev"] == rev
    hypothetical = toolkit.customer_journey(segment="tier:VIP|VVIP", rev="H1", with_routes=False)
    assert hypothetical["rev"] == "R2" and hypothetical["query"]["rev"] == "R2"
    by_customer = toolkit.customer_journey(
        customer_id=hypothetical["customer"]["id"], rev="R0", with_routes=False
    )
    assert "rev" not in by_customer["query"]


def test_journey_endpoint_returns_the_ui_state_object(toolkit):
    client = TestClient(create_api(toolkit))
    body = client.get("/api/analytics/journey", params={"segment": "age_band:40s", "rank": 1}).json()
    assert {
        "title",
        "customer",
        "date",
        "stops",
        "segments",
        "transitions",
        "summary",
        "caveat",
        "query",
    } <= set(body)
    assert body["query"] == {"segment": {"age_band": "40s"}, "rank": 1}
    assert client.get("/api/analytics/journey", params={"customer_id": "C9999999"}).status_code == 404


def test_candidate_stops_count_merged_stops_not_receipts():
    from datetime import date

    from cad2anything.analytics.journey import journey_candidates, journey_stops

    day = date(2025, 10, 4)
    rows = pd.DataFrame(
        {
            "customer_key": ["C1"] * 5 + ["C2"] * 3,
            "day": [day] * 8,
            "ts": pd.to_datetime(
                [
                    "2025-10-04 10:00",
                    "2025-10-04 10:20",  # same unit again: one stop
                    "2025-10-04 11:00",
                    "2025-10-04 11:30",
                    "2025-10-04 12:00",  # back to the first unit: a new stop
                    "2025-10-04 10:00",
                    "2025-10-04 10:10",
                    "2025-10-04 10:20",
                ]
            ),
            "unit_code": ["3F-C05", "3F-C05", "3F-C06", "4F-C01", "3F-C05", "1F-C01", "1F-C01", "1F-C01"],
            "floor": ["3F", "3F", "3F", "4F", "3F", "1F", "1F", "1F"],
            "brand_id": ["B1"] * 8,
            "brand_name": ["브랜드"] * 8,
            "category": ["COSMETICS"] * 8,
            "amount_krw": [1000.0] * 8,
        }
    )

    ranked = journey_candidates(rows, min_stops=1)

    assert list(ranked["customer_id"]) == ["C1", "C2"]
    assert list(ranked["stops"]) == [4, 1] and list(ranked["receipts"]) == [5, 3]
    for row in ranked.itertuples(index=False):
        stops, _ = journey_stops(rows, row.customer_id, row.date)
        assert len(stops) == row.stops
        assert sum(s["receipts"] for s in stops) == row.receipts


def test_journey_summary_stops_match_every_alternative(toolkit):
    journey = toolkit.customer_journey(segment="tier:VIP|VVIP", with_routes=False)

    assert journey["summary"]["stops"] == len(journey["stops"])
    assert journey["summary"]["receipts"] == sum(s["receipts"] for s in journey["stops"])
    assert journey["summary"]["receipts"] >= journey["summary"]["stops"]
    assert [s["unit_code"] for s in journey["stops"]] == list(
        dict.fromkeys(
            [
                code
                for i, code in enumerate(s["unit_code"] for s in journey["stops"])
                if i == 0 or code != journey["stops"][i - 1]["unit_code"]
            ]
        )
    )
    for alternative in journey["alternatives"]:
        other = toolkit.customer_journey(
            customer_id=alternative["customer_id"], date=alternative["date"], with_routes=False
        )
        assert other["summary"]["stops"] == alternative["stops"]
        assert other["summary"]["receipts"] == alternative["receipts"]


def test_journey_walk_reports_skipped_and_straight_legs(toolkit):
    journey = toolkit.customer_journey(segment="tier:VIP|VVIP")
    summary = journey["summary"]

    assert summary["legs"] == len(journey["stops"]) - 1
    assert summary["skipped_legs"] == 0 and summary["straight_legs"] == 0
    assert summary["measured_legs"] == summary["legs"] and summary["walk_complete"] is True
    assert summary["walk_m"] > 0 and "walk_notes" not in summary

    # A stop in a unit the drawing does not know must not be counted as a 0 m leg.
    partial = toolkit.customer_journey(
        customer_id=journey["customer"]["id"], date=journey["date"], with_routes=False
    )
    partial["stops"][1] = {**partial["stops"][1], "unit_code": "ZZ-X99"}
    toolkit._add_routes(partial)

    assert partial["summary"]["skipped_legs"] == 2
    assert partial["summary"]["measured_legs"] == partial["summary"]["legs"] - 2
    assert partial["summary"]["walk_complete"] is False
    assert partial["summary"]["walk_m"] < summary["walk_m"]
    assert any("walk_m에 넣지 않았어요" in note for note in partial["summary"]["walk_notes"])
