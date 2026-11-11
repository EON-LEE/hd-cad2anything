import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cad2anything.agents import SPECIALISTS
from cad2anything.agents import deploy as deploy_module
from cad2anything.agents.deploy import load_definitions, load_ui_tools, tool_specs
from cad2anything.agents.runtime import (
    INCOMPLETE_ANSWERS,
    MAX_ROUNDS,
    RESUME_NOTE,
    SPECIALIST_OUTPUT_LIMIT,
    AgentRuntime,
    agent_reference,
    agent_versions_from_env,
    failure_kind,
    turn_budget_from_env,
    with_sources,
)
from cad2anything.agents.ui_actions import UiActionHandler, normalize, screen_state

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


@pytest.mark.parametrize("capture", [False, True])
def test_raw_tool_outputs_are_only_in_opt_in_evaluation_traces(capture):
    client = FakeClient(
        {
            "atlas-orchestrator": [
                {
                    "output": [
                        {
                            "type": "mcp_call",
                            "id": "m1",
                            "name": "spatial_query",
                            "arguments": "{}",
                            "output": '{"matched": 5}',
                        },
                        {
                            "type": "function_call",
                            "call_id": "f1",
                            "name": "ui_set_floor",
                            "arguments": '{"floor": "4F"}',
                        },
                    ]
                },
                {"output": [_message("4층이에요.")]},
            ]
        }
    )
    events = list(AgentRuntime(client, capture_tool_outputs=capture).run("4층", {}))
    ends = {e["tool_call_id"]: e for e in events if e["type"] == "TOOL_CALL_END"}
    assert all(("output" in e) is capture for e in ends.values())
    if capture:
        assert ends["m1"]["output"] == {"matched": 5}
        assert ends["f1"]["output"] == {"ok": True, "floor": "4F"}


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
    assert paths == ["/heatmap", "/highlights", "/edges", "/journey", "/route", "/flow", "/focus"]
    flow = UiActionHandler().handle("ui_clear", {"what": "flow"}, {})
    assert flow.patches == [{"op": "replace", "path": "/flow", "value": None}]


def test_ui_show_flow_draws_a_standalone_overlay():
    flow = {
        "mode": "popup",
        "title": "팝업 후보",
        "rev": "R2",
        "query": {"floor": None},
        "floors": {"3F": {"segments": [], "max_value": 10, "total": 50}, "4F": {"segments": [], "total": 90}},
        "markers": [{"floor": "3F", "x": 1.0, "y": 2.0, "rank": 1, "kind": "spot", "title": "후보 1"}],
        "legend": {"low": "적음", "high": "많음"},
        "summary": {"candidates": 1},
        "caveat": "추정",
    }
    calls = []
    handler = UiActionHandler(flow_provider=lambda **params: calls.append(params) or flow)
    ui_state = {"rev": "R2", "floor": "1F"}
    result = handler.handle("ui_show_flow", {"mode": "popup", "category": "화장품", "hours": ""}, ui_state)
    assert calls == [{"rev": "R2", "mode": "popup", "category": "화장품"}]
    paths = [p["path"] for p in result.patches]
    assert paths == [
        "/heatmap",
        "/highlights",
        "/edges",
        "/compareRev",
        "/journey",
        "/route",
        "/flow",
        "/floor",
    ]
    assert result.patches[-1]["value"] == "3F" and ui_state["floor"] == "3F"
    output = json.loads(result.output)
    assert output["ok"] and output["markers"][0]["title"] == "후보 1" and "floors" not in output
    assert "팝업 후보" in output["shown"]
    same_answer = handler.handle("ui_highlight_units", {"unit_codes": ["3F-C05"]}, ui_state)
    assert [p["path"] for p in same_answer.patches] == ["/highlights"]
    traffic = UiActionHandler(flow_provider=lambda **params: {**flow, "mode": "traffic", "markers": []})
    state = {"rev": "R2", "floor": "1F"}
    handler_result = traffic.handle("ui_show_flow", {"mode": "traffic"}, state)
    assert state["floor"] == "4F" and json.loads(handler_result.output)["mode"] == "traffic"
    on_alternative = {"rev": "FW2026-A2", "floor": "4F"}
    switched = traffic.handle("ui_show_flow", {"mode": "traffic"}, on_alternative)
    assert {"op": "replace", "path": "/rev", "value": "R2"} in switched.patches
    assert on_alternative["rev"] == "R2" and "R2 개정" in json.loads(switched.output)["shown"]
    failing = UiActionHandler(flow_provider=lambda **params: (_ for _ in ()).throw(ValueError("없음")))
    failed = failing.handle("ui_show_flow", {"mode": "egress"}, {})
    assert json.loads(failed.output)["ok"] is False and not failed.patches
    assert json.loads(UiActionHandler().handle("ui_show_flow", {"mode": "traffic"}, {}).output)["ok"] is False


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
        ("TEXT_MESSAGE_END", None),
        ("RUN_FINISHED", None),
    ]
    assert events[1]["label"] == "공간 생산성 계산" and events[2]["label"] == "4층 공간 생산성 계산"
    assert events[1]["method"].startswith("결제 실적을 연 매출로 환산해")
    assert events[3]["label"] == "4층으로 이동" and events[5]["ok"] is True
    assert events[5]["result"] is None  # the label already says what a UI action did
    # A short answer is held until the response ends (it could still turn out to be a progress note).
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
    assert "요청 한도" in events[-1]["message"]
    assert "rate limited" not in events[-1]["message"]


def _added(item):
    return {"type": "response.output_item.added", "item": item}


def _deltas(text, size=7):
    return [
        {"type": "response.output_text.delta", "delta": text[i : i + size]} for i in range(0, len(text), size)
    ]


