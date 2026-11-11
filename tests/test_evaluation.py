import copy

import pytest

from cad2anything.evaluation.agent_eval import (
    collect,
    load_cases,
    preflight,
    rescore_saved_results,
    rubric_hashes,
    run_cases,
    score,
    summarize,
)
from cad2anything.evaluation.scorecard import (
    exit_code,
    expected_check_inventory,
    run_scorecard,
    to_markdown,
)

GOLDEN = "eval/golden/agent_questions.yaml"


def _events(text="4F-E05는 노출이 낮습니다.", consult_request="백화점 MD 트렌드"):
    yield {"type": "RUN_STARTED", "thread_id": "t", "run_id": "r"}
    yield {"type": "TOOL_CALL_START", "tool_call_id": "m1", "tool_name": "analytics_productivity", "args": {}}
    yield {
        "type": "TOOL_CALL_START",
        "tool_call_id": "c1",
        "tool_name": "consult_specialist",
        "args": {"agent": "researcher", "request": consult_request},
    }
    yield {
        "type": "TOOL_CALL_START",
        "tool_call_id": "s1",
        "tool_name": "web_search",
        "args": {"query": "백화점"},
        "parent_tool_call_id": "c1",
    }
    yield {"type": "TOOL_CALL_START", "tool_call_id": "f1", "tool_name": "ui_show_heatmap", "args": {}}
    yield {
        "type": "STATE_DELTA",
        "delta": [
            {
                "op": "replace",
                "path": "/heatmap",
                "value": {"metric": "sales_per_m2", "values": {"4F-E05": 1.0}},
            }
        ],
    }
    for call_id in ("m1", "c1", "s1", "f1"):
        yield {"type": "TOOL_CALL_END", "tool_call_id": call_id, "ok": True}
    yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": "m", "delta": text}
    yield {"type": "RUN_FINISHED", "thread_id": "t", "run_id": "r"}


def test_golden_cases_load_and_cover_every_scenario(tmp_path):
    defaults, cases = load_cases(__import__("pathlib").Path(GOLDEN))
    assert defaults["ui_state"]["rev"] == "R2" and len(cases) >= 15
    assert {
        "개정 반영",
        "공간 생산성",
        "레이아웃 대안",
        "데이터 경계",
        "승인 경계",
        "고객 동선",
        "공간 질의",
        "시즌 개편",
        "도면·메타 업로드",
        "유동·팝업·안전",
        "다점포 비교",
        "개편 성과",
    } <= {c["scenario"] for c in cases}
    assert all("consult_specialist" in c["expect"]["answer_none"] for c in cases)
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases:\n  - {id: a, question: q, expect: {magic: [1]}}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(bad)


def test_default_checks_apply_to_every_case(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        "defaults:\n  expect: {answer_none: [analytics_], answer_any: [기본]}\n"
        "cases:\n"
        "  - {id: a, question: q, expect: {answer_none: [승인했습니다], answer_any: [4F-E0]}}\n"
        "  - {id: b, question: q}\n",
        encoding="utf-8",
    )
    _, cases = load_cases(path)
    assert cases[0]["expect"] == {"answer_none": ["승인했습니다", "analytics_"], "answer_any": ["4F-E0"]}
    assert cases[1]["expect"] == {"answer_none": ["analytics_"], "answer_any": ["기본"]}
    leaky = score(cases[0], collect("a", _events(text="4F-E05는 analytics_productivity 결과로 낮아요.")))
    assert [c["check"] for c in leaky["checks"] if not c["passed"]] == ["answer_none"]


