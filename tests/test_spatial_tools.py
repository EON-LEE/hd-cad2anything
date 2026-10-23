import json

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from cad2anything.agents.labels import tool_label
from cad2anything.agents.ui_actions import UiActionHandler
from cad2anything.services.atlas_api import create_api
from cad2anything.services.tools import ToolError, Toolkit
from cad2anything.spatial.vocabulary import facade_directions, side_of, unit_vocabulary


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> Toolkit:
    return Toolkit(populate(tmp_path_factory.mktemp("spatial-ws")), use_postgis=False)


def test_sides_follow_the_drawing_thirds():
    bounds = (0.0, 0.0, 120.0, 80.0)
    assert side_of(60, 40, bounds) == "중앙"
    assert side_of(10, 75, bounds) == "북서" and side_of(110, 5, bounds) == "남동"
    assert side_of(60, 79, bounds) == "북" and side_of(119, 40, bounds) == "동"


def test_facade_directions_find_corner_units():
    unit = box(0, 0, 10, 10)
    walkway = box(-3, -3, 13, 0).union(box(10, -3, 13, 13))  # south and east of the unit
    assert set(facade_directions(unit, walkway)) == {"남", "동"}
    assert facade_directions(unit, box(-3, 10, 13, 13)) == ["북"]
    assert facade_directions(unit, box(10, 4, 13, 5)) == []  # a 1 m opening is not a facade


def test_vocabulary_matches_the_drawing_conventions(synthetic_store):
    vocab = unit_vocabulary(synthetic_store["models"]["R2"]).set_index("unit_code")
    bands = {
        u.attributes["unit_code"]: u.attributes.get("band") for u in synthetic_store["models"]["R2"].units()
    }
    expected = {"N": "북", "S": "남", "E": "동", "W": "서"}
    for code, band in bands.items():
        if band in expected:
            assert expected[band] in vocab.loc[code, "side"], code
    front = set(vocab.index[vocab["escalator_front"]])
    assert {"3F-C05", "3F-C06", "3F-C17", "3F-C18"} <= front
    assert vocab.loc["3F-C05", "front_escalator"] == "ESC-A" and vocab.loc["3F-C05", "escalator_view_m"] < 10
    assert vocab["traffic_potential"].between(0, 1).all()
    assert vocab.groupby("floor")["traffic_potential"].max().eq(1.0).all()
    assert vocab.loc[vocab["main_walkway"], "traffic_potential"].min() >= 0.5
    assert (vocab["corner"] <= (vocab["frontage_sides"] >= 2)).all()


def test_query_filters_aggregates_and_relations(toolkit):
    table = toolkit.unit_table("R2")
    vacant = toolkit.spatial_query(
        "R2", filters={"vacant": True}, group_by="floor", metrics=["count", "sum:area_m2"]
    )
    expected = table[table["vacant"]].groupby("floor")["area_m2"].sum()
    assert {g["floor"]: g["sum_area_m2"] for g in vacant["groups"]} == pytest.approx(
        expected.to_dict(), rel=1e-3
    )
    assert vacant["totals"]["count"] == int(table["vacant"].sum())

    east = toolkit.spatial_query("R2", filters={"floor": "4층", "side": "동쪽"}, limit=200)
    assert east["matched"] > 0 and all("동" in row["side"] for row in east["rows"])
    assert all(row["floor"] == "4F" for row in east["rows"])

    neighbors = toolkit.spatial_query(
        "R2", filters={"adjacent_to": "화장품"}, metrics=["count", "rate:sales_krw/area_m2"]
    )
    cosmetics = set(table.loc[table["category"] == "COSMETICS", "unit_code"])
    pairs = toolkit._adjacency("R2")
    expected_codes = (
        set(pairs.loc[pairs["unit_a"].isin(cosmetics), "unit_b"])
        | set(pairs.loc[pairs["unit_b"].isin(cosmetics), "unit_a"])
    ) - cosmetics
    assert {row["unit_code"] for row in neighbors["rows"]} == expected_codes
    assert neighbors["totals"]["count"] == len(expected_codes)

    near = toolkit.spatial_query(
        "R2", filters={"floor": "3F", "near": {"to": "ESC-A", "within_m": 15}}, sort_by="distance_m"
    )
    distances = [row["distance_m"] for row in near["rows"]]
    assert "3F-C05" in {row["unit_code"] for row in near["rows"]}
    assert distances == sorted(distances) and max(distances) <= 15

    ranked = toolkit.spatial_query("R2", filters={"floor": "4F"}, sort_by="-sales_per_m2", limit=3)
    values = [row["sales_per_m2"] for row in ranked["rows"]]
    assert values == sorted(values, reverse=True) and ranked["truncated"]
    assert any("연 단위로 환산" in note for note in ranked["notes"])

    with pytest.raises(ToolError, match="알 수 없는 필드"):
        toolkit.spatial_query("R2", filters={"height": 3})
    with pytest.raises(ToolError, match="집계는"):
        toolkit.spatial_query("R2", metrics=["avg:area_m2"])


def test_query_on_a_hypothetical_revision_uses_expected_sales(toolkit):
    result = toolkit.spatial_query("H1", filters={"floor": "4F"}, sort_by="-expected_sales_per_m2", limit=2)
    assert result["kind"] == "hypothetical" and "sales_krw" not in result["rows"][0]
    assert any("기대 매출" in note for note in result["notes"])


