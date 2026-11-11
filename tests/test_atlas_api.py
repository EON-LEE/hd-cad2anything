import asyncio
import json
import logging
import sys
import time
from datetime import date
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from cad2anything.model import Revision, RevisionKind, SpatialModel
from cad2anything.services import app as server_app
from cad2anything.services import atlas_api
from cad2anything.services.app import create_app
from cad2anything.services.atlas_api import create_api
from cad2anything.services.errors import DecisionExistsError, NotFoundToolError, ToolError
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


def test_chat_emits_keepalives_during_slow_run(toolkit, monkeypatch):
    class SlowRuntime:
        def run(self, _message, _ui_state, conversation_id=None):
            yield {"type": "RUN_STARTED", "thread_id": "conv-slow", "run_id": "r1"}
            time.sleep(0.06)
            yield {"type": "RUN_FINISHED", "thread_id": "conv-slow", "run_id": "r1"}

    monkeypatch.setattr(atlas_api, "CHAT_KEEPALIVE_S", 0.01)
    monkeypatch.setenv("CHAT_TURN_DEADLINE_S", "1")
    response = TestClient(create_api(toolkit, runtime_factory=lambda _: SlowRuntime())).post(
        "/api/chat", json={"message": "진행 상태"}
    )

    assert response.status_code == 200
    assert ": keepalive\n\n" in response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_FINISHED"]


def test_chat_deadline_emits_error_and_cancels_runtime(toolkit, monkeypatch, caplog):
    class WaitingRuntime:
        cancel = None

        def run(self, _message, _ui_state, conversation_id=None, cancel=None):
            self.cancel = cancel
            yield {"type": "RUN_STARTED", "thread_id": "conv-wait", "run_id": "r1"}
            cancel.wait(1)

    runtime = WaitingRuntime()
    monkeypatch.setattr(atlas_api, "CHAT_KEEPALIVE_S", 0.01)
    monkeypatch.setenv("CHAT_TURN_DEADLINE_S", "0.07")
    caplog.set_level(logging.INFO, logger="cad2anything.services.atlas_api")
    response = TestClient(create_api(toolkit, runtime_factory=lambda _: runtime)).post(
        "/api/chat", json={"message": "기밀 질문"}
    )

    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == {"type": "RUN_ERROR", "message": atlas_api.CHAT_DEADLINE_MESSAGE}
    assert not any(event["type"] == "RUN_FINISHED" for event in events)
    assert runtime.cancel is not None and runtime.cancel.is_set()
    assert "outcome=deadline error_type=TimeoutError" in caplog.text
    assert "기밀 질문" not in caplog.text


def test_chat_runtime_failure_is_generic_and_logs_only_error_type(toolkit, caplog):
    class ErrorRuntime:
        def run(self, _message, _ui_state, conversation_id=None):
            yield {"type": "RUN_STARTED", "thread_id": conversation_id, "run_id": "r1"}
            raise RuntimeError("private error details")

    caplog.set_level(logging.INFO, logger="cad2anything.services.atlas_api")
    response = TestClient(create_api(toolkit, runtime_factory=lambda _: ErrorRuntime())).post(
        "/api/chat", json={"message": "기밀 질문", "thread_id": "unsafe\nidentifier"}
    )
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1] == {"type": "RUN_ERROR", "message": atlas_api.CHAT_ERROR_MESSAGE}
    assert "outcome=error error_type=RuntimeError" in caplog.text
    assert "thread_id=none" in caplog.text
    assert "기밀 질문" not in caplog.text
    assert "private error details" not in caplog.text
    assert "unsafe" not in caplog.text


def test_chat_disconnect_cancels_worker(toolkit):
    class WaitingRuntime:
        cancel = None

        def run(self, _message, _ui_state, conversation_id=None, cancel=None):
            self.cancel = cancel
            yield {"type": "RUN_STARTED", "thread_id": "conv-disconnect", "run_id": "r1"}
            cancel.wait(1)

    runtime = WaitingRuntime()
    api = create_api(toolkit, runtime_factory=lambda _: runtime)
    endpoint = next(route.endpoint for route in api.routes if getattr(route, "path", "") == "/api/chat")
    response = endpoint(atlas_api.ChatRequest(message="연결 종료"))

    async def disconnect():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)

    asyncio.run(disconnect())
    assert runtime.cancel is not None and runtime.cancel.is_set()