def test_preamble_before_a_tool_becomes_a_progress_note_and_the_answer_follows_the_tools():
    plan = "4층 매장의 ㎡당 매출을 계산하고 하위 매장의 원인을 나눠 볼게요."
    answer = (
        "4층에서 효율이 가장 낮은 곳은 란제리 매장 다섯 곳이에요.\n\n- Ravandre Nuit(4F-E05)가 가장 낮아요."
    )
    output = {
        "rev": "R2",
        "level": "unit",
        "summary": {"rows": 50, "low_efficiency": 5},
        "low_efficiency": [
            {"unit_code": "4F-E02", "brand_name": "Merelle Nuit", "sales_per_m2": 5470000},
            {"unit_code": "4F-E05", "brand_name": "Ravandre Nuit", "sales_per_m2": 4030000},
        ],
    }
    mcp_added = {"type": "mcp_call", "id": "m1", "name": "analytics_productivity", "arguments": ""}
    mcp_done = mcp_added | {
        "arguments": '{"rev":"R2","floor":"4F"}',
        "output": json.dumps(output, ensure_ascii=False),
    }
    failing = {
        "type": "mcp_call",
        "id": "m2",
        "name": "spatial_query",
        "arguments": '{"rev":"R2"}',
        "output": '{"ok": false, "tool": "spatial_query", "error": "필드가 없어요"}',
    }
    scripted = {
        "atlas-orchestrator": [
            [
                _added({"type": "message"}),
                *_deltas(plan),
                _added(mcp_added),
                {"type": "response.output_item.done", "item": mcp_done},
                {"type": "response.output_item.done", "item": failing},
                _added({"type": "message"}),
                *_deltas(answer),
                _completed([mcp_done, failing, _message(answer)]),
            ]
        ]
    }
    client = FakeClient({})
    client.responses = FakeStreamResponses(scripted)
    runtime = AgentRuntime(client, id_factory=lambda: "id", stream=True)

    events = list(runtime.run("4층 효율 낮은 매장", {"floor": "3F"}))

    types = [e["type"] for e in events]
    note = types.index("THINKING_TEXT_MESSAGE_START")
    assert types[note : note + 4] == [
        "THINKING_TEXT_MESSAGE_START",
        "THINKING_TEXT_MESSAGE_CONTENT",
        "THINKING_TEXT_MESSAGE_END",
        "TOOL_CALL_START",
    ]
    assert events[note + 1]["delta"] == plan
    text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == answer and plan not in text
    # Unlabelled text is decided when the next output item arrives, so the answer starts after the tools.
    assert types.index("TEXT_MESSAGE_START") > max(i for i, t in enumerate(types) if t == "TOOL_CALL_END")
    ends = {e["tool_call_id"]: e for e in events if e["type"] == "TOOL_CALL_END"}
    assert ends["m1"]["ok"] is True
    assert ends["m1"]["result"] == "매장 50곳 중 저효율 5곳 · 가장 낮은 곳 Ravandre Nuit(4F-E05)"
    assert ends["m2"]["ok"] is False and ends["m2"]["result"] == "조건을 고쳐 다시 시도해요"
    assert ends["m2"]["method"] == "도면에서 계산한 매장 속성으로 거르고 묶어 집계해요"


def test_specialist_preambles_are_nested_notes_and_not_part_of_its_answer():
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 MD 개편 공개 사례를 조사해줘"}',
    }
    floor = {"type": "function_call", "call_id": "f1", "name": "ui_set_floor", "arguments": '{"floor":"4F"}'}
    scripted = {
        "atlas-orchestrator": [
            {"output": [_message("공개 사례를 먼저 찾아볼게요."), consult]},
            {"output": [floor]},
            {"output": [_message("국내 백화점은 시즌마다 층별 MD를 바꿔요.")]},
        ],
        "market-researcher": [
            {
                "output": [
                    _message("백화점 개편 기사를 검색할게요."),
                    {"type": "web_search_call", "id": "w1", "action": {"query": "백화점 MD 개편"}},
                    _message("조사 결과: 3건"),
                ]
            }
        ],
    }
    client = FakeClient(scripted)
    events = list(AgentRuntime(client, id_factory=lambda: "id").run("MD 개편 사례", {}))

    notes = [e for e in events if e["type"] == "THINKING_TEXT_MESSAGE_START"]
    contents = [e["delta"] for e in events if e["type"] == "THINKING_TEXT_MESSAGE_CONTENT"]
    assert contents == ["공개 사례를 먼저 찾아볼게요.", "백화점 개편 기사를 검색할게요."]
    assert "parent_tool_call_id" not in notes[0]
    assert notes[1]["parent_tool_call_id"] == "c1" and notes[1]["agent"] == "market-researcher"
    specialist_input = client.responses.calls[2]["input"]
    assert specialist_input[0]["output"] == "조사 결과: 3건"
    ends = {e["tool_call_id"]: e for e in events if e["type"] == "TOOL_CALL_END"}
    assert ends["c1"]["result"] == "시장 조사 에이전트의 분석을 받았어요"
    assert ends["c1"]["method"].startswith("시장 조사 에이전트가")
    text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == "국내 백화점은 시즌마다 층별 MD를 바꿔요."


def test_answer_written_before_a_last_ui_call_is_still_the_answer():
    floor = {"type": "function_call", "call_id": "f1", "name": "ui_set_floor", "arguments": '{"floor":"4F"}'}
    scripted = {
        "atlas-orchestrator": [
            {"output": [_message("4층 효율이 가장 낮아요."), floor]},
            {"output": []},
        ]
    }
    events = list(AgentRuntime(FakeClient(scripted), id_factory=lambda: "id").run("4층", {}))
    text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == "4층 효율이 가장 낮아요." and events[-1]["type"] == "RUN_FINISHED"


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
    assert [p["path"] for p in result.patches] == [
        *cleared,
        "/route",
        "/flow",
        "/journey",
        "/floor",
        "/panel",
    ]
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
    assert [p["path"] for p in earlier.patches] == ["/highlights", "/journey", "/route", "/flow"]
    assert all(p["value"] is None for p in earlier.patches[1:])
    compare = handler.handle("ui_compare", {"rev_from": "R0", "rev_to": "R1"}, {})
    assert [p["path"] for p in compare.patches] == ["/rev", "/compareRev", "/journey", "/route", "/flow"]

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
    assert json.loads(result.output) == {
        "ok": True,
        "panel": "table",
        "rows": 2,
        "shown": "도면 아래 표 패널에 표를 열었습니다.",
    }

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
    assert "개정 검토 보기" in json.loads(changes.output)["shown"]
    assert "열었습니다" not in json.loads(changes.output)["shown"]


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