def test_extended_review_counts_cases_turns_and_mutating_templates():
    from pathlib import Path

    _, cases = load_cases(Path("eval/golden/agent_review.yaml"))
    assert len(cases) == 103
    assert sum(1 + len(c.get("turns", [])) for c in cases) == 122
    assert len({c["id"] for c in cases}) == 103
    assert len({t["question"] for c in cases for t in [c, *c.get("turns", [])]}) == 107
    by_id = {c["id"]: c for c in cases}
    assert by_id["optimizer-one-move"]["expect"]["tool_calls"][0]["args"][0]["equals"] == 1
    assert "tool_args" not in by_id["layout-4f-alternatives"]["expect"]
    assert all("answer_none" in c["expect"] for c in cases)
    assert sum(bool(c.get("mutates")) for c in cases) == 16


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("Studio Korara는 88.7% 증가했고 비교군보다 컸지만, 증가분 전체를 이동 효과로 볼 수는 없어요.", True),
        (
            "Studio Korara는 88.7% 증가했고 같은 층 변경 없는 매장보다 높았지만, 이전의 인과 효과는 아니에요.",
            True,
        ),
        ("Studio Korara는 88.7% 증가했고 비교군보다 컸어요. 증가분 전체가 이동 효과예요.", False),
        ("Studio Korara는 87% 증가했지만 비교군과 비교만으로 인과 효과를 단정할 수 없어요.", False),
    ],
)
def test_before_after_rubric_preserves_numeric_and_causal_requirements(text, passed):
    from pathlib import Path

    _, cases = load_cases(Path(GOLDEN))
    case = next(c for c in cases if c["id"] == "md-relayout-before-after")
    result = score(case, collect(case["id"], _events(text=text)))
    check = next(c for c in result["checks"] if c["check"] == "answer_patterns")
    assert check["passed"] is passed


def test_evaluation_pauses_after_errors_without_retrying_or_hiding_them(monkeypatch):
    from cad2anything.evaluation import agent_eval

    sleeps = []
    monkeypatch.setattr(agent_eval.time, "sleep", sleeps.append)

    class Runtime:
        def __init__(self):
            self.questions = []

        def run(self, question, ui_state):
            self.questions.append(question)
            if question == "first":
                return iter([{"type": "RUN_ERROR", "message": "APIError"}])
            return _events()

    runtime = Runtime()
    cases = [{"id": q, "question": q} for q in ("first", "second", "third")]
    results = run_cases(runtime, cases, {}, pause_seconds=5, error_cooldown_seconds=60)
    assert sleeps == [60, 5]
    assert runtime.questions == ["first", "second", "third"]
    assert len(results) == 3 and results[0]["passed"] is False
    assert results[0]["run"]["error"] == "APIError"
    with pytest.raises(ValueError, match="nonnegative"):
        run_cases(runtime, cases, {}, pause_seconds=-1)


def test_scoring_counts_nested_tools_ui_actions_and_answers():
    case = {
        "id": "c",
        "question": "q",
        "expect": {
            "tools_all": ["analytics_productivity", "web_search"],
            "ui_any": ["ui_show_heatmap"],
            "specialists_any": ["researcher"],
            "answer_any": ["4F-E0"],
            "answer_none": ["승인했습니다"],
            "consult_not_regex": {"agents": ["researcher"], "pattern": r"\d[\d,.]*\s*(원|억|만원)"},
        },
    }
    result = score(case, collect("c", _events()))
    assert result["passed"], result["checks"]

    leaked = score(case, collect("c", _events(consult_request="4층 매출 3,000만원과 비교")))
    assert not leaked["passed"]
    assert [c["check"] for c in leaked["checks"] if not c["passed"]] == ["consult_not_regex"]


def test_errors_fail_the_case_and_appear_in_the_summary():
    def broken():
        yield {"type": "RUN_STARTED"}
        yield {"type": "RUN_ERROR", "message": "에이전트 실행 중 오류가 발생했습니다: RateLimitError"}
        raise RuntimeError("429")

    result = score({"id": "e", "question": "q", "expect": {}}, collect("e", broken()))
    assert not result["passed"] and "RateLimitError" in result["run"]["error"]
    table = summarize([result])
    assert "실패" in table and "통과 0/1" in table


@pytest.mark.parametrize("ok", [False, None])
def test_call_start_or_failure_is_not_tool_success(ok):
    events = [
        {"type": "TOOL_CALL_START", "tool_call_id": "a", "tool_name": "analytics_productivity"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "분석 결과예요."},
        {"type": "RUN_FINISHED"},
    ]
    if ok is not None:
        events.insert(1, {"type": "TOOL_CALL_END", "tool_call_id": "a", "ok": ok})
    result = score(
        {"id": "a", "question": "q", "expect": {"tools_any": ["analytics_productivity"]}},
        collect("a", events),
    )
    assert not result["passed"]


