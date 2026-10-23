"""Estimated corridor flows, popup candidates and the flow REST route (docs/specs/atlas-api.md §3 ``flow``)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cad2anything.services.errors import NotFoundToolError, ToolError
from cad2anything.services.flow_api import build_flow_router
from cad2anything.services.flow_tools import FlowTools
from cad2anything.services.tools import Toolkit
from cad2anything.spatial.flows import FlowRouter, purchase_trips, thin
from cad2anything.spatial.routes import RoutePlanner

# The coordinator adds FlowTools to Toolkit; until then the mixin goes first.
_BASES = (Toolkit,) if issubclass(Toolkit, FlowTools) else (FlowTools, Toolkit)
FLOW_KEYS = {"mode", "title", "rev", "query", "floors", "markers", "legend", "summary", "caveat"}
SEGMENT_KEYS = {"points", "weight", "value", "color", "label"}
MARKER_KEYS = {"floor", "x", "y", "rank", "kind", "label", "title", "unit_code"}


class _Toolkit(*_BASES):  # type: ignore[misc]
    pass


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> _Toolkit:
    return _Toolkit(populate(tmp_path_factory.mktemp("flow-ws")), use_postgis=False)


@pytest.fixture(scope="module")
def router(synthetic_store) -> FlowRouter:
    return FlowRouter(synthetic_store["models"]["R2"])


def _stub_flow(graph, edge_flow: np.ndarray, entity_ids: set[str]) -> float:
    return float(
        sum(
            edge_flow[e]
            for e, edge in enumerate(graph.edges)
            if edge.stub and graph.node_entity[edge.u] in entity_ids
        )
    )


def test_thinning_leaves_connected_center_lines():
    mask = np.zeros((24, 12), dtype=bool)
    mask[2:22, 3:9] = True  # a 20 x 6 corridor
    mask[10:16, 0:12] = True  # crossed by a 6-wide corridor
    lines = thin(mask)
    assert lines.sum() < mask.sum() / 3 and (lines <= mask).all()
    assert all(lines[i, 3:9].sum() <= 2 for i in range(4, 8))  # one cell wide away from the junction
    cells = set(zip(*np.nonzero(lines), strict=True))
    start = next(iter(cells))
    seen, stack = {start}, [start]
    while stack:
        i, j = stack.pop()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if (i + di, j + dj) in cells and (i + di, j + dj) not in seen:
                    seen.add((i + di, j + dj))
                    stack.append((i + di, j + dj))
    assert seen == cells


def test_trips_join_consecutive_purchases_of_one_customer_day():
    rows = pd.DataFrame(
        {
            "customer_key": ["A", "A", "A", "A", "A", "B", "B"],
            "ts": pd.to_datetime(
                [
                    "2025-10-04 10:00",
                    "2025-10-04 10:30",  # same unit again: one stop
                    "2025-10-04 11:00",
                    "2025-10-04 12:00",
                    "2025-10-05 10:00",  # next day: no trip from the day before
                    "2025-10-04 15:00",
                    "2025-10-04 14:00",  # unsorted input
                ]
            ),
            "unit_code": ["3F-C05", "3F-C05", "3F-C06", "1F-C01", "1F-C01", "B1-C02", "B1-C01"],
            "weight": [50.0, 50.0, 40.0, 60.0, 50.0, 10.0, 30.0],
        }
    )
    trips = purchase_trips(rows)
    assert list(zip(trips["unit_a"], trips["unit_b"], strict=True)) == [
        ("3F-C05", "3F-C06"),
        ("3F-C06", "1F-C01"),
        ("B1-C01", "B1-C02"),
    ]
    assert trips["weight"].tolist() == [45.0, 50.0, 20.0]
    assert purchase_trips(rows.iloc[:0]).empty


def test_routed_trips_are_conserved_on_both_floors(router):
    pairs = pd.DataFrame(
        {
            "unit_a": ["3F-W02", "B1-C05", "3F-C05", "XX-C01"],
            "unit_b": ["3F-E01", "3F-C17", "3F-C06", "3F-C05"],
            "weight": [2.0, 1.0, 3.0, 5.0],
        }
    )
    flows = router.route(pairs)
    assert flows.routed == pytest.approx(6.0) and flows.unrouted == pytest.approx(5.0)
    assert flows.routed_rows.tolist() == [True, True, True, False]
    assert flows.legs == pytest.approx({"3F": 6.0, "B1": 1.0})
    assert sum(flows.via.values()) == pytest.approx(1.0) and set(flows.via) <= {"ESC-A", "ESC-B"}
    for floor, graph in router.graphs.items():
        flow = flows.edge_flow[floor]
        units = {u.entity_id for u in graph.units}
        landings = {m.entity_id for m in graph.landmarks if m.kind == "escalator"}
        expected_units = {"3F": 2 * 2.0 + 2 * 3.0 + 1.0, "B1": 1.0}.get(floor, 0.0)
        assert _stub_flow(graph, flow, units) == pytest.approx(expected_units), floor
        assert _stub_flow(graph, flow, landings) == pytest.approx(1.0 if floor in ("B1", "3F") else 0.0)
        assert bool(graph.segments(flow)) == (floor in ("B1", "3F"))
        assert (graph.node_flow(flow)[: graph.n_centerline] >= -1e-9).all()


def test_a_trip_follows_the_corridors_like_a_route(router, synthetic_store):
    flows = router.route(pd.DataFrame({"unit_a": ["3F-W02"], "unit_b": ["3F-E01"], "weight": [1.0]}))
    graph = router.graphs["3F"]
    segments = graph.segments(flows.edge_flow["3F"])
    assert len(segments) == 1 and segments[0][1] == pytest.approx(1.0)
    points = segments[0][0]
    planner = RoutePlanner(synthetic_store["models"]["R2"])
    ends = {planner.access_xy("3F-W02"), planner.access_xy("3F-E01")}
    assert all(min(math.dist(p, end) for p in (points[0], points[-1])) < 4.5 for end in ends)
    length = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    route = planner.route("3F-W02", "3F-E01").length_m
    assert 0.7 * route < length < 1.3 * route
    pass_by = graph.pass_by(flows.edge_flow["3F"])
    assert (
        pass_by["3F-W02"] == 0.0 and pass_by["3F-E01"] == 0.0 and max(pass_by.values()) == pytest.approx(1.0)
    )


def test_flow_map_counts_every_trip_and_filters_change_totals(toolkit):
    base = toolkit.analytics_flow_map()
    rev = base["rev"]
    assert rev == "R2" and base["label"] == "추정치" and base["sample"]["trips"] > 0
    start, end, _ = toolkit.revision_period(rev)
    rows = toolkit._journey_rows()
    part = rows[(rows["day"] >= date.fromisoformat(start)) & (rows["day"] <= date.fromisoformat(end))]
    part = part.sort_values(["customer_key", "ts"], kind="mergesort").reset_index(drop=True)
    key = part["customer_key"].astype(str) + "|" + part["day"].astype(str)
    repeat = key.eq(key.shift()) & part["unit_code"].eq(part["unit_code"].shift())
    stops, key = part[~repeat].reset_index(drop=True), key[~repeat].reset_index(drop=True)
    after = stops.shift(-1)
    pair = key.eq(key.shift(-1))
    codes = set(toolkit._flow_router(rev).codes)
    known = pair & stops["unit_code"].isin(codes) & after["unit_code"].isin(codes)
    expected = ((stops["weight"] + after["weight"]) / 2)[known].sum()
    assert base["trips"] == pytest.approx(expected, abs=1.0)
    assert base["sample"]["trips"] == int(known.sum())
    walks = sum(f["walks"] for f in base["floors"])
    assert base["trips"] <= walks <= 2 * base["trips"] + len(base["floors"])

    weekend = toolkit.analytics_flow_map(day_type="weekend")
    weekday = toolkit.analytics_flow_map(day_type="평일")
    assert weekend["trips"] + weekday["trips"] == pytest.approx(base["trips"], abs=2)
    assert 0 < weekend["period"]["days"] < base["period"]["days"]
    vip = toolkit.analytics_flow_map(segment="tier:VIP|VVIP", top=3)
    lunch = toolkit.analytics_flow_map(hours="12-14")
    assert 0 < vip["trips"] < base["trips"] and 0 < lunch["trips"] < base["trips"]
    assert (
        vip["query"]["segment"] == {"tier": ["VIP", "VVIP"]} and vip["query"]["segment_label"] == "VIP·VVIP"
    )
    assert len(vip["top_corridors"]) == 3 and len(vip["top_units"]) == 3

    values = [c["value"] for c in base["top_corridors"]]
    assert values == sorted(values, reverse=True) and all(
        0 < c["share_of_floor"] <= 1 for c in base["top_corridors"]
    )
    assert all(("앞" in c["label"] or "통로" in c["label"]) for c in base["top_corridors"])
    units = [u["value"] for u in base["top_units"]]
    assert units == sorted(units, reverse=True) and "추정" in base["caveat"]

    third = toolkit.analytics_flow_map(floor="3층")
    assert [f["floor"] for f in third["floors"]] == ["3F"]
    assert all(c["floor"] == "3F" for c in third["top_corridors"])
    clipped = toolkit.analytics_flow_map(start="2025-03-15", end="2025-10-31")
    assert clipped["rev"] == "R2" and clipped["period"]["start"] == "2025-09-01"
    assert any("만 집계" in note for note in clipped["notes"])
    older = toolkit._flow_query("R1", None, None, "all", None)
    assert (older.rev, older.start.isoformat(), older.end.isoformat()) == ("R1", "2025-03-01", "2025-08-31")


def test_flow_object_matches_the_contract(toolkit):
    flow = toolkit.flow_object("traffic", segment="age_band:40s", day_type="weekend", hours="10-18")
    assert set(flow) >= FLOW_KEYS and flow["mode"] == "traffic" and flow["markers"] == []
    assert flow["title"] == "40대 고객의 추정 유동(주말 10~18시)"
    assert flow["query"]["segment"] == {"age_band": "40s"} and flow["query"]["hours"] == [10, 18]
    assert {"low", "high", "unit", "note"} <= set(flow["legend"])
    assert {"customer_days", "trips", "floors"} <= set(flow["summary"])
    assert flow["floors"] and list(flow["floors"]) == flow["summary"]["floors"]
    for floor, layer in flow["floors"].items():
        assert {"segments", "max_value", "total"} <= set(layer)
        assert 0 < len(layer["segments"]) <= 400
        weights = [s["weight"] for s in layer["segments"]]
        assert all(0 <= w <= 1 for w in weights) and max(weights) == 1.0
        assert max(s["value"] for s in layer["segments"]) == layer["max_value"]
        for segment in layer["segments"]:
            assert set(segment) >= SEGMENT_KEYS and segment["color"] is None and segment["label"]
            assert len(segment["points"]) >= 2
            assert all(0 <= x <= 120 and 0 <= y <= 80 for x, y in segment["points"]), floor
    one = toolkit.flow_object("traffic", floor="B1")
    assert list(one["floors"]) == ["B1"] and one["title"] == "전체 고객의 추정 유동"
    again = toolkit.flow_object("traffic", segment="age_band:40s", day_type="weekend", hours="10-18")
    assert again == flow


def test_popup_candidates_rank_units_and_corridor_spots(toolkit, synthetic_store):
    result = toolkit.spatial_popup_candidates(top=50)
    candidates = result["candidates"]
    assert result["query"]["day_type"] == "weekend" and result["query"]["booth_m2"] == 9.0
    scores = [c["score"] for c in candidates]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 1 for s in scores)
    assert [c["rank"] for c in candidates] == list(range(1, len(candidates) + 1))
    units = {c["unit_code"] for c in candidates if c["kind"] == "unit"}
    vacant = {
        u.attributes["unit_code"]
        for u in synthetic_store["models"]["R2"].units()
        if u.attributes.get("vacant")
    }
    assert vacant <= units and "4F-P01" in units
    spots = [c for c in candidates if c["kind"] == "spot"]
    assert spots and all(c["unit_code"] is None and c["clear_width_m"] >= 7.8 for c in spots)
    assert all(c["booth_m2"] == 9.0 and c["passers"] > 0 for c in spots)
    for c in candidates:
        assert c["reasons"] and c["near"] and c["floor"] and c["passers"] >= 0
        assert "앞을 지나" in c["reasons"][0] or "유동이 없어요" in c["reasons"][0]

    top = toolkit.spatial_popup_candidates(floor="4F", category="잡화", hours="12-18")
    assert {c["floor"] for c in top["candidates"]} == {"4F"} and len(top["candidates"]) <= 5
    assert top["query"]["category"] == "ACCESSORIES" and "카테고리 궁합" in top["scoring"]
    assert {c["kind"] for c in top["candidates"]} == {"unit", "spot"}

    flow = toolkit.flow_object("popup")
    assert set(flow) >= FLOW_KEYS and flow["mode"] == "popup" and flow["query"]["day_type"] == "weekend"
    assert [m["rank"] for m in flow["markers"]] == list(range(1, len(flow["markers"]) + 1))
    for marker in flow["markers"]:
        assert set(marker) >= MARKER_KEYS and marker["kind"] in {"spot", "unit"}
        assert marker["label"] == str(marker["rank"]) and marker["title"].startswith(
            f"후보 {marker['rank']} · "
        )
        assert marker["floor"] in flow["floors"]


def test_flow_arguments_are_checked(toolkit):
    with pytest.raises(ToolError, match="가상 개정"):
        toolkit.analytics_flow_map(rev="H1")
    with pytest.raises(NotFoundToolError):
        toolkit.flow_object("traffic", rev="R9")
    with pytest.raises(NotFoundToolError):
        toolkit.analytics_flow_map(floor="12F")
    for kwargs in ({"day_type": "holiday"}, {"hours": "14-10"}, {"hours": "25"}, {"segment": "height:180"}):
        with pytest.raises(ToolError):
            toolkit.analytics_flow_map(**kwargs)
    with pytest.raises(ToolError, match="mode"):
        toolkit.flow_object("heatmap")
    with pytest.raises(ToolError, match="쓰이지 않았어요"):
        toolkit.analytics_flow_map(rev="R2", start="2024-10-01", end="2024-12-31")


def test_flow_route_serves_the_ui_object_and_maps_errors(toolkit):
    app = FastAPI()
    app.include_router(build_flow_router(toolkit))
    client = TestClient(app)
    body = client.get("/api/spatial/flow", params={"segment": "tier:VIP", "hours": "10-14"})
    assert body.status_code == 200 and set(body.json()) >= FLOW_KEYS and body.json()["mode"] == "traffic"
    egress = client.get("/api/spatial/flow", params={"mode": "egress", "min_width_m": "2.5", "floor": "4F"})
    assert egress.status_code == 200 and list(egress.json()["floors"]) == ["4F"]
    for params in (
        {"mode": "crowd"},
        {"hours": "9-25"},
        {"day_type": "sometimes"},
        {"mode": "egress", "min_width_m": "abc"},
        {"mode": "egress", "max_travel_m": "-5"},
        {"segment": "height:180"},
    ):
        assert client.get("/api/spatial/flow", params=params).status_code == 400, params
    assert client.get("/api/spatial/flow", params={"rev": "R9"}).status_code == 404
    assert client.get("/api/spatial/flow", params={"mode": "egress", "floor": "12F"}).status_code == 404
    # The screen may show a hypothetical alternative: traffic falls back to its actual base revision.
    based = client.get("/api/spatial/flow", params={"rev": "H1", "mode": "popup"}).json()
    assert based["rev"] == "R2" and any(
        "H1에는 거래 실적이 없어" in note for note in based["summary"]["notes"]
    )