class FlakyResponses(FakeResponses):
    """Fails the first specialist call (a stream that breaks mid-way) or every one."""

    def __init__(self, scripted, failures, error=None):
        super().__init__(scripted)
        self.failures = failures
        self.error = error

    def create(self, **kwargs):
        name = kwargs["extra_body"]["agent_reference"]["name"]
        if name != "atlas-orchestrator" and self.failures > 0:
            self.calls.append(kwargs)
            self.failures -= 1
            raise self.error or ConnectionError("stream broke")
        return super().create(**kwargs)


def test_failed_specialist_is_retried_then_answered_without_it():
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    for failures, expected_ok in ((1, True), (2, False)):
        scripted = {
            "atlas-orchestrator": [{"output": [consult]}, {"output": [_message("답")]}],
            "market-researcher": [{"output_text": "조사 결과"}],
        }
        client = FakeClient(scripted)
        client.responses = FlakyResponses(scripted, failures)
        events = list(AgentRuntime(client, id_factory=lambda: "id").run("벤치마크와 비교", {}))

        assert events[-1]["type"] == "RUN_FINISHED"
        researcher_calls = [
            c
            for c in client.responses.calls
            if c["extra_body"]["agent_reference"]["name"] == "market-researcher"
        ]
        assert len(researcher_calls) == 2
        output = client.responses.calls[-1]["input"][0]["output"]
        end = next(e for e in events if e["type"] == "TOOL_CALL_END" and e["tool_call_id"] == "c1")
        if expected_ok:
            assert output == "조사 결과" and end["ok"] is True
        else:
            assert json.loads(output)["ok"] is False and "ConnectionError" in json.loads(output)["error"]
            assert end["ok"] is False and "실패" in end["summary"]


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


# --- answers and notes (R1, R7) --------------------------------------------------------------------------


def _text(events):
    return "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")


def _notes(events):
    return [e["delta"] for e in events if e["type"] == "THINKING_TEXT_MESSAGE_CONTENT"]


def _streaming(scripted, **kwargs):
    client = FakeClient({})
    client.responses = FakeStreamResponses(scripted)
    return client, AgentRuntime(client, id_factory=lambda: "id", stream=True, **kwargs)


def _broken(*events, error=None):
    """A response stream that sends ``events`` and then breaks."""

    def stream():
        yield from events
        raise error or ConnectionError("stream broke")

    return stream()


def _mcp(call_id, name, arguments="{}", output='{"ok": true}'):
    added = {"type": "mcp_call", "id": call_id, "name": name, "arguments": ""}
    return added, added | {"arguments": arguments, "output": output}


def _done(item):
    return {"type": "response.output_item.done", "item": item}


class _Status(Exception):
    def __init__(self, status_code, request_id=None):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.request_id = request_id


def test_a_long_unlabelled_plan_before_tools_stays_a_note():
    plan = (
        "먼저 4층 매장의 ㎡당 매출을 계산할게요.\n"
        + "그다음 같은 카테고리 매장끼리 비교해 원인을 나눠 볼게요. " * 6
    )
    answer = "4층에서 효율이 가장 낮은 곳은 란제리 매장 다섯 곳이에요."
    added, done = _mcp("m1", "analytics_productivity", '{"rev":"R2","floor":"4F"}')
    scripted = {
        "atlas-orchestrator": [
            [
                _added({"type": "message"}),
                *_deltas(plan),
                _added(added),
                _done(done),
                _added({"type": "message"}),
                *_deltas(answer),
                _completed([done, _message(answer)]),
            ]
        ]
    }
    _, runtime = _streaming(scripted)
    events = list(runtime.run("4층 효율", {"rev": "R2"}))
    types = [e["type"] for e in events]
    assert len(plan) > 200 and _notes(events) == [plan.strip()] and _text(events) == answer
    assert types.index("TEXT_MESSAGE_START") > types.index("TOOL_CALL_END")
    assert events[-1] == {
        "type": "RUN_FINISHED",
        "thread_id": "conv-1",
        "run_id": "id",
        "outcome": "answered",
    }


