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
