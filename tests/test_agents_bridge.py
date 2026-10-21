import json
import re
from pathlib import Path
from types import SimpleNamespace

from cad2anything.agents import SPECIALISTS
from cad2anything.agents.deploy import load_definitions, load_ui_tools, tool_specs
from cad2anything.agents.runtime import AgentRuntime
from cad2anything.agents.ui_actions import UiActionHandler, normalize

REPO = Path(__file__).resolve().parents[1]


class FakeResponses:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["extra_body"]["agent_reference"]["name"]
        return self.scripted[name].pop(0)


class FakeClient:
    def __init__(self, scripted):
        self.responses = FakeResponses(scripted)
        self._count = 0
        self.conversations = SimpleNamespace(create=self._create_conversation)

    def _create_conversation(self):
        self._count += 1
        return SimpleNamespace(id=f"conv-{self._count}")


def _message(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def test_normalize_scales_and_inverts():
    assert normalize({"a": 10, "b": 20, "c": 15}) == {"a": 0.0, "b": 1.0, "c": 0.5}
    assert normalize({"a": 10, "b": 20}, invert=True) == {"a": 1.0, "b": 0.0}
    assert normalize({}) == {}


def test_runtime_handles_mcp_ui_and_specialist_calls():
    scripted = {
        "atlas-orchestrator": [
            {
                "output": [
                    {
                        "type": "mcp_call",
                        "id": "m1",
                        "name": "analytics_productivity",
                        "arguments": '{"rev":"R2"}',
                        "output": "[...]",
                    },
                    {
                        "type": "function_call",
                        "call_id": "f1",
                        "name": "ui_show_heatmap",
                        "arguments": '{"metric":"sales_per_m2","rev":"R2","floor":"4F"}',
                    },
                    {
                        "type": "function_call",
                        "call_id": "f2",
                        "name": "consult_specialist",
                        "arguments": '{"agent":"researcher","request":"최근 국내 백화점 층별 MD 트렌드를 조사해줘"}',
                    },
                ]
            },
            {
                "output": [_message("4층 동측 zone의 평효율이 가장 낮습니다.")],
                "output_text": "4층 동측 zone의 평효율이 가장 낮습니다.",
            },
        ],
        "market-researcher": [{"output_text": "업계 동향 요약(출처 포함)"}],
    }
    client = FakeClient(scripted)
    handler = UiActionHandler(heatmap_provider=lambda metric, rev, floor: {"4F-E01": 100.0, "4F-N02": 300.0})
    runtime = AgentRuntime(client, ui_handler=handler, id_factory=lambda: "id")

    events = list(runtime.run("4층 저효율 zone은?", {"rev": "R2", "floor": "3F"}))

    types = [event["type"] for event in events]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert types.count("TOOL_CALL_START") == 3
    delta = next(event["delta"] for event in events if event["type"] == "STATE_DELTA")
    assert delta[0] == {"op": "replace", "path": "/floor", "value": "4F"}
    assert delta[1]["value"]["values"] == {"4F-E01": 0.0, "4F-N02": 1.0}
    assert delta[2] == {"op": "replace", "path": "/journey", "value": None}
    text = "".join(event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == "4층 동측 zone의 평효율이 가장 낮습니다."

    follow_up = client.responses.calls[2]
    assert follow_up["conversation"] == "conv-1"
    outputs = {item["call_id"]: item["output"] for item in follow_up["input"]}
    assert outputs["f2"] == "업계 동향 요약(출처 포함)"
    assert json.loads(outputs["f1"])["units"] == 2
    specialist_call = client.responses.calls[1]
    assert specialist_call["extra_body"]["agent_reference"]["name"] == SPECIALISTS["researcher"]


def test_specialist_tool_calls_are_streamed_under_the_consult_call():
    scripted = {
        "atlas-orchestrator": [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "consult_specialist",
                        "arguments": '{"agent":"space_productivity_analyst","request":"R2 4F 저효율 매장의 원인을 진단해줘"}',
                    }
                ]
            },
            {"output": [_message("진단 완료")]},
        ],
        "space-productivity-analyst": [
            {
                "output": [
                    {"type": "mcp_call", "id": "s1", "name": "analytics_productivity", "arguments": "{}"},
                    {"type": "web_search_call", "id": "s2", "action": {"query": "백화점 평효율"}},
                    {"type": "code_interpreter_call", "id": "s3", "code": "print(1)", "status": "completed"},
                    {"type": "mcp_call", "id": "s4", "name": "analytics_drivers", "error": "boom"},
                    _message("4F-E01 저효율"),
                ],
                "output_text": "4F-E01 저효율",
            }
        ],
    }
    runtime = AgentRuntime(FakeClient(scripted), id_factory=lambda: "id")

    events = list(runtime.run("4층 진단", {}))

    nested = [e for e in events if e["type"] == "TOOL_CALL_START" and e.get("parent_tool_call_id") == "c1"]
    assert [e["tool_name"] for e in nested] == [
        "analytics_productivity",
        "web_search",
        "code_interpreter",
        "analytics_drivers",
    ]
    assert {e["agent"] for e in nested} == {"space-productivity-analyst"}
    assert nested[1]["args"] == {"query": "백화점 평효율"}
    ends = {e["tool_call_id"]: e["summary"] for e in events if e["type"] == "TOOL_CALL_END"}
    assert ends["s4"].startswith("오류") and ends["c1"].endswith("도구 호출 4회")
    starts = [e["tool_call_id"] for e in events if e["type"] == "TOOL_CALL_START"]
    assert starts == ["c1", "s1", "s2", "s3", "s4"]