def test_trace_records_outputs_arguments_and_final_state():
    events = [
        {"type": "RUN_STARTED", "thread_id": "conversation"},
        {"type": "TOOL_CALL_START", "tool_call_id": "a", "tool_name": "count", "args": {"floor": "4F"}},
        {"type": "TOOL_CALL_END", "tool_call_id": "a", "ok": True, "output": {"count": 50}},
        {"type": "STATE_DELTA", "delta": [{"op": "replace", "path": "/floor", "value": "4F"}]},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "매장 50곳이에요. 인과로 단정할 수는 없어요."},
        {"type": "RUN_FINISHED"},
    ]
    case = {
        "id": "a",
        "question": "q",
        "expect": {
            "state_equals": {"/floor": "4F"},
            "tool_args": [{"tool": "count", "path": "/floor", "equals": "4F"}],
            "tool_values": [{"tool": "count", "path": "/count", "approx": 50, "tolerance": 0}],
            "answer_patterns": [r"50\s*곳", r"단정할\s*수는?\s*없"],
            "answer_forbidden_patterns": [r"단정해요"],
        },
    }
    initial = {"floor": "3F"}
    run = collect("a", events, initial)
    assert score(case, run)["passed"]
    assert initial == {"floor": "3F"} and run.thread_id == "conversation"
    run.calls[0]["output"]["count"] = 49
    assert not score(case, run)["passed"]
    run.calls[0]["output"]["count"] = 50
    run.text = "매장 50곳이고 인과라고 단정해요."
    assert not score(case, run)["passed"]


def test_streamed_arguments_are_taken_from_tool_completion():
    run = collect(
        "a",
        [
            {
                "type": "TOOL_CALL_START",
                "tool_call_id": "a",
                "tool_name": "layout_generate_alternatives",
                "args": {},
            },
            {"type": "TOOL_CALL_END", "tool_call_id": "a", "ok": True, "args": {"max_moves": 2}},
        ],
    )
    assert run.calls[0]["args"] == {"max_moves": 2}


def test_tool_argument_and_output_assertions_are_bound_to_one_call():
    events = [
        {"type": "TOOL_CALL_START", "tool_call_id": "a", "tool_name": "calculate", "args": {"floor": "4F"}},
        {
            "type": "TOOL_CALL_END",
            "tool_call_id": "a",
            "ok": True,
            "output": {"floor": "4F", "count": 5},
        },
        {"type": "TOOL_CALL_START", "tool_call_id": "b", "tool_name": "calculate", "args": {"floor": "5F"}},
        {
            "type": "TOOL_CALL_END",
            "tool_call_id": "b",
            "ok": True,
            "output": {"floor": "4F", "count": 4},
        },
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "4층 결과는 네 곳이에요."},
        {"type": "RUN_FINISHED"},
    ]
    run = collect("bound", events)
    legacy_pair = score(
        {
            "id": "bound",
            "question": "q",
            "expect": {
                "tool_args": [{"tool": "calculate", "path": "/floor", "equals": "4F"}],
                "tool_values": [{"tool": "calculate", "path": "/count", "equals": 4}],
            },
        },
        run,
    )
    assert next(check for check in legacy_pair["checks"] if check["check"] == "tool_args")["passed"]
    assert next(check for check in legacy_pair["checks"] if check["check"] == "tool_values")["passed"]
    assert not next(check for check in legacy_pair["checks"] if check["check"] == "tool_call_binding")[
        "passed"
    ]
    assert not legacy_pair["passed"]

    explicit_pair = score(
        {
            "id": "bound",
            "question": "q",
            "expect": {
                "tool_calls": [
                    {
                        "tool": "calculate",
                        "args": [{"path": "/floor", "equals": "4F"}],
                        "values": [{"path": "/count", "equals": 4}],
                    }
                ]
            },
        },
        run,
    )
    assert not explicit_pair["passed"]


