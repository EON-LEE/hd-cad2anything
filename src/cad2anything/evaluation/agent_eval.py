"""Evaluate the Foundry agents on Korean golden questions.

Each case runs one chat through the orchestrator and checks rule-based expectations: which tools were
called (including calls made inside specialist agents), which UI actions changed the screen, which facts
appear in the answer, and whether requests to the web researcher leaked internal figures.

Usage:
    uv run --no-sync python -m cad2anything.evaluation.agent_eval --cases eval/golden/agent_questions.yaml
        [--only ID,ID] [--out eval/results/run.json]

Environment: FOUNDRY_PROJECT_ENDPOINT, plus WORKSPACE_DIR (and WORKSPACE_MIRROR settings) for heatmap values.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

CHECK_TYPES = (
    "tools_any",
    "tools_all",
    "tools_none",
    "ui_any",
    "specialists_any",
    "answer_any",
    "answer_all",
    "answer_none",
    "consult_not_regex",
    "answer_patterns",
    "answer_forbidden_patterns",
    "state_equals",
    "tool_args",
    "tool_values",
)


@dataclass
class CaseRun:
    case_id: str
    text: str = ""
    notes: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    ui_actions: list[str] = field(default_factory=list)
    specialists: list[str] = field(default_factory=list)
    consult_requests: list[dict[str, str]] = field(default_factory=list)
    state_paths: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    thread_id: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    finished: bool = False


def collect(
    case_id: str, events: Iterable[dict[str, Any]], initial_state: dict[str, Any] | None = None
) -> CaseRun:
    """Fold AG-UI compatible events into what the checks need; exceptions become ``error``."""
    run = CaseRun(case_id, state=copy.deepcopy(initial_state or {}))
    started = time.perf_counter()
    try:
        for event in events:
            kind = event.get("type")
            if kind == "RUN_STARTED":
                run.thread_id = event.get("thread_id")
            elif kind == "RUN_FINISHED":
                run.finished = True
            elif kind == "TOOL_CALL_START":
                name = str(event.get("tool_name"))
                run.tools.append(name)
                args = event.get("args") or {}
                run.calls.append({"id": event.get("tool_call_id"), "name": name, "args": args, "ok": None})
                if name.startswith("ui_"):
                    run.ui_actions.append(name)
                elif name == "consult_specialist":
                    run.specialists.append(str(args.get("agent", "")))
                    run.consult_requests.append(
                        {"agent": str(args.get("agent", "")), "request": str(args.get("request", ""))}
                    )
            elif kind == "TOOL_CALL_END":
                call = next((c for c in reversed(run.calls) if c["id"] == event.get("tool_call_id")), None)
                if call is not None:
                    call.update(ok=event.get("ok"), output=event.get("output"))
                    if "args" in event:
                        call["args"] = event["args"]
            elif kind == "STATE_DELTA":
                run.state_paths.extend(str(op.get("path")) for op in event.get("delta", []))
                for op in event.get("delta", []):
                    _apply_patch(run.state, op)
            elif kind == "TEXT_MESSAGE_CONTENT":
                run.text += str(event.get("delta", ""))
            elif kind == "THINKING_TEXT_MESSAGE_CONTENT":
                run.notes.append(str(event.get("delta", "")))
            elif kind == "RUN_ERROR":
                run.error = str(event.get("message"))
    except Exception as exc:  # the runtime re-raises after RUN_ERROR; keep evaluating other cases
        run.error = run.error or f"{type(exc).__name__}: {exc}"
    run.seconds = round(time.perf_counter() - started, 1)
    return run


def _pointer(value: Any, path: str) -> Any:
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError(f"not a JSON pointer: {path}")
    for token in path[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def _apply_patch(state: dict[str, Any], op: dict[str, Any]) -> None:
    path = op["path"]
    parent, _, key = path.rpartition("/")
    target = _pointer(state, parent)
    key = key.replace("~1", "/").replace("~0", "~")
    if op["op"] not in ("replace", "add", "remove"):
        raise ValueError(f"unsupported UI patch: {op['op']}")
    if isinstance(target, list):
        if op["op"] == "remove":
            target.pop(int(key))
        elif op["op"] == "add":
            target.insert(len(target) if key == "-" else int(key), copy.deepcopy(op["value"]))
        else:
            target[int(key)] = copy.deepcopy(op["value"])
    elif op["op"] == "remove":
        del target[key]
    else:
        target[key] = copy.deepcopy(op["value"])


def _values_match(value: Any, expected: dict[str, Any]) -> bool:
    try:
        actual = _pointer(value, expected["path"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    if "equals" in expected:
        return actual == expected["equals"]
    if "contains" in expected:
        return isinstance(actual, (list, dict, str)) and expected["contains"] in actual
    if "approx" in expected:
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and abs(actual - expected["approx"]) <= expected.get("tolerance", 0)
        )
    raise ValueError("value check requires equals, contains or approx")


def _check(kind: str, expected: Any, run: CaseRun) -> bool:
    if kind == "tools_any":
        return any(c["name"] in expected and c["ok"] is True for c in run.calls)
    if kind == "tools_all":
        return all(any(c["name"] == tool and c["ok"] is True for c in run.calls) for tool in expected)
    if kind == "tools_none":
        return not any(tool in run.tools for tool in expected)
    if kind == "ui_any":
        return any(c["name"] in expected and c["ok"] is True for c in run.calls)
    if kind == "specialists_any":
        return any(
            c["name"] == "consult_specialist" and c["ok"] is True and c["args"].get("agent") in expected
            for c in run.calls
        )
    if kind == "answer_any":
        return any(text in run.text for text in expected)
    if kind == "answer_all":
        return all(text in run.text for text in expected)
    if kind == "answer_none":
        # Progress notes are shown to users as well, so they must not leak tool names either.
        visible = "\n".join([run.text, *run.notes])
        return not any(text in visible for text in expected)
    if kind == "answer_patterns":
        return all(re.search(pattern, run.text) is not None for pattern in expected)
    if kind == "answer_forbidden_patterns":
        return not any(re.search(pattern, run.text) for pattern in expected)
    if kind == "state_equals":
        return all(
            _values_match(run.state, {"path": path, "equals": value}) for path, value in expected.items()
        )
    if kind in ("tool_args", "tool_values"):
        key = "args" if kind == "tool_args" else "output"
        return all(
            any(
                c["name"] == check["tool"] and c["ok"] is True and _values_match(c.get(key), check)
                for c in run.calls
            )
            for check in expected
        )
    if kind == "consult_not_regex":
        pattern = re.compile(expected["pattern"])
        agents = set(expected.get("agents", []))
        return not any(
            pattern.search(item["request"])
            for item in run.consult_requests
            if not agents or item["agent"] in agents
        )
    raise ValueError(f"unknown check type: {kind}")


def score(case: dict[str, Any], run: CaseRun) -> dict[str, Any]:
    checks = []
    for kind, expected in (case.get("expect") or {}).items():
        if kind not in CHECK_TYPES:
            raise ValueError(f"{case['id']}: unknown check type {kind}")
        checks.append({"check": kind, "expected": expected, "passed": _check(kind, expected, run)})
    checks.extend(
        [
            {"check": "run_finished", "passed": run.finished},
            {"check": "answer_nonempty", "passed": bool(run.text.strip())},
            {"check": "tools_completed", "passed": all(c["ok"] is not None for c in run.calls)},
        ]
    )
    passed = run.error is None and all(c["passed"] for c in checks)
    return {
        "id": case["id"],
        "scenario": case.get("scenario", ""),
        "question": case["question"],
        "passed": passed,
        "checks": checks,
        "run": asdict(run),
    }


def _merge_expect(defaults: dict[str, Any], expect: dict[str, Any]) -> dict[str, Any]:
    """Apply default checks to a case: the case wins, except ``*_none`` lists, which are combined."""
    merged = dict(expect)
    for kind, values in defaults.items():
        if kind not in merged:
            merged[kind] = values
        elif kind.endswith("_none"):
            merged[kind] = list(dict.fromkeys([*merged[kind], *values]))
    return merged


def load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "base_cases" in data:
        base_defaults, base = load_cases(path.parent / data["base_cases"])
        data["defaults"] = {**base_defaults, **data.get("defaults", {})}
        templates = {case["id"]: case for case in base}
        expanded = []
        for item in data["cases"]:
            template = copy.deepcopy(templates[item["extends"]]) if "extends" in item else {}
            template.update({key: value for key, value in item.items() if key != "expect"})
            template["expect"] = {**template.get("expect", {}), **item.get("expect", {})}
            expanded.append(template)
        data["cases"] = [*base, *expanded]
    defaults = data.get("defaults", {})
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    for case in cases:
        case["expect"] = _merge_expect(defaults.get("expect") or {}, case.get("expect") or {})
        for turn in [case, *case.get("turns", [])]:
            unknown = set(turn.get("expect", {})) - set(CHECK_TYPES)
            if unknown:
                raise ValueError(f"{case['id']}: unknown check types {sorted(unknown)}")
    return defaults, cases


def run_cases(
    runtime: Any,
    cases: list[dict[str, Any]],
    defaults: dict[str, Any],
    on_result: Callable[[dict[str, Any]], None] | None = None,
    repeat: int = 1,
) -> list[dict[str, Any]]:
    results = []
    if repeat < 1:
        raise ValueError("repeat must be positive")
    for attempt in range(1, repeat + 1):
        for case in cases:
            ui_state = copy.deepcopy({**defaults.get("ui_state", {}), **case.get("ui_state", {})})
            thread_id = None
            for index, turn in enumerate([case, *case.get("turns", [])]):
                current = {
                    **case,
                    **turn,
                    "expect": _merge_expect(defaults.get("expect", {}), turn.get("expect", {})),
                }
                events = (
                    runtime.run(turn["question"], ui_state, conversation_id=thread_id)
                    if thread_id
                    else runtime.run(turn["question"], ui_state)
                )
                run = collect(case["id"], events, ui_state)
                result = score(current, run)
                result.update(attempt=attempt, turn=index + 1)
                results.append(result)
                if on_result:
                    on_result(result)
                ui_state, thread_id = run.state, run.thread_id
                if not result["passed"] and (run.error or not run.finished):
                    break
    return results


def summarize(results: list[dict[str, Any]]) -> str:
    lines = [
        "| 결과 | 사례 | 실행 | 턴 | 시나리오 | 시간(초) | 실패한 점검 |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for result in results:
        failed = [c["check"] for c in result["checks"] if not c["passed"]]
        if result["run"]["error"]:
            failed.append(f"오류: {result['run']['error'][:60]}")
        status = "통과" if result["passed"] else "실패"
        lines.append(
            f"| {status} | {result['id']} | {result.get('attempt', 1)} | {result.get('turn', 1)} | "
            f"{result['scenario']} | {result['run']['seconds']} | {', '.join(failed)} |"
        )
    total = len(results)
    passed = sum(r["passed"] for r in results)
    lines.append("")
    lines.append(f"통과 {passed}/{total}")
    attempts = sorted({r.get("attempt", 1) for r in results})
    for attempt in attempts:
        rows = [r for r in results if r.get("attempt", 1) == attempt]
        lines.append(f"독립 실행 {attempt}: {sum(r['passed'] for r in rows)}/{len(rows)}턴 통과")
    groups: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        groups.setdefault(result["id"], []).append(result)
    stable = sum(
        all(r["passed"] for r in rows) and {r.get("attempt", 1) for r in rows} == set(attempts)
        for rows in groups.values()
    )
    lines.append(f"모든 독립 실행에서 통과한 사례: {stable}/{len(groups)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", type=Path, default=Path("eval/golden/agent_questions.yaml"))
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--out", type=Path, help="JSON results path (default eval/results/<timestamp>.json)")
    parser.add_argument("--repeat", type=int, default=1, help="independent runs, not automatic retries")
    parser.add_argument(
        "--list", action="store_true", help="list case and turn counts without calling Foundry"
    )
    args = parser.parse_args(argv)
    defaults, cases = load_cases(args.cases)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        unknown = wanted - {case["id"] for case in cases}
        if unknown:
            parser.error(f"unknown case ids: {sorted(unknown)}")
        cases = [case for case in cases if case["id"] in wanted]
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.list:
        print(
            f"cases={len(cases)} turns={sum(1 + len(c.get('turns', [])) for c in cases)} repeat={args.repeat}"
        )
        return 0

    from cad2anything.services.app import foundry_runtime_factory
    from cad2anything.services.tools import Toolkit, workspace_from_env

    runtime = foundry_runtime_factory(Toolkit(workspace_from_env()))
    runtime.capture_tool_outputs = True
    out = args.out or Path("eval/results") / f"agent-eval-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    completed: list[dict[str, Any]] = []

    def save_result(result: dict[str, Any]) -> None:
        completed.append(result)
        out.write_text(json.dumps(completed, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            f"{'통과' if result['passed'] else '실패'} {result['id']} "
            f"(run={result['attempt']} turn={result['turn']}, {result['run']['seconds']}s)",
            flush=True,
        )

    results = run_cases(
        runtime,
        cases,
        defaults,
        on_result=save_result,
        repeat=args.repeat,
    )
    print(summarize(results))
    print(f"결과 파일: {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