def test_ui_clear_all_resets_overlays():
    result = UiActionHandler().handle("ui_clear", {"what": "all"}, {})
    paths = [patch["path"] for patch in result.patches]
    assert paths == ["/heatmap", "/highlights", "/edges", "/journey", "/focus"]


class FakeStreamResponses:
    def __init__(self, scripted):
        self.scripted = scripted
        self.calls = []

    def create(self, **kwargs):
        assert kwargs.get("stream") is True
        self.calls.append(kwargs)
        return iter(self.scripted[kwargs["extra_body"]["agent_reference"]["name"]].pop(0))


def _completed(output, output_text=""):
    return {"type": "response.completed", "response": {"output": output, "output_text": output_text}}


def test_streaming_runtime_forwards_text_and_tool_progress_with_labels():
    mcp_added = {"type": "mcp_call", "id": "m1", "name": "analytics_productivity", "arguments": ""}
    mcp_done = mcp_added | {"arguments": '{"rev":"R2","floor":"4F"}', "output": '{"rows": 50}'}
    floor_call = {
        "type": "function_call",
        "call_id": "f1",
        "name": "ui_set_floor",
        "arguments": '{"floor":"4F"}',
    }
    scripted = {
        "atlas-orchestrator": [
            [
                {"type": "response.created"},
                {"type": "response.output_item.added", "item": mcp_added},
                {"type": "response.output_item.done", "item": mcp_done},
                _completed([mcp_done, floor_call]),
            ],
            [
                {"type": "response.output_text.delta", "delta": "4층 매장 중 "},
                {"type": "response.output_text.delta", "delta": "다섯 곳이 낮아요."},
                _completed([_message("4층 매장 중 다섯 곳이 낮아요.")], "4층 매장 중 다섯 곳이 낮아요."),
            ],
        ]
    }
    client = FakeClient({})
    client.responses = FakeStreamResponses(scripted)
    runtime = AgentRuntime(client, id_factory=lambda: "id", stream=True)

    events = list(runtime.run("4층 효율 낮은 매장", {"floor": "3F"}))

    kinds = [(e["type"], e.get("tool_call_id")) for e in events]
    assert kinds == [
        ("RUN_STARTED", None),
        ("TOOL_CALL_START", "m1"),
        ("TOOL_CALL_END", "m1"),
        ("TOOL_CALL_START", "f1"),
        ("STATE_DELTA", None),
        ("TOOL_CALL_END", "f1"),
        ("TEXT_MESSAGE_START", None),
        ("TEXT_MESSAGE_CONTENT", None),
        ("TEXT_MESSAGE_CONTENT", None),
        ("TEXT_MESSAGE_END", None),
        ("RUN_FINISHED", None),
    ]
    assert events[1]["label"] == "공간 생산성 계산" and events[2]["label"] == "4층 공간 생산성 계산"
    assert events[3]["label"] == "4층으로 이동" and events[5]["ok"] is True
    assert (
        "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
        == "4층 매장 중 다섯 곳이 낮아요."
    )
    follow_up = client.responses.calls[1]["input"]
    assert follow_up == [
        {"type": "function_call_output", "call_id": "f1", "output": '{"ok": true, "floor": "4F"}'}
    ]