def test_health_and_meta_report_cached_mirror_outage_without_failing_readiness(tmp_path):
    class BrokenMirror:
        def health(self):
            return {
                "db": "down",
                "mirror_errors": 2,
                "last_mirror_error": "OperationalError",
                "last_mirror_error_at": "2026-01-01T00:00:00+00:00",
            }

    ws = Workspace(tmp_path)
    ws.save_model(SpatialModel(Revision("R0", effective_date="2025-01-01")))
    ws.mirror = BrokenMirror()
    toolkit = type("FakeToolkit", (), {"ws": ws, "data_through": lambda self: None})()
    client = TestClient(create_api(toolkit))

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["workspace_loaded"] is True
    assert ready.json()["db"] == "down"
    assert ready.json()["mirror_errors"] == 2
    assert ready.json()["last_mirror_error_at"] == "2026-01-01T00:00:00+00:00"
    assert client.get("/healthz").json()["ok"] is True
    meta = client.get("/api/meta").json()
    assert meta["writes_available"] is False
    assert meta["mirror_errors"] == 2
    assert meta["last_mirror_error"] == "OperationalError"

    empty = TestClient(create_api(type("FakeToolkit", (), {"ws": Workspace(tmp_path / "empty")})()))
    assert empty.get("/healthz").status_code == 200
    not_ready = empty.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["workspace_loaded"] is False


def test_meta_has_sales_only_for_revisions_with_observed_transactions(tmp_path):
    ws = Workspace(tmp_path)
    for rev_id, effective_date in (
        ("R0", "2025-01-01"),
        ("R1", "2025-03-01"),
        ("R2", "2025-06-01"),
        ("R3", "2026-01-01"),
    ):
        ws.save_model(SpatialModel(Revision(rev_id, effective_date=effective_date)))
    ws.save_model(SpatialModel(Revision("H1", kind=RevisionKind.HYPOTHETICAL)))
    ws.write_text("business/transactions.csv", "ts\n2025-02-01T12:00:00\n2025-07-01T12:00:00\n")
    toolkit = type("FakeToolkit", (), {"ws": ws, "data_through": lambda self: date(2025, 7, 1)})()
    client = TestClient(create_api(toolkit))
    revisions = {rev["rev_id"]: rev for rev in client.get("/api/meta").json()["revisions"]}

    assert {rev: data["has_sales"] for rev, data in revisions.items()} == {
        "R0": True,
        "R1": False,
        "R2": True,
        "R3": False,
        "H1": False,
    }
    assert revisions["R2"]["kind"] == "actual"
    assert revisions["H1"]["kind"] == "hypothetical"

    ws.write_text(
        "business/transactions.csv",
        "ts\n2025-02-01T12:00:00\n2025-04-10T12:00:00\n2025-07-01T12:00:00\n",
    )
    assert {r["rev_id"]: r["has_sales"] for r in client.get("/api/meta").json()["revisions"]}["R1"]


def test_foundry_runtime_factory_marks_hypothetical_sales_as_estimates(monkeypatch):
    from cad2anything.agents.runtime import AgentRuntime

    azure = ModuleType("azure")
    azure.__path__ = []
    identity = ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda: object()
    azure.identity = identity
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test")
    captured = {}
    monkeypatch.setattr(
        AgentRuntime,
        "from_project",
        lambda endpoint, credential, ui_handler=None: captured.update(handler=ui_handler) or object(),
    )
    toolkit = SimpleNamespace(_has_sales=lambda rev: rev != "H1")

    server_app.foundry_runtime_factory(toolkit)

    assert captured["handler"]._estimated("sales_per_m2", "H1") is True
    assert captured["handler"]._estimated("sales_per_m2", "R2") is False


def test_relayout_plan_conflict_maps_to_409_and_replacement_passes_through(toolkit, monkeypatch):
    calls = []

    def stub_plan(brief_id, **params):
        calls.append((brief_id, params))
        if not params["replace_decision"]:
            raise DecisionExistsError("기존 선택을 유지하려면 변경을 취소해 주세요.")
        return {"reused": False}

    monkeypatch.setattr(toolkit, "relayout_plan", stub_plan)
    client = TestClient(create_api(toolkit))
    conflict = client.post("/api/relayout/FW2026/plan", json={"max_moves": 5})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "기존 선택을 유지하려면 변경을 취소해 주세요."
    accepted = client.post(
        "/api/relayout/FW2026/plan",
        json={"replace_decision": True, "as_of": "2025-08-31", "max_moves": 5},
    )
    assert accepted.json() == {"reused": False}
    assert calls[-1] == (
        "FW2026",
        {
            "objective": None,
            "n_alternatives": 3,
            "max_moves": 5,
            "downtime_weeks": None,
            "movable": None,
            "as_of": "2025-08-31",
            "replace_decision": True,
            "extra_exits": [],
            "extra_entries": [],
        },
    )

    def missing(*_, **__):
        raise NotFoundToolError("missing")

    monkeypatch.setattr(toolkit, "relayout_plan", missing)
    assert client.post("/api/relayout/missing/plan", json={}).status_code == 404

    def invalid(*_, **__):
        raise ToolError("invalid")

    monkeypatch.setattr(toolkit, "relayout_plan", invalid)
    assert client.post("/api/relayout/FW2026/plan", json={}).status_code == 400


def test_relayout_select_records_api_decision_source(toolkit, monkeypatch):
    captured = []
    monkeypatch.setattr(
        toolkit,
        "relayout_select",
        lambda *args, **kwargs: captured.append((args, kwargs)) or {"ok": True},
    )
    client = TestClient(create_api(toolkit))
    assert client.post(
        "/api/relayout/FW2026/select", json={"alt_id": "ALT-1", "decided_by": "테스트 사용자"}
    ).json() == {"ok": True}
    assert captured == [(("FW2026", "ALT-1", "테스트 사용자"), {"decision_source": "api"})]


