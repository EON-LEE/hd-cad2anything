import pytest

from cad2anything.evaluation.agent_eval import collect, load_cases, score, summarize
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


def test_scorecard_runs_on_the_small_synthetic_store(tmp_path, populate):
    populate(tmp_path)
    checks = run_scorecard(tmp_path)
    by_id = {check.check_id: check for check in checks}
    assert by_id["parse-R0"].passed is None and by_id["diff-R0-R1"].passed is None
    for check_id in ("low-efficiency-band", "exposure-trap", "layout-constraints"):
        assert by_id[check_id].passed is not None, by_id[check_id].note
    assert by_id["layout-constraints"].passed
    assert "| 결과 | 점검 |" in to_markdown(checks)