def test_phase_labels_route_commentary_to_notes_and_stream_the_final_answer():
    plan = "4층 매장의 ㎡당 매출을 계산할게요.\n" + "하위 매장의 원인을 나눠 볼게요. " * 10
    answer = "4층에서 효율이 가장 낮은 곳은 란제리 매장 다섯 곳이에요."
    added, done = _mcp("m1", "analytics_productivity")
    scripted = {
        "atlas-orchestrator": [
            [
                _added({"type": "message", "phase": "commentary"}),
                *_deltas(plan),
                _done({"type": "message", "phase": "commentary"}),
                _added(added),
                _done(done),
                _added({"type": "message", "phase": "final_answer"}),
                *_deltas(answer, size=5),
                _completed([done, _message(answer)]),
            ]
        ]
    }
    _, runtime = _streaming(scripted)
    events = list(runtime.run("4층 효율", {}))
    contents = [e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert _notes(events) == [plan.strip()]
    assert "".join(contents) == answer and len(contents) == len(_deltas(answer, size=5))  # streamed live


def test_a_progress_note_is_never_shown_as_the_answer_when_a_ui_action_fails():
    """Review R1: "I colored the plan" followed by a failed UI action and no answer must not become the answer."""
    heatmap = {
        "type": "function_call",
        "call_id": "f1",
        "name": "ui_show_heatmap",
        "arguments": '{"metric":"sales_per_m2","rev":"R2","floor":"4F"}',
    }
    scripted = {
        "atlas-orchestrator": [
            {"output": [_message("도면에 빨간색으로 칠해 두었어요."), heatmap]},
            {"output": []},
        ]
    }
    events = list(AgentRuntime(FakeClient(scripted), id_factory=lambda: "id").run("4층 색칠", {"rev": "R2"}))
    text = _text(events)
    assert "칠해 두었어요" not in text
    assert text.startswith(INCOMPLETE_ANSWERS["tool_failed"])
    assert "- 실패한 단계: 4층 도면에 '㎡당 매출' 색칠" in text
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["outcome"] == "tool_failed" and events[-1]["incomplete"] is True


def test_no_answer_after_successful_tools_is_reported_as_incomplete():
    _, done = _mcp("m1", "analytics_productivity", '{"rev":"R2"}', '{"rows": 50}')
    scripted = {"atlas-orchestrator": [{"output": [_message("계산해 볼게요."), done]}]}
    events = list(AgentRuntime(FakeClient(scripted), id_factory=lambda: "id").run("4층", {}))
    text = _text(events)
    assert text.startswith(INCOMPLETE_ANSWERS["no_answer"]) and "- 완료한 단계: 공간 생산성 계산" in text
    assert "계산해 볼게요" not in text and _notes(events) == ["계산해 볼게요."]
    assert events[-1]["outcome"] == "no_answer"


def test_rounds_exhausted_end_with_an_incomplete_answer():
    floor = {"type": "function_call", "name": "ui_set_floor", "arguments": '{"floor":"4F"}'}
    scripted = {"atlas-orchestrator": [{"output": [floor | {"call_id": f"f{i}"}]} for i in range(MAX_ROUNDS)]}
    client = FakeClient(scripted)
    events = list(AgentRuntime(client, id_factory=lambda: "id").run("4층", {}))
    text = _text(events)
    assert len(client.responses.calls) == MAX_ROUNDS
    assert text.startswith(INCOMPLETE_ANSWERS["rounds_exhausted"]) and "- 완료한 단계: 4층으로 이동\n" in text
    assert events[-1]["outcome"] == "rounds_exhausted"


def test_text_before_ui_actions_is_a_note_or_the_start_of_the_answer():
    floor = {"type": "function_call", "call_id": "f1", "name": "ui_set_floor", "arguments": '{"floor":"4F"}'}
    short = {
        "atlas-orchestrator": [
            {"output": [_message("도면에 표시할게요."), floor]},
            {"output": [_message("4층 효율이 가장 낮아요.")]},
        ]
    }
    events = list(AgentRuntime(FakeClient(short), id_factory=lambda: "id").run("4층", {}))
    assert _text(events) == "4층 효율이 가장 낮아요." and _notes(events) == ["도면에 표시할게요."]

    answer = "4층에서 효율이 가장 낮은 곳은 란제리 매장이에요.\n- Ravandre Nuit(4F-E05)가 가장 낮아요."
    long = {
        "atlas-orchestrator": [
            {"output": [_message(answer), floor]},
            {"output": [_message("도면에 표시해 두었어요.")]},
        ]
    }
    events = list(AgentRuntime(FakeClient(long), id_factory=lambda: "id").run("4층", {}))
    assert _text(events) == f"{answer}\n\n도면에 표시해 두었어요." and _notes(events) == []

    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 MD 개편 공개 사례를 조사해줘"}',
    }
    mixed = {
        "atlas-orchestrator": [
            {"output": [_message("공개 사례도 찾아볼게요."), floor, consult]},
            {"output": [_message("사례를 정리했어요.")]},
        ],
        "market-researcher": [{"output_text": "조사 결과"}],
    }
    events = list(AgentRuntime(FakeClient(mixed), id_factory=lambda: "id").run("사례", {}))
    assert _notes(events) == ["공개 사례도 찾아볼게요."] and _text(events) == "사례를 정리했어요."


def test_a_broken_specialist_attempt_ends_its_tool_calls_before_the_retry():
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    search = {"type": "web_search_call", "id": "w1", "action": {"query": "백화점 평효율"}}
    retry = search | {"id": "w2", "status": "completed"}
    scripted = {
        "atlas-orchestrator": [
            [_completed([consult])],
            [*_deltas("답이에요."), _completed([_message("답이에요.")])],
        ],
        "market-researcher": [
            _broken(_added(search)),
            [_added(retry), _done(retry), *_deltas("조사 결과"), _completed([_message("조사 결과")])],
        ],
    }
    client, runtime = _streaming(scripted)
    events = list(runtime.run("벤치마크", {}))
    starts = [e["tool_call_id"] for e in events if e["type"] == "TOOL_CALL_START"]
    ends = {e["tool_call_id"]: e for e in events if e["type"] == "TOOL_CALL_END"}
    assert set(starts) == set(ends) == {"c1", "w1", "w2"}  # no step stays "running"
    assert ends["w1"]["ok"] is False and "끊겨" in ends["w1"]["result"]
    retry_start = next(
        i for i, e in enumerate(events) if e["type"] == "TOOL_CALL_START" and e["tool_call_id"] == "w2"
    )
    assert events.index(ends["w1"]) < retry_start
    assert ends["c1"]["ok"] is True and client.responses.calls[-1]["input"][0]["output"] == "조사 결과"


# --- resuming broken responses (R3) ------------------------------------------------------------------------


def test_a_broken_stream_after_a_tool_result_is_resumed_once_without_running_it_again():
    added, done = _mcp("m1", "registry_run", '{"generator":"signage"}', '{"ok": true, "artifact": "A-1"}')
    answer = "사인물 초안을 만들었어요."
    scripted = {
        "atlas-orchestrator": [
            _broken(
                _added({"type": "message"}), *_deltas("사인물 초안을 만들게요."), _added(added), _done(done)
            ),
            [*_deltas(answer), _completed([_message(answer)])],
        ]
    }
    client, runtime = _streaming(scripted)
    events = list(runtime.run("사인물 만들어줘", {"rev": "R2"}))
    calls = client.responses.calls
    assert len(calls) == 2 and _text(events) == answer and events[-1]["outcome"] == "answered"
    resumed = calls[1]["input"]
    assert resumed[0] == {"role": "user", "content": calls[0]["input"]}  # the conversation lacks the question
    note = resumed[-1]["content"]
    assert (
        note.startswith("[연결 재개]")
        and "registry_run" in note
        and "A-1" in note
        and "다시 호출하지 말고" in note
    )
    assert RESUME_NOTE in _notes(events)
    assert [e["ok"] for e in events if e.get("tool_call_id") == "m1" and e["type"] == "TOOL_CALL_END"] == [
        True
    ]