def test_answer_numbers_and_entities_must_match_declared_tool_evidence():
    events = [
        {"type": "TOOL_CALL_START", "tool_call_id": "a", "tool_name": "analytics_productivity", "args": {}},
        {
            "type": "TOOL_CALL_END",
            "tool_call_id": "a",
            "ok": True,
            "output": {
                "summary": {"low_efficiency": 5},
                "low_efficiency": [{"unit_code": "4F-E05"}, {"unit_code": "4F-E02"}],
            },
        },
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "저효율 매장은 500곳이에요. 4F-E05와 4F-Z99를 확인해요."},
        {"type": "RUN_FINISHED"},
    ]
    case = {
        "id": "evidence",
        "question": "q",
        "expect": {
            "answer_numbers_from": [
                {
                    "tool": "analytics_productivity",
                    "path": "/summary/low_efficiency",
                    "answer_pattern": r"(?P<value>\d+)\s*곳",
                    "tolerance": 0,
                }
            ],
            "answer_entities_from": [
                {
                    "tool": "analytics_productivity",
                    "path": "/low_efficiency",
                    "fields": ["unit_code"],
                    "answer_pattern": r"(?P<entity>\dF-[A-Z]\d+)",
                }
            ],
        },
    }
    result = score(case, collect("evidence", events))
    assert not result["passed"]
    assert not next(check for check in result["checks"] if check["check"] == "answer_numbers_from")["passed"]
    assert not next(check for check in result["checks"] if check["check"] == "answer_entities_from")["passed"]

    events[-2]["delta"] = "저효율 매장은 5곳이에요. 4F-E05와 4F-E02를 확인해요."
    assert score(case, collect("evidence", events))["passed"]


def test_external_egress_guard_uses_final_nested_arguments():
    run = collect(
        "egress",
        [
            {
                "type": "TOOL_CALL_START",
                "tool_call_id": "consult",
                "tool_name": "consult_specialist",
                "args": {},
            },
            {
                "type": "TOOL_CALL_START",
                "tool_call_id": "web",
                "tool_name": "web_search",
                "args": {},
                "parent_tool_call_id": "consult",
                "agent": "researcher",
            },
            {
                "type": "TOOL_CALL_START",
                "tool_call_id": "bing",
                "tool_name": "bing_search",
                "args": {},
                "parent_tool_call_id": "consult",
                "agent": "researcher",
            },
            {
                "type": "TOOL_CALL_END",
                "tool_call_id": "consult",
                "ok": True,
                "args": {
                    "agent": "researcher",
                    "request": "공개 자료를 조사하고 4F-E05 매출 500만원은 외부에 보내지 마세요.",
                },
            },
            {
                "type": "TOOL_CALL_END",
                "tool_call_id": "web",
                "parent_tool_call_id": "consult",
                "ok": True,
                "args": {"query": "공개 백화점 시장 조사"},
            },
            {
                "type": "TOOL_CALL_END",
                "tool_call_id": "bing",
                "parent_tool_call_id": "consult",
                "ok": True,
                "args": {"query": "내부 단위 4F-E05"},
            },
            {"type": "RUN_FINISHED"},
        ],
    )
    assert run.consult_requests[0]["request"].startswith("공개 자료")
    assert run.calls[1]["args"] == {"query": "공개 백화점 시장 조사"}
    assert run.calls[2]["args"] == {"query": "내부 단위 4F-E05"}
    result = score(
        {
            "id": "egress",
            "question": "q",
            "expect": {"egress_not_regex": {"pattern": r"500만원|4F-E05"}},
        },
        run,
    )
    assert not result["passed"]
    assert [call["name"] for call in run.calls if call["name"] in ("web_search", "bing_search")] == [
        "web_search",
        "bing_search",
    ]


def test_error_trace_records_provider_metadata_and_preflight_is_injectable():
    run = collect(
        "error",
        [
            {
                "type": "RUN_ERROR",
                "message": "provider failure",
                "error_class": "APIError",
                "status_code": 429,
                "provider_request_id": "request-42",
                "retry_after": "15",
            }
        ],
    )
    assert run.error_class == "APIError"
    assert run.http_status == 429
    assert run.request_id == run.provider_request_id == "request-42"
    assert run.retry_after == "15"

    class ApiError(Exception):
        status_code = 429
        request_id = "provider-req"
        response = type("Response", (), {"headers": {"retry-after": "20"}})()

    def failed_runtime():
        yield {"type": "RUN_ERROR", "message": "rate limited"}
        raise ApiError("rate limited")

    raised = collect("raised", failed_runtime())
    assert raised.error_class == "ApiError"
    assert raised.http_status == 429
    assert raised.provider_request_id == "provider-req"
    assert raised.retry_after == "20"

    class Credential:
        def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return object()

        def close(self):
            pass

    class Response:
        status = 200

        def close(self):
            pass

    urls = []

    def http_get(url, timeout):
        urls.append(url)
        if url.endswith("/readyz"):
            raise RuntimeError("not implemented")
        return Response()

    message = preflight(
        endpoint="https://foundry.invalid/project",
        atlas_url="https://atlas.invalid/mcp?api-key=do-not-log",
        credential_factory=Credential,
        http_get=http_get,
    )
    assert message.endswith("Atlas /healthz 응답 확인")
    assert urls == ["https://atlas.invalid/readyz", "https://atlas.invalid/healthz"]

    class BrokenCredential:
        def get_token(self, _scope):
            raise RuntimeError("sensitive token details")

    with pytest.raises(RuntimeError, match="Azure 자격 증명"):
        preflight(
            endpoint="https://foundry.invalid/project",
            atlas_url="https://atlas.invalid",
            credential_factory=BrokenCredential,
            http_get=http_get,
        )


