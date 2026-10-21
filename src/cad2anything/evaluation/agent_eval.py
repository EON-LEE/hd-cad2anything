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
import json
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
)


@dataclass
class CaseRun:
    case_id: str
    text: str = ""
    tools: list[str] = field(default_factory=list)
    ui_actions: list[str] = field(default_factory=list)
    specialists: list[str] = field(default_factory=list)
    consult_requests: list[dict[str, str]] = field(default_factory=list)
    state_paths: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0


def collect(case_id: str, events: Iterable[dict[str, Any]]) -> CaseRun:
    """Fold AG-UI compatible events into what the checks need; exceptions become ``error``."""
    run = CaseRun(case_id)
    started = time.perf_counter()
    try:
        for event in events:
            kind = event.get("type")
            if kind == "TOOL_CALL_START":
                name = str(event.get("tool_name"))
                run.tools.append(name)
                args = event.get("args") or {}
                if name.startswith("ui_"):
                    run.ui_actions.append(name)
                elif name == "consult_specialist":
                    run.specialists.append(str(args.get("agent", "")))
                    run.consult_requests.append(
                        {"agent": str(args.get("agent", "")), "request": str(args.get("request", ""))}
                    )
            elif kind == "STATE_DELTA":
                run.state_paths.extend(str(op.get("path")) for op in event.get("delta", []))
            elif kind == "TEXT_MESSAGE_CONTENT":
                run.text += str(event.get("delta", ""))
            elif kind == "RUN_ERROR":
                run.error = str(event.get("message"))
    except Exception as exc:  # the runtime re-raises after RUN_ERROR; keep evaluating other cases
        run.error = run.error or f"{type(exc).__name__}: {exc}"
    run.seconds = round(time.perf_counter() - started, 1)
    return run


def _check(kind: str, expected: Any, run: CaseRun) -> bool:
    if kind == "tools_any":
        return any(tool in run.tools for tool in expected)
    if kind == "tools_all":
        return all(tool in run.tools for tool in expected)
    if kind == "tools_none":
        return not any(tool in run.tools for tool in expected)
    if kind == "ui_any":
        return any(action in run.ui_actions for action in expected)
    if kind == "specialists_any":
        return any(agent in run.specialists for agent in expected)
    if kind == "answer_any":
        return any(text in run.text for text in expected)
    if kind == "answer_all":
        return all(text in run.text for text in expected)
    if kind == "answer_none":
        return not any(text in run.text for text in expected)
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
    defaults = data.get("defaults", {})
    cases = data["cases"]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    for case in cases:
        case["expect"] = _merge_expect(defaults.get("expect") or {}, case.get("expect") or {})
        unknown = set(case["expect"]) - set(CHECK_TYPES)
        if unknown:
            raise ValueError(f"{case['id']}: unknown check types {sorted(unknown)}")
    return defaults, cases


def run_cases(
    runtime: Any,
    cases: list[dict[str, Any]],
    defaults: dict[str, Any],
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    results = []
    for case in cases:
        ui_state = {**defaults.get("ui_state", {}), **case.get("ui_state", {})}
        run = collect(case["id"], runtime.run(case["question"], ui_state))
        result = score(case, run)
        results.append(result)
        if on_result:
            on_result(result)
    return results


def summarize(results: list[dict[str, Any]]) -> str:
    lines = ["| 결과 | 사례 | 시나리오 | 시간(초) | 실패한 점검 |", "|---|---|---|---:|---|"]
    for result in results:
        failed = [c["check"] for c in result["checks"] if not c["passed"]]
        if result["run"]["error"]:
            failed.append(f"오류: {result['run']['error'][:60]}")
        status = "통과" if result["passed"] else "실패"
        lines.append(
            f"| {status} | {result['id']} | {result['scenario']} | {result['run']['seconds']} | {', '.join(failed)} |"
        )
    total = len(results)
    passed = sum(r["passed"] for r in results)
    lines.append("")
    lines.append(f"통과 {passed}/{total}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", type=Path, default=Path("eval/golden/agent_questions.yaml"))
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--out", type=Path, help="JSON results path (default eval/results/<timestamp>.json)")
    args = parser.parse_args(argv)
    defaults, cases = load_cases(args.cases)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        cases = [case for case in cases if case["id"] in wanted]

    from cad2anything.services.app import foundry_runtime_factory
    from cad2anything.services.tools import Toolkit, workspace_from_env

    runtime = foundry_runtime_factory(Toolkit(workspace_from_env()))
    results = run_cases(
        runtime,
        cases,
        defaults,
        on_result=lambda r: print(
            f"{'통과' if r['passed'] else '실패'} {r['id']} ({r['run']['seconds']}s)", flush=True
        ),
    )
    out = args.out or Path("eval/results") / f"agent-eval-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summarize(results))
    print(f"결과 파일: {out}")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