def test_a_resume_sends_function_outputs_again_only_when_the_conversation_lacks_them():
    floor = {"type": "function_call", "call_id": "f1", "name": "ui_set_floor", "arguments": '{"floor":"4F"}'}
    for stored in (False, True):
        scripted = {
            "atlas-orchestrator": [
                [_completed([floor])],
                _broken({"type": "response.created"}),
                [*_deltas("4층으로 옮겼어요."), _completed([_message("4층으로 옮겼어요.")])],
            ]
        }
        handled = []
        handler = UiActionHandler()
        original = handler.handle
        handler.handle = lambda name, args, state, handled=handled, original=original: (
            handled.append(name) or original(name, args, state)
        )
        client = FakeClient({})
        client.responses = FakeStreamResponses(scripted)
        items = [{"type": "function_call_output", "call_id": "f1", "output": "{}"}] if stored else []
        client.conversations.items = SimpleNamespace(
            list=lambda conversation_id, items=items, **kw: SimpleNamespace(data=items)
        )
        runtime = AgentRuntime(client, ui_handler=handler, id_factory=lambda: "id", stream=True)
        events = list(runtime.run("4층", {}))

        assert handled == ["ui_set_floor"]  # function tools never run twice
        resumed = client.responses.calls[2]["input"]
        outputs = [item for item in resumed if item.get("type") == "function_call_output"]
        assert outputs == ([] if stored else client.responses.calls[1]["input"])
        assert "[연결 재개]" in resumed[-1]["content"] and events[-1]["outcome"] == "answered"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (_Status(429), "요청 한도"),
        (_Status(401), "완료하지 못했어요"),
        (_Status(400), "완료하지 못했어요"),
        (ValueError("bug"), "완료하지 못했어요"),
    ],
)
def test_non_transient_failures_are_not_resumed(error, message):
    added, done = _mcp("m1", "analytics_productivity")
    client, runtime = _streaming({"atlas-orchestrator": [_broken(_added(added), _done(done), error=error)]})
    events = []
    with pytest.raises(type(error)):
        for event in runtime.run("4층", {}):
            events.append(event)
    assert len(client.responses.calls) == 1
    assert events[-1]["type"] == "RUN_ERROR" and message in events[-1]["message"]
    assert events[-1]["error_type"] == type(error).__name__


def test_a_resume_is_tried_once_and_only_after_tool_results():
    added, done = _mcp("m1", "analytics_productivity")
    twice = {
        "atlas-orchestrator": [_broken(_added(added), _done(done)), _broken({"type": "response.created"})]
    }
    client, runtime = _streaming(twice)
    events = []
    with pytest.raises(ConnectionError):
        for event in runtime.run("4층", {}):
            events.append(event)
    assert len(client.responses.calls) == 2 and events[-1]["code"] == "transient"

    nothing = {"atlas-orchestrator": [_broken(_added({"type": "message"}), *_deltas("확인할게요"))]}
    client, runtime = _streaming(nothing)
    with pytest.raises(ConnectionError):
        list(runtime.run("4층", {}))
    assert len(client.responses.calls) == 1

    answered = {
        "atlas-orchestrator": [
            _broken(
                _added(added),
                _done(done),
                _added({"type": "message", "phase": "final_answer"}),
                *_deltas("4층은"),
            )
        ]
    }
    client, runtime = _streaming(answered)
    events = []
    with pytest.raises(ConnectionError):
        for event in runtime.run("4층", {}):
            events.append(event)
    assert len(client.responses.calls) == 1 and _text(events) == "4층은"  # answer text already left


def test_failure_kinds():
    assert failure_kind(_Status(429)) == "rate_limit" and failure_kind(_Status(403)) == "auth"
    assert failure_kind(_Status(503)) == "transient" and failure_kind(_Status(408)) == "transient"
    assert failure_kind(_Status(422)) == "rejected" and failure_kind(TimeoutError()) == "transient"
    assert failure_kind(type("RemoteProtocolError", (Exception,), {})()) == "transient"
    assert (
        failure_kind(KeyError("x")) == "other"
        and failure_kind(RuntimeError("rate limit reached")) == "rate_limit"
    )


# --- specialists (R4, R5, R8) ------------------------------------------------------------------------------


def test_specialist_sources_survive_truncation():
    body = "백화점 층별 효율 사례를 정리했어요. " * 400
    text = body + "\n\n출처:\n- 기사 A — https://news.example/a"
    annotations = [
        {
            "type": "url_citation",
            "url": "https://news.example/a",
            "title": "기사 A",
            "start_index": 0,
            "end_index": 1,
        },
        {
            "type": "url_citation",
            "url": "https://report.example/b",
            "title": "보고서 B",
            "start_index": 0,
            "end_index": 1,
        },
        {
            "type": "url_citation",
            "url": "https://report.example/b",
            "title": "보고서 B",
            "start_index": 2,
            "end_index": 3,
        },
    ]
    message = {
        "type": "message",
        "content": [{"type": "output_text", "text": text, "annotations": annotations}],
    }
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    scripted = {
        "atlas-orchestrator": [{"output": [consult]}, {"output": [_message("요약했어요.")]}],
        "market-researcher": [{"output": [message]}],
    }
    client = FakeClient(scripted)
    list(AgentRuntime(client, id_factory=lambda: "id").run("벤치마크", {}))
    output = client.responses.calls[-1]["input"][0]["output"]
    assert len(text) > SPECIALIST_OUTPUT_LIMIT >= len(output)
    assert (
        output.count("https://news.example/a") == 1
        and output.count("보고서 B — https://report.example/b") == 1
    )
    assert "길이 제한으로 본문 뒷부분을 생략했어요" in output
    assert output.index("생략했어요") < output.index("출처:")


def test_with_sources_formats_citations_and_caps_long_lists():
    assert with_sources("요약", []) == "요약"
    assert (
        with_sources("요약", [("기사", "https://a.example")]) == "요약\n\n출처:\n- 기사 — https://a.example"
    )
    assert with_sources("요약", [("", "https://a.example")]) == "요약\n\n출처:\n- https://a.example"
    many = [(f"기사 {i}", f"https://news.example/{i}") for i in range(200)]
    capped = with_sources("요약", many)
    assert capped.startswith("요약\n\n출처:") and "줄 생략" in capped and len(capped) < 1700
    assert with_sources("참고 자료를 보면 매출이 늘었어요.", []) == "참고 자료를 보면 매출이 늘었어요."


