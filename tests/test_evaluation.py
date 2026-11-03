import pytest

from cad2anything.evaluation.agent_eval import collect, load_cases, run_cases, score, summarize
from cad2anything.evaluation.scorecard import run_scorecard, to_markdown

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
    yield {"type": "STATE_DELTA", "delta": [{"op": "replace", "path": "/heatmap", "value": {}}]}
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


def test_extended_review_has_100_cases_and_115_turns_without_mutating_templates():
    from pathlib import Path

    _, cases = load_cases(Path("eval/golden/agent_review.yaml"))
    assert len(cases) == 100
    assert sum(1 + len(c.get("turns", [])) for c in cases) == 115
    assert len({c["id"] for c in cases}) == 100
    assert len({t["question"] for c in cases for t in [c, *c.get("turns", [])]}) == 99
    by_id = {c["id"]: c for c in cases}
    assert by_id["optimizer-one-move"]["expect"]["tool_args"][0]["equals"] == 1
    assert "tool_args" not in by_id["layout-4f-alternatives"]["expect"]
    assert all("answer_none" in c["expect"] for c in cases)


@pytest.mark.parametrize(
    ("text", "passed"),
    [
        ("Studio Korara는 88.7% 증가했고 비교군보다 컸지만, 증가분 전체를 이동 효과로 볼 수는 없어요.", True),
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


def test_empty_or_truncated_answers_fail_even_without_expectations():
    assert not score({"id": "a", "question": "q"}, collect("a", [{"type": "RUN_FINISHED"}]))["passed"]
    assert not score(
        {"id": "a", "question": "q"}, collect("a", [{"type": "TEXT_MESSAGE_CONTENT", "delta": "답"}])
    )["passed"]


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
        ("후속 질문", {"floor": "4F"}, "thread-1"),
        ("첫 질문", {"floor": "3F"}, None),
        ("후속 질문", {"floor": "4F"}, "thread-3"),
    ]
    results[0]["passed"] = False
    assert "모든 독립 실행에서 통과한 사례: 0/1" in summarize(results)


def test_scorecard_runs_on_the_small_synthetic_store(tmp_path, populate):
    populate(tmp_path)
    checks = run_scorecard(tmp_path)
    by_id = {check.check_id: check for check in checks}
    assert by_id["parse-R0"].passed is None and by_id["diff-R0-R1"].passed is None
    for check_id in ("low-efficiency-band", "exposure-trap", "layout-constraints"):
        assert by_id[check_id].passed is not None, by_id[check_id].note
    assert by_id["layout-constraints"].passed
    assert "| 결과 | 점검 |" in to_markdown(checks)