def test_streaming_runtime_reports_failed_responses():
    scripted = {"atlas-orchestrator": [[{"type": "error", "message": "rate limited"}]]}
    client = FakeClient({})
    client.responses = FakeStreamResponses(scripted)
    runtime = AgentRuntime(client, id_factory=lambda: "id", stream=True)
    events = []
    try:
        for event in runtime.run("질문", {}):
            events.append(event)
    except RuntimeError as exc:
        assert "rate limited" in str(exc)
    assert events[-1]["type"] == "RUN_ERROR"


def test_ui_show_journey_draws_the_path_and_reports_stops_to_the_model():
    journey = {
        "customer": {"id": "C0000001", "segment": "40대 여성 VIP"},
        "date": "2025-11-08",
        "weekday": "토",
        "stops": [
            {
                "order": 1,
                "time": "11:32",
                "floor": "B1",
                "unit_code": "B1-C05",
                "brand_name": "A",
                "x": 1.0,
                "y": 2.0,
            },
            {
                "order": 2,
                "time": "12:05",
                "floor": "1F",
                "unit_code": "1F-C11",
                "brand_name": "B",
                "x": 3.0,
                "y": 4.0,
            },
        ],
        "segments": [{"floor": "B1", "points": [[1.0, 2.0], [5.0, 2.0]], "from_order": 1, "to_order": 2}],
        "transitions": [
            {"from_order": 1, "to_order": 2, "from_floor": "B1", "to_floor": "1F", "via": "ESC-A"}
        ],
        "summary": {"stops": 2},
        "caveat": "결제 순서",
    }
    calls = []
    handler = UiActionHandler(journey_provider=lambda **params: calls.append(params) or journey)
    ui_state = {"floor": "3F"}
    result = handler.handle(
        "ui_show_journey", {"segment": {"age_band": "40s"}, "rank": 2, "date": ""}, ui_state
    )

    assert calls == [{"segment": {"age_band": "40s"}, "rank": 2}]
    cleared = ["/heatmap", "/highlights", "/edges", "/compareRev"]
    assert [p["path"] for p in result.patches] == [*cleared, "/journey", "/floor", "/panel"]
    assert [p["value"] for p in result.patches[:4]] == [None, [], [], None]
    floor_patch = next(p for p in result.patches if p["path"] == "/floor")
    assert floor_patch["value"] == "B1" and ui_state["floor"] == "B1"
    output = json.loads(result.output)
    assert output["ok"] and [s["unit_code"] for s in output["stops"]] == ["B1-C05", "1F-C11"]
    assert "x" not in output["stops"][0] and "segments" not in output

    failing = UiActionHandler(journey_provider=lambda **params: (_ for _ in ()).throw(ValueError("없음")))
    assert json.loads(failing.handle("ui_show_journey", {}, {}).output)["ok"] is False