def test_routes_between_named_places(toolkit):
    route = toolkit.spatial_route("R2", "정문", destination_category="레스토랑", destination_floor="8F")
    assert route["origin"]["kind"] == "entrance" and route["origin"]["floor"] == "1F"
    assert route["destination"]["floor"] == "8F" and route["destination"]["kind"] == "unit"
    assert route["transitions"][0]["via"].startswith("ESC-")
    assert route["transitions"][0]["floors"] == 7 and route["transitions"][0]["height_m"] == 35.0
    assert route["walk_m"] % 5 == 0 and route["walk_m"] > 0 and "segments" not in route
    assert len(route["alternatives"]) == 3
    assert all(alt["walk_m"] >= route["walk_m"] for alt in route["alternatives"])

    drawn = toolkit.route_object("R2", "ENT-1", "3F-C17")
    assert [s["floor"] for s in drawn["segments"]] == ["1F", "3F"]
    assert drawn["destination"]["x"] is not None and drawn["origin"]["label"] == "ENT-1 정문"
    from_escalator = toolkit.route_object("R2", "1F:ESC-A", "3F-C17")
    assert (
        from_escalator["origin"]["label"].startswith("1층 ESC-A") and from_escalator["legs"][0]["walk_m"] == 0
    )

    same_floor = toolkit.route_object("R2", "3F-C05", "3층 화장실", via="elevator")
    assert same_floor["transitions"] == [] and len(same_floor["segments"]) == 1

    with pytest.raises(ToolError, match="찾지 못했습니다"):
        toolkit.spatial_route("R2", "없는 장소", "3F-C05")


def test_describe_floor_gives_a_map_card(toolkit):
    card = toolkit.spatial_describe_floor("R2", "3층")
    assert card["floor"] == "3F" and card["size_m"] == {"width": 120.0, "depth": 80.0}
    assert {m["ref"] for m in card["landmarks"]["escalator"]} == {"ESC-A", "ESC-B"}
    assert "3F-C05" in {u["unit_code"] for u in card["escalator_front"]}
    assert card["zones"] and all(z["sides"] for z in card["zones"])
    assert "에스컬레이터 정면 매장은" in card["summary"] and "도면 기준" in card["summary"]


def test_ui_show_route_draws_a_standalone_path(toolkit):
    handler = UiActionHandler(
        route_provider=lambda rev=None, **params: toolkit.route_object(rev or "R2", **params)
    )
    ui_state = {"rev": "R2", "floor": "3F"}
    result = handler.handle("ui_show_route", {"origin": "정문", "destination": "3F-C17"}, ui_state)
    paths = [p["path"] for p in result.patches]
    assert paths == [
        "/heatmap",
        "/highlights",
        "/edges",
        "/compareRev",
        "/journey",
        "/flow",
        "/route",
        "/floor",
    ]
    assert result.patches[-1]["value"] == "1F" and ui_state["floor"] == "1F"
    output = json.loads(result.output)
    assert output["ok"] and output["walk_m"] > 0 and "segments" not in output and "왼쪽 위" in output["shown"]
    later = handler.handle("ui_highlight_units", {"unit_codes": ["3F-C17"]}, ui_state)
    assert [p["path"] for p in later.patches] == ["/highlights"]  # same answer keeps the route
    failed = handler.handle("ui_show_route", {"origin": "없는 곳", "destination": "3F-C17"}, {"rev": "R2"})
    assert json.loads(failed.output)["ok"] is False and not failed.patches


def test_route_endpoint_and_labels(toolkit):
    client = TestClient(create_api(toolkit))
    body = client.get(
        "/api/spatial/route", params={"rev": "R2", "origin": "ENT-1", "destination": "3F-C17"}
    ).json()
    assert {"title", "origin", "destination", "segments", "transitions", "walk_m", "caveat"} <= set(body)
    assert (
        client.get("/api/spatial/route", params={"origin": "ENT-1", "destination": "XX"}).status_code == 404
    )
    assert tool_label("spatial_describe_floor", {"floor": "4F"}) == "4층 구조 확인"
    assert tool_label(
        "ui_show_route", {"origin": "정문", "destination_category": "레스토랑", "destination_floor": "8F"}
    ) == ("경로 그리기: 정문 → 8층 레스토랑")
    assert tool_label("spatial_query", {"filters": {"floor": "3F", "corner": True}}) == "3층 공간 조건 조회"


def test_specialists_with_spatial_tools_get_every_spatial_tool():
    """docs/specs/mcp-tools.md grants `spatial_*` as a group; agents.yaml must list all of them."""
    from cad2anything.agents.deploy import load_definitions
    from cad2anything.services.mcp_server import TOOL_DESCRIPTIONS

    spatial = {name for name in TOOL_DESCRIPTIONS if name.startswith("spatial_")}
    for agent in load_definitions("chat", "mini"):
        tools = agent.get("mcp_tools")
        if isinstance(tools, list):
            assert set(tools) <= set(TOOL_DESCRIPTIONS), agent["name"]
            if any(t.startswith("spatial_") for t in tools):
                assert spatial <= set(tools), agent["name"]