def test_a_failed_consultation_reused_in_the_same_run_is_still_failed():
    consult = {
        "type": "function_call",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    scripted = {
        "atlas-orchestrator": [
            {"output": [consult | {"call_id": "a"}]},
            {"output": [consult | {"call_id": "b"}]},
            {"output": [_message("벤치마크는 확인하지 못했어요.")]},
        ],
        "market-researcher": [],
    }
    client = FakeClient(scripted)
    client.responses = FlakyResponses(scripted, failures=1, error=_Status(400))
    events = list(AgentRuntime(client, id_factory=lambda: "id").run("벤치마크와 비교", {}))
    ends = {e["tool_call_id"]: e for e in events if e["type"] == "TOOL_CALL_END"}
    assert ends["a"]["ok"] is False and ends["b"]["ok"] is False and "재사용" in ends["b"]["summary"]
    assert ends["b"]["result"] == "시장 조사 에이전트의 답을 받지 못해 직접 정리해요"
    researcher = [
        c
        for c in client.responses.calls
        if c["extra_body"]["agent_reference"]["name"] != "atlas-orchestrator"
    ]
    assert len(researcher) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [(_Status(429), "요청 한도"), (_Status(401), "_Status"), (ValueError("bug"), "ValueError")],
)
def test_only_transient_specialist_failures_are_retried(error, expected):
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    scripted = {
        "atlas-orchestrator": [{"output": [consult]}, {"output": [_message("답")]}],
        "market-researcher": [{"output_text": "조사 결과"}],
    }
    client = FakeClient(scripted)
    client.responses = FlakyResponses(scripted, failures=2, error=error)
    list(AgentRuntime(client, id_factory=lambda: "id").run("벤치마크", {}))
    researcher = [
        c for c in client.responses.calls if c["extra_body"]["agent_reference"]["name"] == "market-researcher"
    ]
    assert len(researcher) == 1
    assert expected in json.loads(client.responses.calls[-1]["input"][0]["output"])["error"]


# --- versions, budget, cancel and logs (R6, R9, R15) ---------------------------------------------------------


def test_agent_versions_can_be_pinned():
    assert agent_versions_from_env({}) == {}
    pinned = agent_versions_from_env(
        {"FOUNDRY_AGENT_VERSIONS": '{"atlas-orchestrator": 7, "generator": "2"}'}
    )
    assert pinned == {"atlas-orchestrator": "7", "generator": "2"}
    for bad in ("[1]", "{", '{"atlas-orchestrator": true}', '{"generator": " "}'):
        with pytest.raises(ValueError):
            agent_versions_from_env({"FOUNDRY_AGENT_VERSIONS": bad})
    assert agent_reference("generator") == {
        "agent_reference": {"name": "generator", "type": "agent_reference"}
    }
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    scripted = {
        "atlas-orchestrator": [{"output": [consult]}, {"output": [_message("답")]}],
        "market-researcher": [{"output_text": "조사 결과"}],
    }
    client = FakeClient(scripted)
    events = list(AgentRuntime(client, id_factory=lambda: "id", agent_versions=pinned).run("질문", {}))
    assert events[0]["agent"] == "atlas-orchestrator" and events[0]["agent_version"] == "7"
    references = [call["extra_body"]["agent_reference"] for call in client.responses.calls]
    assert references[0] == {"name": "atlas-orchestrator", "type": "agent_reference", "version": "7"}
    assert references[1] == {"name": "market-researcher", "type": "agent_reference"}