def test_ui_show_journey_uses_the_revision_on_screen_and_switches_when_needed():
    calls = []

    def provider(**params):
        calls.append(params)
        rev = "R0" if params.get("customer_id") else params.get("rev", "R2")
        return {"rev": rev, "stops": [{"order": 1, "floor": "1F", "unit_code": "1F-C01"}], "caveat": "추정"}

    handler = UiActionHandler(journey_provider=provider)
    same = handler.handle("ui_show_journey", {"segment": {"tier": ["VIP"]}}, {"rev": "R2", "floor": "3F"})
    assert calls[-1] == {"segment": {"tier": ["VIP"]}, "rev": "R2"}
    assert [p["path"] for p in same.patches][-3:] == ["/journey", "/floor", "/panel"]
    assert "왼쪽" in json.loads(same.output)["shown"]

    ui_state = {"rev": "R2", "floor": "3F"}
    other = handler.handle("ui_show_journey", {"customer_id": "C0000001"}, ui_state)
    assert calls[-1] == {"customer_id": "C0000001"}
    assert [p["path"] for p in other.patches][-4:] == ["/journey", "/rev", "/floor", "/panel"]
    assert ui_state["rev"] == "R0" and "R0 개정으로 바꿨습니다" in json.loads(other.output)["shown"]


def test_other_overlays_replace_a_journey_from_an_earlier_answer_only():
    handler = UiActionHandler(
        journey_provider=lambda **params: {"rev": "R2", "stops": [{"order": 1, "floor": "1F"}]}
    )
    earlier = handler.handle("ui_highlight_units", {"unit_codes": ["3F-C05"]}, {"rev": "R2"})
    assert [p["path"] for p in earlier.patches] == ["/highlights", "/journey"]
    assert earlier.patches[1]["value"] is None
    compare = handler.handle("ui_compare", {"rev_from": "R0", "rev_to": "R1"}, {})
    assert [p["path"] for p in compare.patches] == ["/rev", "/compareRev", "/journey"]

    ui_state = {"rev": "R2", "floor": "3F"}
    handler.handle("ui_show_journey", {}, ui_state)
    same_answer = handler.handle("ui_draw_edges", {"edges": [{"from": "1F-C01", "to": "3F-C05"}]}, ui_state)
    assert [p["path"] for p in same_answer.patches] == ["/edges"]


def test_tool_labels_are_plain_korean():
    from cad2anything.agents.labels import floor_ko, tool_label

    assert floor_ko("B1") == "지하 1층" and floor_ko("8F") == "8층"
    assert (
        tool_label("ui_show_heatmap", {"metric": "low_efficiency", "floor": "4F"})
        == "4층 도면에 '저효율 매장' 색칠"
    )
    assert tool_label("consult_specialist", {"agent": "researcher"}) == "시장 조사 에이전트에게 확인"
    assert tool_label(
        "analytics_cross_shopping", {"segment": {"age_band": "40s", "tier": ["VIP", "VVIP"]}}
    ) == ("40대 VIP·VVIP 교차구매 분석")
    assert tool_label("web_search", {"query": "백화점 MD"}) == "웹 검색: 백화점 MD"
    assert tool_label("spatial_floor_summary", {"rev": "R2", "floor": "4F"}) == "4층 정보 확인"
    assert tool_label("spatial_unit_details", {"rev": "R2", "unit_code": "4F-E05"}) == "4F-E05 매장 확인"
    assert tool_label("something_new", {}) == "작업 중"


def test_table_panels_are_normalized_or_rejected():
    handler = UiActionHandler()
    as_objects = {"title": "저효율", "rows": [{"매장": "4F-E01", "㎡당 매출": 100}, {"매장": "4F-E02"}]}
    result = handler.handle("ui_show_panel", {"type": "table", "payload": as_objects}, {})
    panel = result.patches[0]["value"]["payload"]
    assert panel["columns"] == ["매장", "㎡당 매출"] and panel["rows"] == [["4F-E01", 100], ["4F-E02", None]]
    assert json.loads(result.output) == {"ok": True, "panel": "table", "rows": 2}

    columnar = {"data": {"층": ["3F", "4F"], "매출": [1, 2]}}
    panel = handler.handle("ui_show_panel", {"type": "table", "payload": columnar}, {}).patches[0]["value"]
    assert panel["payload"]["columns"] == ["층", "매출"] and panel["payload"]["rows"] == [
        ["3F", 1],
        ["4F", 2],
    ]

    empty = handler.handle("ui_show_panel", {"type": "table", "payload": {"title": "빈 표"}}, {})
    assert not empty.patches and json.loads(empty.output)["ok"] is False

    changes = handler.handle(
        "ui_show_panel", {"type": "changes", "payload": {"rev_from": "R0", "rev_to": "R1"}}, {}
    )
    assert changes.patches[0]["value"] == {"type": "changes", "payload": {"rev_from": "R0", "rev_to": "R1"}}