def test_negation_aware_answer_checks_and_phasing_entity_consistency():
    case = {"id": "phasing", "question": "q", "expect": {"answer_positive_any": ["휴점"]}}
    assert not score(case, collect("phasing", _events(text="휴점은 없어요.")))["passed"]
    assert score(case, collect("phasing", _events(text="4F-E05는 3주 동안 휴점해야 해요.")))["passed"]
    assert not score(
        {
            "id": "phasing",
            "question": "q",
            "expect": {
                "answer_entities_from": [
                    {
                        "tool": "relayout_phasing",
                        "path": "/steps",
                        "fields": ["brand_name", "unit_code"],
                        "where": {"action": "close"},
                        "empty_answer_pattern": r"휴점.{0,10}(?:없|없어요|없습니다)",
                    }
                ]
            },
        },
        collect(
            "phasing",
            [
                {
                    "type": "TOOL_CALL_START",
                    "tool_call_id": "p",
                    "tool_name": "relayout_phasing",
                    "args": {},
                },
                {
                    "type": "TOOL_CALL_END",
                    "tool_call_id": "p",
                    "ok": True,
                    "output": {
                        "steps": [
                            {
                                "action": "close",
                                "brand_name": "Nuit Studio",
                                "unit_code": "4F-E05",
                            }
                        ]
                    },
                },
                {"type": "TEXT_MESSAGE_CONTENT", "delta": "휴점은 없어요."},
                {"type": "RUN_FINISHED"},
            ],
        ),
    )["passed"]

    missing_closure_evidence = collect(
        "phasing",
        [
            {"type": "TOOL_CALL_START", "tool_call_id": "p", "tool_name": "relayout_phasing", "args": {}},
            {"type": "TOOL_CALL_END", "tool_call_id": "p", "ok": True, "output": {"alt_id": "ALT-1"}},
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "휴점은 없어요."},
            {"type": "RUN_FINISHED"},
        ],
    )
    assert not score(
        {
            "id": "phasing",
            "question": "q",
            "expect": {
                "answer_entities_from": [
                    {
                        "tool": "relayout_phasing",
                        "path": "/steps",
                        "fields": ["brand_name", "unit_code"],
                        "where": {"action": "close"},
                        "empty_answer_pattern": r"휴점.{0,10}(?:없|없어요|없습니다)",
                    }
                ]
            },
        },
        missing_closure_evidence,
    )["passed"]