def test_deploy_records_versions_models_and_instruction_hashes(tmp_path, monkeypatch):
    for name in ("CHAT_MODEL_DEPLOYMENT", "MINI_MODEL_DEPLOYMENT", "MCP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    dry = deploy_module.deploy(dry_run=True)
    definitions = {d["name"]: d for d in load_definitions("gpt-6.1-sol", "gpt-5.4-mini")}
    for name, info in dry.items():
        expected = hashlib.sha256(definitions[name]["instructions"].encode("utf-8")).hexdigest()
        assert info["instructions_sha256"] == expected and "version" not in info

    created = []

    class FakeAgents:
        def create_version(self, agent_name, definition, **kwargs):
            created.append((agent_name, kwargs))
            return SimpleNamespace(version=str(len(created)))

    class FakeProject:
        def __init__(self, endpoint, credential):
            self.agents = FakeAgents()

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", FakeProject)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: object())
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("MCP_CONNECTION_ID", "conn-id")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example")
    record = tmp_path / "local" / "deployed_agents.json"
    summary = deploy_module.deploy(record_path=record)
    saved = json.loads(record.read_text(encoding="utf-8"))
    assert saved["agents"] == summary and summary["atlas-orchestrator"]["version"] == "1"
    for name, kwargs in created:
        assert kwargs["metadata"] == {"instructions_sha256": summary[name]["instructions_sha256"]}
    pins = agent_versions_from_env({"FOUNDRY_AGENT_VERSIONS": saved["env"]["FOUNDRY_AGENT_VERSIONS"]})
    assert pins == {name: info["version"] for name, info in summary.items()}
    assert "https://mcp.example" not in record.read_text(encoding="utf-8")
    assert "data/local/*" in (REPO / ".gitignore").read_text(encoding="utf-8")
    assert deploy_module.DEPLOY_RECORD.parent == REPO / "data" / "local"


def test_turn_budget_stops_new_consultations_and_rounds():
    consult = {
        "type": "function_call",
        "call_id": "c1",
        "name": "consult_specialist",
        "arguments": '{"agent":"researcher","request":"백화점 층별 효율 공개 벤치마크를 조사해줘"}',
    }
    scripted = {
        "atlas-orchestrator": [{"output": [consult]}, {"output": [_message("답")]}],
        "market-researcher": [{"output_text": "조사 결과"}],
    }
    client = FakeClient(scripted)
    events = list(AgentRuntime(client, id_factory=lambda: "id", turn_budget_s=-1).run("벤치마크", {}))
    assert len(client.responses.calls) == 1  # no specialist call and no second round
    end = next(e for e in events if e["type"] == "TOOL_CALL_END")
    assert end["ok"] is False and "시간 제한" in end["summary"]
    assert events[-1]["outcome"] == "time_exhausted" and _text(events).startswith(
        INCOMPLETE_ANSWERS["time_exhausted"]
    )
    assert turn_budget_from_env({}) == 200.0 and turn_budget_from_env({"AGENT_TURN_BUDGET_S": "0"}) is None
    assert turn_budget_from_env({"AGENT_TURN_BUDGET_S": "90"}) == 90.0
    assert turn_budget_from_env({"AGENT_TURN_BUDGET_S": "soon"}) == 200.0


class _CancellingStream:
    def __init__(self, cancel):
        self.cancel = cancel
        self.closed = False

    def __iter__(self):
        yield _added({"type": "message"})
        self.cancel.set()
        yield {"type": "response.output_text.delta", "delta": "계산할게요"}
        yield _completed([_message("계산할게요")])

    def close(self):
        self.closed = True


def test_cancel_stops_the_run_at_the_next_event_and_closes_the_stream():
    cancel = threading.Event()
    stream = _CancellingStream(cancel)
    client = FakeClient({})
    client.responses = SimpleNamespace(
        calls=[], create=lambda **kwargs: client.responses.calls.append(kwargs) or stream
    )
    events = list(AgentRuntime(client, id_factory=lambda: "id", stream=True).run("4층", {}, cancel=cancel))
    assert stream.closed and [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1] == {"type": "RUN_ERROR", "message": "요청이 취소됐어요.", "code": "cancelled"}

    already = threading.Event()
    already.set()
    idle = FakeClient({"atlas-orchestrator": []})
    events = list(AgentRuntime(idle, id_factory=lambda: "id").run("4층", {}, cancel=already))
    assert events == [{"type": "RUN_ERROR", "message": "요청이 취소됐어요.", "code": "cancelled"}]
    assert idle.responses.calls == [] and idle._count == 0


def test_runs_log_one_structured_line(caplog):
    caplog.set_level(logging.INFO, logger="cad2anything.agents")
    _, done = _mcp("m1", "analytics_productivity", '{"rev":"R2"}', '{"rows": 50}')
    scripted = {"atlas-orchestrator": [{"output": [done, _message("4층이에요.")]}]}
    list(AgentRuntime(FakeClient(scripted), id_factory=lambda: "id").run("비밀 질문", {}))
    lines = [r.getMessage() for r in caplog.records if r.name == "cad2anything.agents"]
    assert len(lines) == 1 and lines[0].startswith("agent_run ") and "비밀 질문" not in lines[0]
    record = json.loads(lines[0].split(" ", 1)[1])
    assert record["thread_id"] == "conv-1" and record["outcome"] == "answered" and record["rounds"] == 1
    assert record["tool_calls"] == 1 and record["failed_tool_calls"] == 0 and record["duration_ms"] >= 0

    caplog.clear()
    _, runtime = _streaming({"atlas-orchestrator": [_broken(error=_Status(503, request_id="req-1"))]})
    with pytest.raises(_Status):
        list(runtime.run("질문", {}))
    error_lines = [r for r in caplog.records if r.name == "cad2anything.agents"]
    assert len(error_lines) == 1 and error_lines[0].levelno == logging.ERROR
    record = json.loads(error_lines[0].getMessage().split(" ", 1)[1])
    assert record["outcome"] == "error" and record["error_type"] == "_Status" and record["status_code"] == 503
    assert record["request_id"] == "req-1" and record["failure"] == "transient"


# --- UI state (R2, R11, R12, R13) ---------------------------------------------------------------------------


def test_overlays_drawn_earlier_in_the_same_answer_stay():
    flow = {"mode": "popup", "rev": "R2", "query": {"floor": "3F"}, "floors": {}, "markers": []}
    for order in (("ui_highlight_units", "ui_show_flow"), ("ui_show_flow", "ui_highlight_units")):
        handler = UiActionHandler(flow_provider=lambda **params: flow)
        state = {"rev": "R2", "floor": "3F", "overlays": ["heatmap", "route"]}
        patches = []
        for name in order:
            args = {"unit_codes": ["3F-C05"]} if name == "ui_highlight_units" else {"mode": "popup"}
            patches += handler.handle(name, args, state).patches
        paths = [p["path"] for p in patches]
        assert paths.count("/highlights") == 1 and paths.count("/flow") == 1, (
            order
        )  # drawn once, never cleared
        assert next(p for p in patches if p["path"] == "/highlights")["value"][0]["unit_code"] == "3F-C05"
        assert {"/heatmap", "/route"} <= set(paths)  # the earlier answer's overlays are replaced
        assert "/journey" not in paths and "/edges" not in paths  # not on screen: nothing to clear


def test_overlays_of_an_earlier_answer_are_cleared_from_the_reported_screen():
    handler = UiActionHandler()
    reported = handler.handle(
        "ui_highlight_units", {"unit_codes": ["4F-E01"]}, {"rev": "R2", "overlays": ["flow"]}
    )
    assert [p["path"] for p in reported.patches] == ["/highlights", "/flow"]
    full_state = {"rev": "R2", "journey": {"stops": []}, "route": None, "flow": None}
    edges = handler.handle("ui_draw_edges", {"edges": [{"from": "4F-E01", "to": "3F-C05"}]}, full_state)
    assert [p["path"] for p in edges.patches] == ["/edges", "/journey"]
    nothing = handler.handle("ui_highlight_units", {"unit_codes": ["4F-E01"]}, {"rev": "R2", "overlays": []})
    assert [p["path"] for p in nothing.patches] == ["/highlights"]
    unknown = handler.handle("ui_highlight_units", {"unit_codes": ["4F-E01"]}, {"rev": "R2"})
    assert [p["path"] for p in unknown.patches] == ["/highlights", "/journey", "/route", "/flow"]


def test_heatmap_switches_the_revision_with_its_colors_and_flags_estimates():
    calls = []
    handler = UiActionHandler(
        heatmap_provider=lambda m, r, f: calls.append((m, r, f)) or {"4F-E01": 1.0, "4F-E02": 3.0},
        has_sales=lambda rev: not rev.startswith("FW2026"),
    )
    state = {"rev": "R2", "floor": "3F", "overlays": ["highlights", "flow", "compare"]}
    result = handler.handle(
        "ui_show_heatmap", {"metric": "sales_per_m2", "rev": "FW2026-A1", "floor": "4F"}, state
    )
    assert calls == [("sales_per_m2", "FW2026-A1", "4F")]
    assert [p["path"] for p in result.patches] == [
        "/rev",
        "/highlights",
        "/compareRev",
        "/flow",
        "/floor",
        "/heatmap",
    ]
    heatmap = result.patches[-1]["value"]
    assert heatmap["rev"] == "FW2026-A1" and heatmap["estimated"] is True
    assert heatmap["label"] == "연환산 ㎡당 매출(원) (모델 추정치)"
    output = json.loads(result.output)
    assert output["rev"] == "FW2026-A1" and output["rev_switched"] is True and output["estimated"] is True
    assert "실적이라고 말하지 마세요" in output["note"] and "모델 추정치" in output["shown"]
    assert "FW2026-A1 개정으로 바꿨습니다" in output["shown"]
    assert state["rev"] == "FW2026-A1" and state["floor"] == "4F"

    state = {"rev": "R2", "floor": "4F", "overlays": []}
    handler.handle("ui_highlight_units", {"unit_codes": ["4F-E01"]}, state)
    switched = handler.handle(
        "ui_show_heatmap", {"metric": "sales_per_m2", "rev": "R0", "floor": "4F"}, state
    )
    assert [p["path"] for p in switched.patches] == ["/rev", "/highlights", "/heatmap"]  # R2 highlights go
    assert switched.patches[-1]["value"]["estimated"] is False
    assert "estimated" not in json.loads(switched.output)
    same = handler.handle(
        "ui_show_heatmap", {"metric": "walk_to_escalator_m", "rev": "R0", "floor": "4F"}, state
    )
    assert [p["path"] for p in same.patches] == ["/heatmap"]
    unknown = UiActionHandler(heatmap_provider=lambda m, r, f: {"4F-E01": 1.0})
    flagless = unknown.handle("ui_show_heatmap", {"metric": "sales_per_m2", "rev": "H1", "floor": "4F"}, {})
    assert flagless.patches[0] == {"op": "replace", "path": "/rev", "value": "H1"}
    flagless_heatmap = next(p["value"] for p in flagless.patches if p["path"] == "/heatmap")
    assert flagless_heatmap["estimated"] is False  # no sales information: not flagged


def test_revision_switch_clears_reported_overlays_when_current_revision_is_unknown():
    handler = UiActionHandler(
        heatmap_provider=lambda *_: {"4F-E01": 1.0},
        has_sales=lambda _rev: True,
    )
    state = {
        "overlays": ["highlights", "flow"],
        "highlights": [{"unit_code": "4F-E02"}],
        "flow": {"mode": "traffic"},
    }

    result = handler.handle("ui_show_heatmap", {"metric": "sales_per_m2", "rev": "R1"}, state)

    assert [patch["path"] for patch in result.patches] == [
        "/rev",
        "/highlights",
        "/flow",
        "/heatmap",
    ]
    assert result.patches[1]["value"] == []
    assert result.patches[2]["value"] is None


def test_route_and_journey_wording_follow_what_was_drawn():
    base = {
        "origin": {"label": "정문", "floor": "1F"},
        "destination": {"label": "8F-C05", "floor": "8F"},
        "walk_m": 85,
        "transitions": [],
    }
    line = [{"floor": "1F", "points": [[0.0, 0.0], [1.0, 1.0]]}]
    cases = [
        (base | {"segments": line}, "1층 도면에 경로를 파란 선으로 그렸습니다"),
        (base | {"segments": line, "straight": True, "walk_m": 40}, "직선(추정)으로 표시했습니다"),
        (base | {"segments": [], "straight": True, "walk_m": None}, "경로 선을 그리지 않았습니다"),
    ]
    for route, expected in cases:
        handler = UiActionHandler(route_provider=lambda rev=None, route=route, **params: route)
        output = json.loads(handler.handle("ui_show_route", {"origin": "정문"}, {"rev": "R2"}).output)
        assert expected in output["shown"]
        if route.get("straight"):
            assert output["straight"] is True and "파란 선" not in output["shown"]
        if route["walk_m"] is None:
            assert "걷는 거리는 알 수 없습니다" in output["shown"]
        elif route.get("straight"):
            assert "직선으로 잰 추정치" in output["shown"]

    journey = {
        "rev": "R2",
        "stops": [{"order": 1, "floor": "1F"}, {"order": 2, "floor": "3F"}],
        "segments": [],
    }
    handler = UiActionHandler(journey_provider=lambda **params: journey)
    shown = json.loads(handler.handle("ui_show_journey", {}, {"rev": "R2"}).output)["shown"]
    assert "추정 경로를 그리고" not in shown and "그리지 못했습니다" in shown
    drawn = journey | {"segments": [{"floor": "1F", "points": [[0, 0], [1, 1]], "straight": True}]}
    handler = UiActionHandler(journey_provider=lambda **params: drawn)
    shown = json.loads(handler.handle("ui_show_journey", {}, {"rev": "R2"}).output)["shown"]
    assert "추정 경로를 그리고" in shown and "직선(추정)" in shown


def test_ui_actions_reject_malformed_calls_instead_of_failing_the_run():
    handler = UiActionHandler()
    assert json.loads(handler.handle("ui_highlight_units", {}, {}).output)["ok"] is False
    assert json.loads(handler.handle("ui_clear", {"what": "everything"}, {}).output)["ok"] is False
    assert json.loads(handler.handle("estimated", {}, {}).output)["ok"] is False  # only ui_* functions


def test_the_prompt_gets_a_compact_screen_state():
    full = {
        "rev": "R2",
        "floor": "3F",
        "selection": [],
        "compareRev": None,
        "heatmap": {"metric": "sales_per_m2", "values": {"3F-C01": 0.5}},
        "highlights": [],
        "flow": None,
        "overlays": ["route"],
        "layers": {"labels": True},
    }
    assert screen_state(full) == {"rev": "R2", "floor": "3F", "overlays": ["heatmap", "route"]}
    scripted = {"atlas-orchestrator": [{"output": [_message("3층이에요.")]}]}
    client = FakeClient(scripted)
    list(AgentRuntime(client, id_factory=lambda: "id").run("여기는?", full))
    sent = client.responses.calls[0]["input"]
    assert sent == '[ui_state] {"rev": "R2", "floor": "3F", "overlays": ["heatmap", "route"]}\n\n여기는?'
