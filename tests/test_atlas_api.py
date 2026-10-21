import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from cad2anything.mockgen.drawings import build_all
from cad2anything.services.app import create_app
from cad2anything.services.atlas_api import create_api
from cad2anything.services.tools import Toolkit
from cad2anything.workspace import Workspace


class ScriptedRuntime:
    def __init__(self):
        self.calls = []

    def run(self, message, ui_state, conversation_id=None):
        self.calls.append((message, ui_state, conversation_id))
        yield {"type": "RUN_STARTED", "thread_id": "conv-1", "run_id": "r1"}
        yield {"type": "STATE_DELTA", "delta": [{"op": "replace", "path": "/floor", "value": "4F"}]}
        yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": "m1", "delta": "4층입니다."}
        yield {"type": "RUN_FINISHED", "thread_id": "conv-1", "run_id": "r1"}


def _business_tables(model) -> dict[str, pd.DataFrame]:
    units = [u for u in model.units() if not u.attributes.get("vacant")]
    tenancies = pd.DataFrame(
        [
            [u.attributes["brand_id"], u.attributes["unit_code"], "R1", "2025-03-01", "2025-08-31", 0.3]
            for u in units
        ],
        columns=["brand_id", "unit_code", "rev_id", "valid_from", "valid_to", "commission_rate"],
    )
    terminals = pd.DataFrame(
        [
            [f"P{i:04d}", u.attributes["brand_id"], u.attributes["unit_code"], "2024-10-01", "2026-09-30"]
            for i, u in enumerate(units)
        ],
        columns=["pos_id", "brand_id", "unit_code", "valid_from", "valid_to"],
    )
    rows = []
    for i in range(len(units)):
        amount = 100_000 + (i % 7) * 10_000
        for day in (2, 3):
            rows.append([f"T{i}-{day}", f"2025-03-{day:02d} 12:00:00", f"P{i:04d}", amount, 50.0])
    transactions = pd.DataFrame(rows, columns=["txn_id", "ts", "pos_id", "amount_krw", "weight"])
    return {"tenancies": tenancies, "pos_terminals": terminals, "transactions": transactions}


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory) -> Toolkit:
    root = tmp_path_factory.mktemp("atlas-ws")
    workspace = Workspace(root)
    _, models, changes = build_all(42)
    for model in models.values():
        workspace.save_model(model, {"source": "truth"})
    for change_set in changes.values():
        workspace.save_change_set(change_set, {"source": "truth"})
    for name, frame in _business_tables(models["R1"]).items():
        frame.to_csv(root / "business" / f"{name}.csv", index=False)
    return Toolkit(workspace, use_postgis=False)


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


def test_productivity_uses_weights_and_ranks_within_floor(client):
    body = client.get("/api/analytics/productivity", params={"rev": "R1", "floor": "3F"}).json()
    rows = body["rows"]
    assert body["window"] == ["2025-03-01", "2025-08-31"]
    assert rows == sorted(rows, key=lambda row: -row["sales_per_m2"])
    occupied = [row for row in rows if not row["vacant"]]
    assert occupied[0]["sales_krw"] % 50 == 0 and occupied[0]["aov_krw"] >= 100_000
    assert any(row["low_efficiency"] for row in occupied)
    assert client.get("/api/analytics/productivity", params={"rev": "H1"}).status_code == 404


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