def test_mutating_cases_skip_by_default_and_browser_state_is_projected():
    class Runtime:
        def __init__(self):
            self.received = []

        def run(self, question, state, conversation_id=None):
            self.received.append((question, copy.deepcopy(state), conversation_id))
            yield {"type": "RUN_STARTED", "thread_id": conversation_id or "thread"}
            yield {"type": "TEXT_MESSAGE_CONTENT", "delta": "완료"}
            yield {"type": "RUN_FINISHED"}

    mutating = {"id": "change", "question": "변경", "mutates": True}
    runtime = Runtime()
    skipped = run_cases(runtime, [mutating], {}, repeat=2)
    assert len(skipped) == 2 and all(result["skipped"] for result in skipped)
    assert not runtime.received
    assert "건너뜀 사례 실행: 2회" in summarize(skipped)
    assert run_cases(runtime, [mutating], {}, allow_mutation=True)[0]["passed"]
    assert runtime.received[-1][0] == "변경"

    case = {
        "id": "projection",
        "question": "처음",
        "turns": [{"question": "다음"}],
    }

    class StateRuntime:
        def __init__(self):
            self.received = []

        def run(self, question, state, conversation_id=None):
            self.received.append(copy.deepcopy(state))
            yield {"type": "RUN_STARTED", "thread_id": conversation_id or "thread"}
            if question == "처음":
                yield {
                    "type": "STATE_DELTA",
                    "delta": [{"op": "replace", "path": "/floor", "value": "4F"}],
                }
            yield {"type": "TEXT_MESSAGE_CONTENT", "delta": "완료"}
            yield {"type": "RUN_FINISHED"}

    state_runtime = StateRuntime()
    run_cases(
        state_runtime,
        [case],
        {
            "ui_state": {
                "rev": "R2",
                "floor": "3F",
                "selection": ["3F-C05"],
                "heatmap": {"metric": "sales_per_m2", "values": {"3F-C05": 1}},
                "highlights": ["3F-C05"],
                "panel": {"type": "layout"},
            }
        },
    )
    assert state_runtime.received[1] == {
        "rev": "R2",
        "floor": "4F",
        "selection": ["3F-C05"],
        "overlays": ["heatmap", "highlights"],
    }


def test_final_overlay_state_must_survive_later_ui_calls():
    events = [
        {"type": "TOOL_CALL_START", "tool_call_id": "h", "tool_name": "ui_show_heatmap", "args": {}},
        {
            "type": "STATE_DELTA",
            "delta": [{"op": "replace", "path": "/heatmap", "value": {"values": {"4F-E05": 1}}}],
        },
        {"type": "TOOL_CALL_END", "tool_call_id": "h", "ok": True},
        {"type": "TOOL_CALL_START", "tool_call_id": "c", "tool_name": "ui_compare", "args": {}},
        {
            "type": "STATE_DELTA",
            "delta": [
                {"op": "replace", "path": "/heatmap", "value": None},
                {"op": "replace", "path": "/compareRev", "value": "R1"},
            ],
        },
        {"type": "TOOL_CALL_END", "tool_call_id": "c", "ok": True},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "비교했어요."},
        {"type": "RUN_FINISHED"},
    ]
    result = score(
        {
            "id": "overlay",
            "question": "q",
            "expect": {"ui_any": ["ui_compare"]},
        },
        collect("overlay", events),
    )
    assert not result["passed"]
    assert not next(check for check in result["checks"] if check["check"] == "ui_overlay_final_state")[
        "passed"
    ]

    empty_heatmap = collect("empty", _events())
    empty_heatmap.state["heatmap"]["values"] = {}
    assert not score(
        {"id": "empty", "question": "q", "expect": {"ui_overlays": ["heatmap"]}},
        empty_heatmap,
    )["passed"]

    cleared = collect(
        "clear",
        [
            {"type": "TOOL_CALL_START", "tool_call_id": "c", "tool_name": "ui_clear", "args": {}},
            {
                "type": "STATE_DELTA",
                "delta": [{"op": "replace", "path": "/heatmap", "value": None}],
            },
            {"type": "TOOL_CALL_END", "tool_call_id": "c", "ok": True},
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "지웠어요."},
            {"type": "RUN_FINISHED"},
        ],
        {"heatmap": {"values": {"4F-E05": 1}}},
    )
    assert score(
        {
            "id": "clear",
            "question": "q",
            "expect": {"ui_any": ["ui_clear"], "state_equals": {"/heatmap": None}},
        },
        cleared,
    )["passed"]


def test_rubric_hashes_and_rescore_compare_recorded_and_current_rules():
    defaults, cases = load_cases(__import__("pathlib").Path(GOLDEN))
    hashes = rubric_hashes(__import__("pathlib").Path(GOLDEN), defaults, cases)
    assert hashes["rubric_version"] == 2
    assert len(hashes["rubric_hash"]) == 64 and len(hashes["cases_hash"]) == 64

    run = collect(
        "rescore",
        [
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "기준안 결과예요."},
            {"type": "RUN_FINISHED"},
        ],
    )
    saved = {
        "id": "rescore",
        "question": "q",
        "attempt": 1,
        "turn": 1,
        "passed": True,
        "checks": [{"check": "recorded", "passed": True}],
        "run": __import__("dataclasses").asdict(run),
    }
    current = [{"id": "rescore", "question": "q", "expect": {"answer_patterns": ["최적해"]}}]
    rescored = rescore_saved_results([saved], {}, current)
    assert rescored[0]["recorded"]["passed"] is True
    assert rescored[0]["rescored"]["passed"] is False
    assert rescored[0]["checks"][0]["check"] == "answer_patterns"