def test_combined_app_routes_mcp_and_api(toolkit, monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "test-key")
    monkeypatch.setenv("ATLAS_WARMUP", "0")
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


def test_app_starts_with_db_unavailable_and_reports_not_ready_until_workspace_loaded(tmp_path, monkeypatch):
    class OfflineMirror:
        backend = "postgres"

        def pull(self, _root):
            raise OSError("synthetic database outage")

        def health(self):
            return {"db": "down", "mirror_errors": 0}

        def healthcheck(self):
            return False

    workspace = Workspace(tmp_path, mirror=OfflineMirror())
    monkeypatch.setattr(server_app, "workspace_from_env", lambda: workspace)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.setenv("ATLAS_WARMUP", "0")
    with TestClient(create_app(runtime_factory=lambda _: ScriptedRuntime())) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert client.get("/readyz").json()["mirror_errors"] == 1


def test_monitor_retries_pending_files_after_db_recovers(tmp_path):
    class RecoveringMirror:
        backend = "postgres"
        online = False

        def __init__(self):
            self.files = {}

        def push(self, path, root):
            if not self.online:
                raise OSError("synthetic outage")
            self.files[path.relative_to(root).as_posix()] = path.read_bytes()

        def health(self):
            return {"db": "ok" if self.online else "down", "mirror_errors": 0}

        def healthcheck(self):
            return self.online

    class OneIteration:
        calls = 0

        def wait(self, _interval):
            self.calls += 1
            return self.calls > 1

    ws = Workspace(tmp_path)
    ws.save_model(SpatialModel(Revision("R0")))
    mirror = RecoveringMirror()
    ws.mirror = mirror
    with pytest.raises(OSError):
        ws.write_text("reports/R0.json", "{}")
    assert ws.mirror_status()["pending_mirror_writes"] == 1

    mirror.online = True
    server_app.monitor_mirror(ws, OneIteration(), interval_s=0)
    assert mirror.files["reports/R0.json"] == b"{}"
    assert ws.mirror_status()["pending_mirror_writes"] == 0
    assert ws.mirror_status()["writes_available"] is True


def test_monitor_retries_partial_startup_pull_after_pending_writes(tmp_path):
    class PartialMirror:
        backend = "postgres"
        online = False

        def __init__(self):
            self.calls = []

        def push(self, path, root):
            if not self.online:
                raise OSError("synthetic outage")
            self.calls.append(("push", path.relative_to(root).as_posix()))

        def pull(self, _root):
            self.calls.append(("pull", None))
            return 0

        def healthcheck(self):
            return self.online

    class OneIteration:
        calls = 0

        def wait(self, _interval):
            self.calls += 1
            return self.calls > 1

    ws = Workspace(tmp_path)
    ws.save_model(SpatialModel(Revision("R0")))
    mirror = PartialMirror()
    ws.mirror = mirror
    ws.mirror_pull_pending = True
    with pytest.raises(OSError):
        ws.write_text("reports/R0.json", "{}")

    mirror.online = True
    server_app.monitor_mirror(ws, OneIteration(), interval_s=0)
    assert mirror.calls == [("push", "reports/R0.json"), ("pull", None)]
    assert ws.mirror_pull_pending is False


def test_monitor_does_not_pull_remote_over_a_write_that_still_cannot_publish(tmp_path):
    class Mirror:
        backend = "postgres"

        def __init__(self):
            self.calls = []

        def push(self, path, root):
            self.calls.append(("push", path.relative_to(root).as_posix()))
            raise OSError("synthetic outage")

        def pull(self, _root):
            self.calls.append(("pull", None))
            return 1

        def healthcheck(self):
            return True

    class OneIteration:
        calls = 0

        def wait(self, _interval):
            self.calls += 1
            return self.calls > 1

    ws = Workspace(tmp_path)
    ws.save_model(SpatialModel(Revision("R0")))
    mirror = Mirror()
    ws.mirror = mirror
    ws.mirror_pull_pending = True
    with pytest.raises(OSError):
        ws.write_text("reports/R0.json", "local")

    server_app.monitor_mirror(ws, OneIteration(), interval_s=0)

    assert mirror.calls == [("push", "reports/R0.json"), ("push", "reports/R0.json")]
    assert ws.mirror_status()["pending_mirror_writes"] == 1
    assert ws.mirror_pull_pending is True


def test_logging_configuration_runs_once_with_env_level(monkeypatch):
    calls = []
    monkeypatch.setattr(server_app, "_logging_configured", False)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(server_app.logging, "basicConfig", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(server_app.logger, "setLevel", lambda level: calls.append(level))

    server_app.configure_logging()
    server_app.configure_logging()
    assert len(calls) == 2
    assert calls[0]["level"] == logging.DEBUG
    assert calls[1] == logging.DEBUG
