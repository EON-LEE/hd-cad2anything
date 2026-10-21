import json

import pytest
from fastapi.testclient import TestClient

from cad2anything.services.app import create_app
from cad2anything.services.atlas_api import create_api
from cad2anything.services.tools import Toolkit


class ScriptedRuntime:
    def __init__(self):
        self.calls = []

    def run(self, message, ui_state, conversation_id=None):
        self.calls.append((message, ui_state, conversation_id))
        yield {"type": "RUN_STARTED", "thread_id": "conv-1", "run_id": "r1"}
        yield {"type": "STATE_DELTA", "delta": [{"op": "replace", "path": "/floor", "value": "4F"}]}
        yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": "m1", "delta": "4층입니다."}
        yield {"type": "RUN_FINISHED", "thread_id": "conv-1", "run_id": "r1"}


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory, populate) -> Toolkit:
    return Toolkit(populate(tmp_path_factory.mktemp("atlas-ws")), use_postgis=False)


@pytest.fixture(scope="module")
def runtime() -> ScriptedRuntime:
    return ScriptedRuntime()


@pytest.fixture(scope="module")
def client(toolkit, runtime) -> TestClient:
    return TestClient(create_api(toolkit, runtime_factory=lambda _: runtime))


def test_meta_and_floor_geojson(client):
    meta = client.get("/api/meta").json()
    assert [r["rev_id"] for r in meta["revisions"]] == ["R0", "R1", "R2", "H1"]
    assert meta["floors"][0]["code"] == "B1" and len(meta["categories"]) == len(meta["categories"])

    floor = client.get("/api/floors/R1/3F").json()
    kinds = {f["properties"]["kind"] for f in floor["features"]}
    assert {"exterior", "walkway", "unit", "vertical_link", "amenity"} <= kinds and "fixture" not in kinds
    unit = next(f for f in floor["features"] if f["properties"]["kind"] == "unit")
    assert unit["geometry"]["type"] == "Polygon" and "brand_name" in unit["properties"]
    with_fixtures = client.get("/api/floors/R1/3F?include=fixtures").json()
    assert any(f["properties"]["kind"] == "fixture" for f in with_fixtures["features"])
    assert client.get("/api/floors/R9/3F").status_code == 404


def test_changes_include_impact(client):
    body = client.get("/api/changes/R0/R1").json()
    assert body["change_set"]["source"] == "truth" and len(body["change_set"]["items"]) == 19
    assert {"area_schedule", "floor_directory_svg"} <= {entry["generator"] for entry in body["impact"]}


def test_artifacts_list_download_and_review(client, toolkit):
    toolkit.registry_run("fixture_schedule", "R1", floors=["B1"])
    toolkit.render_adhoc_table("R1", "amenity", ["floor", "amenity_type"], "편의시설 목록")
    listing = {item["generator"]: item for item in client.get("/api/artifacts/R1").json()}
    assert listing["fixture_schedule"]["status"] == "awaiting_approval"
    adhoc = next(key for key in listing if key.startswith("adhoc--"))
    assert listing[adhoc]["status"] == "draft"

    url = listing["fixture_schedule"]["files"][0]["url"]
    assert client.get(url).status_code == 200
    assert client.get("/api/artifacts/R1/fixture_schedule/artifact.json").status_code == 404
    assert client.get("/api/artifacts/R1/..--..--models/R1.json").status_code in (400, 404)

    review = {"decision": "approve", "reviewer": "공간기획팀", "note": "확인"}
    approved = client.post("/api/artifacts/R1/fixture_schedule/review", json=review)
    assert approved.status_code == 200 and approved.json()["status"] == "published"
    assert client.post("/api/artifacts/R1/fixture_schedule/review", json=review).status_code == 409