def test_empty_or_truncated_answers_fail_even_without_expectations():
    assert not score({"id": "a", "question": "q"}, collect("a", [{"type": "RUN_FINISHED"}]))["passed"]
    assert not score(
        {"id": "a", "question": "q"}, collect("a", [{"type": "TEXT_MESSAGE_CONTENT", "delta": "답"}])
    )["passed"]


def test_explicitly_incomplete_run_fails_even_with_a_nonempty_answer():
    run = collect(
        "a",
        [
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "요청을 완료하지 못했습니다."},
            {"type": "RUN_FINISHED", "outcome": "no_answer", "incomplete": True},
        ],
    )
    result = score({"id": "a", "question": "q"}, run)

    assert run.finished and run.incomplete
    assert not result["passed"]
    assert not next(check for check in result["checks"] if check["check"] == "run_complete")["passed"]


def test_followup_turns_share_conversation_and_state_but_repeats_are_independent():
    class Runtime:
        def __init__(self):
            self.received = []

        def run(self, message, state, conversation_id=None):
            self.received.append((message, state.copy(), conversation_id))
            yield {"type": "RUN_STARTED", "thread_id": conversation_id or f"thread-{len(self.received)}"}
            yield {"type": "STATE_DELTA", "delta": [{"op": "replace", "path": "/floor", "value": "4F"}]}
            yield {"type": "TEXT_MESSAGE_CONTENT", "delta": "완료"}
            yield {"type": "RUN_FINISHED"}

    runtime = Runtime()
    cases = [{"id": "a", "question": "첫 질문", "turns": [{"question": "후속 질문"}]}]
    results = run_cases(runtime, cases, {"ui_state": {"floor": "3F"}}, repeat=2)
    assert len(results) == 4 and all(r["passed"] for r in results)
    assert runtime.received == [
        ("첫 질문", {"floor": "3F"}, None),
        ("후속 질문", {"floor": "4F", "overlays": []}, "thread-1"),
        ("첫 질문", {"floor": "3F"}, None),
        ("후속 질문", {"floor": "4F", "overlays": []}, "thread-3"),
    ]
    assert "고유 질문 문장: 2개" in summarize(results)
    assert "모든 턴 통과 대화: 2/2" in summarize(results)
    results[0]["passed"] = False
    assert "모든 독립 실행에서 모든 턴 통과한 사례: 0/1" in summarize(results)


def test_scorecard_runs_on_the_small_synthetic_store(tmp_path, populate):
    populate(tmp_path)
    checks = run_scorecard(tmp_path)
    by_id = {check.check_id: check for check in checks}
    assert by_id["parse-R0"].passed is None and by_id["diff-R0-R1"].passed is None
    assert by_id["parse-R0"].note == "정답 파일 없음: layout_R0.json"
    assert by_id["diff-R0-R1"].note == "정답 파일 없음: changes_R0_R1.json"
    assert [check.check_id for check in checks] == expected_check_inventory()
    for check_id in ("low-efficiency-band", "exposure-trap", "layout-constraints"):
        assert by_id[check_id].passed is not None, by_id[check_id].note
    assert by_id["layout-constraints"].passed
    assert "| 결과 | 점검 |" in to_markdown(checks)
    assert exit_code([by_id["parse-R0"]]) == 1
    assert exit_code([by_id["parse-R0"]], allow_skips=True) == 0


def test_scorecard_calculation_errors_fail_instead_of_becoming_skips(tmp_path, populate, monkeypatch):
    from cad2anything.evaluation import scorecard

    populate(tmp_path)

    def broken(*_args, **_kwargs):
        raise ValueError("calculation broke")

    monkeypatch.setattr(scorecard.Toolkit, "analytics_productivity", broken)
    checks = run_scorecard(tmp_path)
    check = next(check for check in checks if check.check_id == "low-efficiency-band")
    assert check.passed is False
    assert check.note == "계산 오류 (ValueError): calculation broke"