def test_heatmaps_carry_legend_hints_for_non_scalar_metrics():
    handler = UiActionHandler(heatmap_provider=lambda metric, rev, floor: {"4F-E01": 1.0, "4F-N02": 0.0})
    result = handler.handle("ui_show_heatmap", {"metric": "low_efficiency", "rev": "R2", "floor": "4F"}, {})
    heatmap = next(p["value"] for p in result.patches if p["path"] == "/heatmap")
    assert heatmap["legend"]["binary"] is True and json.loads(result.output)["flagged_units"] == ["4F-E01"]
    sales = handler.handle("ui_show_heatmap", {"metric": "sales_per_m2", "rev": "R2", "floor": "4F"}, {})
    assert "legend" not in next(p["value"] for p in sales.patches if p["path"] == "/heatmap")


def test_consult_rejects_empty_requests_and_reuses_repeated_ones():
    consult = {
        "type": "function_call",
        "name": "consult_specialist",
        "arguments": '{"agent":"space_productivity_analyst","request":"R2 4F 저효율 매장의 원인을 진단해줘"}',
    }
    junk = {
        "type": "function_call",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"아니오"}',
    }
    scripted = {
        "atlas-orchestrator": [
            {"output": [consult | {"call_id": "a"}, consult | {"call_id": "b"}, junk | {"call_id": "c"}]},
            {"output": [_message("완료")]},
        ],
        "space-productivity-analyst": [{"output_text": "진단 결과"}],
    }
    client = FakeClient(scripted)
    events = list(AgentRuntime(client, id_factory=lambda: "id").run("4층 진단", {}))

    specialist_calls = [
        c
        for c in client.responses.calls
        if c["extra_body"]["agent_reference"]["name"] != "atlas-orchestrator"
    ]
    assert len(specialist_calls) == 1
    outputs = {item["call_id"]: item["output"] for item in client.responses.calls[-1]["input"]}
    assert outputs["a"] == outputs["b"] == "진단 결과"
    assert json.loads(outputs["c"])["ok"] is False
    summaries = {e["tool_call_id"]: e["summary"] for e in events if e["type"] == "TOOL_CALL_END"}
    assert "재사용" in summaries["b"] and "상담하지 않음" in summaries["c"]


def _documented_mcp_tools() -> set[str]:
    text = (REPO / "docs" / "specs" / "mcp-tools.md").read_text(encoding="utf-8")
    section = text.split("## 2.")[1].split("## 3.")[0]
    return set(re.findall(r"\| [a-z]+ \| `([a-z_]+)`", section))


def test_agent_tool_selection_matches_contract():
    definitions = {d["name"]: d for d in load_definitions("chat", "mini")}
    ui_tools = load_ui_tools()
    documented = _documented_mcp_tools()
    assert len(documented) >= 20

    for agent in definitions.values():
        specs = tool_specs(agent, ui_tools, "https://mcp.example/mcp", "conn-id", None)
        for spec in specs:
            if spec["kind"] == "mcp" and spec["allowed_tools"] is not None:
                assert set(spec["allowed_tools"]) <= documented, agent["name"]
                assert spec["project_connection_id"] == "conn-id" and "headers" not in spec

    orchestrator = tool_specs(
        definitions["atlas-orchestrator"], ui_tools, "https://mcp.example/mcp", None, "k"
    )
    names = [spec.get("name", spec["kind"]) for spec in orchestrator]
    assert "consult_specialist" in names and "ui_show_heatmap" in names and "web_search" not in names
    assert orchestrator[0]["headers"] == {"x-api-key": "k"}

    researcher = tool_specs(definitions["market-researcher"], ui_tools, None, None, None)
    assert [spec["kind"] for spec in researcher] == ["web_search"]
    assert definitions["generator"]["model"] == "mini"
    assert set(SPECIALISTS.values()) <= set(definitions)