def test_productivity_is_annualized_with_drivers_for_low_units(client):
    body = client.get("/api/analytics/productivity", params={"rev": "R1", "floor": "3F"}).json()
    assert body["period"] == {"start": "2025-03-01", "end": "2025-08-31", "days": 184}
    rows = body["rows"]
    assert rows == sorted(rows, key=lambda row: -(row["sales_per_m2"] or 0))
    web_fields = {"unit_code", "brand_name", "category", "sales_krw", "sales_per_m2", "sales_per_pyeong"}
    assert all(web_fields | {"percentile", "low_efficiency", "driver"} <= set(row) for row in rows)
    low = [row for row in rows if row["low_efficiency"]]
    assert low and all(row["driver"] for row in low)
    assert 0 <= min(row["percentile"] for row in rows) <= max(row["percentile"] for row in rows) <= 100
    assert body["metric"]["min"] <= body["metric"]["max"]
    assert client.get("/api/analytics/productivity", params={"rev": "H1"}).status_code == 404


def test_cross_shopping_and_transitions_follow_the_contract(client):
    pairs = client.get(
        "/api/analytics/cross-shopping",
        params={"level": "category", "segment": "age_band:40s;tier:VIP|VVIP"},
    ).json()["pairs"]
    assert pairs and {"a", "b", "a_name", "b_name", "lift", "jaccard", "confidence_ab", "n_customers"} <= set(
        pairs[0]
    )
    focal = client.get(
        "/api/analytics/cross-shopping", params={"level": "category", "focal": "화장품"}
    ).json()
    assert all(pair["a"] == "COSMETICS" for pair in focal["pairs"])
    flows = client.get("/api/analytics/transitions").json()
    assert len(flows["matrix"]) == len(flows["floors"]) and {"share", "base_floor"} <= set(flows["fountain"])
    assert client.get("/api/analytics/cross-shopping", params={"level": "aisle"}).status_code == 400


def test_layout_alternatives_become_hypothetical_revisions(client, toolkit):
    request = {"rev": "R2", "floors": ["4F"], "constraints": {"area_tolerance": 0.25, "n_alternatives": 2}}
    body = client.post("/api/layout/alternatives", json=request).json()
    alternatives = body["alternatives"]
    assert 1 <= len(alternatives) <= 2
    first = alternatives[0]
    assert {
        "id",
        "rev_id",
        "moves",
        "objective_before",
        "objective_after",
        "kpi_delta",
        "constraints",
    } <= set(first)
    assert first["kpi_delta"]["low"] <= first["kpi_delta"]["sales_krw"] <= first["kpi_delta"]["high"]
    assert all(move["to_unit"].startswith("4F-") for move in first["moves"])
    assert all(check["ok"] for check in first["constraints"])
    assert toolkit.ws.model_meta(first["rev_id"])["source"] == "hypothetical"
    floor = client.get(f"/api/floors/{first['rev_id']}/4F").json()
    assert any(f["properties"]["kind"] == "unit" for f in floor["features"])
    bad = client.post(
        "/api/layout/alternatives", json={"rev": "R2", "floors": ["4F"], "constraints": {"x": 1}}
    )
    assert bad.status_code == 400


def test_chat_streams_sse_events(client, runtime):
    response = client.post(
        "/api/chat", json={"message": "4층 보여줘", "ui_state": {"rev": "R1", "floor": "3F"}}
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "STATE_DELTA",
        "TEXT_MESSAGE_CONTENT",
        "RUN_FINISHED",
    ]
    assert runtime.calls[-1][1] == {"rev": "R1", "floor": "3F"}


def test_combined_app_routes_mcp_and_api(toolkit, monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "test-key")
    app = create_app(toolkit, runtime_factory=lambda _: ScriptedRuntime())
    with TestClient(app) as client:
        assert client.get("/healthz").json()["ok"] is True
        assert client.post("/mcp", json={}).status_code == 401
        response = client.post(
            "/mcp",
            headers={"x-api-key": "test-key", "accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "spatial_floor_summary" in names
