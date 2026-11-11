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
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

import yaml

CHECK_TYPES = (
    "tools_any",
    "tools_all",
    "tools_none",
    "ui_any",
    "ui_overlays",
    "specialists_any",
    "answer_any",
    "answer_all",
    "answer_none",
    "answer_not",
    "consult_not_regex",
    "egress_not_regex",
    "answer_patterns",
    "answer_positive_any",
    "answer_forbidden_patterns",
    "answer_numbers_from",
    "answer_entities_from",
    "state_equals",
    "tool_args",
    "tool_values",
    "tool_calls",
)

UI_OVERLAY_PATHS = {
    "heatmap": "heatmap",
    "highlights": "highlights",
    "edges": "edges",
    "flow": "flow",
    "route": "route",
    "journey": "journey",
    "compare": "compareRev",
}
UI_TOOL_OVERLAYS = {
    "ui_show_heatmap": "heatmap",
    "ui_highlight_units": "highlights",
    "ui_draw_edges": "edges",
    "ui_show_flow": "flow",
    "ui_show_route": "route",
    "ui_show_journey": "journey",
    "ui_compare": "compare",
}
MARKET_RESEARCHER_KEYS = {"researcher", "market_researcher", "market_research", "marketresearcher"}
EGRESS_SEARCH_RE = re.compile(
    r"(?:^|[_-])(?:web|bing)(?:[_-]?(?:grounding[_-]?)?search)(?:[_-]|$)"
    r"|(?:^|[_-])search[_-](?:web|bing)(?:[_-]|$)",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![\w-])(?P<value>\d[\d,]*(?:\.\d+)?)(?![\w-])")
NUMBER_WORDS = {
    "하나": 1.0,
    "한": 1.0,
    "둘": 2.0,
    "두": 2.0,
    "셋": 3.0,
    "세": 3.0,
    "넷": 4.0,
    "네": 4.0,
    "다섯": 5.0,
    "여섯": 6.0,
    "일곱": 7.0,
    "여덟": 8.0,
    "아홉": 9.0,
    "열": 10.0,
}
NEGATION_RE = re.compile(
    r"없(?:어요|습니다|다|는|을|다고|어서|으며)?|없이|아니(?:에요|예요|다|라고)?|"
    r"않(?:아요|습니다|는다|고|은)?|못|불필요|발생하지\s*않",
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
    error_class: str | None = None
    http_status: int | None = None
    request_id: str | None = None
    provider_request_id: str | None = None
    retry_after: str | int | float | None = None
    seconds: float = 0.0
    thread_id: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    finished: bool = False
    incomplete: bool = False


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
                run.incomplete = bool(event.get("incomplete")) or event.get("outcome") not in (
                    None,
                    "answered",
                )
            elif kind == "TOOL_CALL_START":
                name = str(event.get("tool_name"))
                run.tools.append(name)
                args = _as_dict(event.get("args") or event.get("arguments"))
                run.calls.append(
                    {
                        "id": event.get("tool_call_id"),
                        "name": name,
                        "args": copy.deepcopy(args),
                        "ok": None,
                        "parent_tool_call_id": event.get("parent_tool_call_id"),
                        "agent": event.get("agent"),
                    }
                )
                if name.startswith("ui_"):
                    run.ui_actions.append(name)
            elif kind == "TOOL_CALL_END":
                call = next(
                    (
                        c
                        for c in reversed(run.calls)
                        if c["id"] == event.get("tool_call_id")
                        and (
                            "parent_tool_call_id" not in event
                            or c.get("parent_tool_call_id") == event.get("parent_tool_call_id")
                        )
                    ),
                    None,
                )
                if call is not None:
                    call.update(ok=event.get("ok"), output=event.get("output"))
                    if "args" in event or "arguments" in event:
                        call["args"] = copy.deepcopy(_as_dict(event.get("args") or event.get("arguments")))
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
                _record_error(run, event.get("error") or event, event)
    except Exception as exc:  # the runtime re-raises after RUN_ERROR; keep evaluating other cases
        run.error = run.error or f"{type(exc).__name__}: {exc}"
        _record_error(run, exc)
    consults = [call for call in run.calls if call["name"] == "consult_specialist"]
    run.specialists = [str(call.get("args", {}).get("agent", "")) for call in consults]
    run.consult_requests = [
        {
            "agent": str(call.get("args", {}).get("agent", "")),
            "request": str(call.get("args", {}).get("request", "")),
        }
        for call in consults
    ]
    run.seconds = round(time.perf_counter() - started, 1)
    return run


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _first_value(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _header_value(headers: Any, *names: str) -> Any:
    if headers is None:
        return None
    for name in names:
        getter = getattr(headers, "get", None)
        value = getter(name) if callable(getter) else _get(headers, name)
        if value not in (None, ""):
            return value
        if isinstance(headers, dict) or callable(getattr(headers, "items", None)):
            value = next(
                (item for key, item in headers.items() if str(key).casefold() == name.casefold()), None
            )
            if value not in (None, ""):
                return value
    return None


def _record_error(run: CaseRun, source: Any, event: dict[str, Any] | None = None) -> None:
    sources = [event, source, _get(source, "response"), _get(source, "body")]
    error_class = _first_value(
        *(_get(item, "error_class") for item in sources if item is not None),
        type(source).__name__ if isinstance(source, BaseException) else None,
    )
    run.error_class = run.error_class or error_class
    status = _first_value(
        *(
            _get(item, name)
            for item in sources
            if item is not None
            for name in ("http_status", "status_code", "status")
        )
    )
    if status is not None:
        with contextlib.suppress(TypeError, ValueError):
            run.http_status = int(status)
    headers = [_get(item, "headers") for item in sources if item is not None]
    request_id = _first_value(
        *(
            _get(item, name)
            for item in sources
            if item is not None
            for name in ("provider_request_id", "request_id", "requestId", "apim_request_id")
        ),
        *(
            _header_value(
                item,
                "x-request-id",
                "request-id",
                "x-ms-request-id",
                "apim-request-id",
                "x-ms-correlation-request-id",
            )
            for item in headers
        ),
    )
    if request_id is not None:
        run.request_id = run.request_id or str(request_id)
        run.provider_request_id = run.provider_request_id or str(request_id)
    retry_after = _first_value(
        *(_get(item, "retry_after") or _get(item, "retry-after") for item in sources if item is not None),
        *(_header_value(item, "retry-after", "x-ms-retry-after-ms") for item in headers),
    )
    if retry_after is not None:
        run.retry_after = retry_after


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


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _matches_value_checks(value: Any, checks: Any) -> bool:
    return all(_values_match(value, check) for check in _items(checks))


def _matching_calls(
    run: CaseRun, tool: str, args_checks: Any = None, output_checks: Any = None
) -> list[dict[str, Any]]:
    return [
        call
        for call in run.calls
        if call["name"] == tool
        and call["ok"] is True
        and _matches_value_checks(call.get("args"), args_checks)
        and _matches_value_checks(call.get("output"), output_checks)
    ]


def _call_predicate_matches(call: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        call["name"] == expected["tool"]
        and call["ok"] is True
        and _matches_value_checks(call.get("args"), expected.get("args"))
        and _matches_value_checks(
            call.get("output"), expected.get("values", expected.get("output", expected.get("tool_values")))
        )
    )


def _paired_tool_checks(expect: dict[str, Any]) -> list[dict[str, Any]]:
    args_by_tool: dict[str, list[dict[str, Any]]] = {}
    values_by_tool: dict[str, list[dict[str, Any]]] = {}
    for check in _items(expect.get("tool_args")):
        args_by_tool.setdefault(check["tool"], []).append(check)
    for check in _items(expect.get("tool_values")):
        values_by_tool.setdefault(check["tool"], []).append(check)
    return [
        {"tool": tool, "args": args_by_tool[tool], "values": values_by_tool[tool]}
        for tool in sorted(args_by_tool.keys() & values_by_tool.keys())
    ]


def _is_egress_call(call: dict[str, Any]) -> bool:
    name = str(call.get("name", "")).casefold().replace("-", "_")
    if name == "consult_specialist":
        agent = str(call.get("args", {}).get("agent", "")).casefold().replace("-", "_")
        return agent in MARKET_RESEARCHER_KEYS
    return bool(EGRESS_SEARCH_RE.search(name)) or ("search" in name and ("web" in name or "bing" in name))


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [float(value)] if math.isfinite(value) else []
    if isinstance(value, str):
        try:
            return [float(value.replace(",", ""))]
        except ValueError:
            return []
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numbers(item)]
    if isinstance(value, list | tuple):
        return [number for item in value for number in _numbers(item)]
    return []


def _parse_answer_number(value: str) -> float | None:
    normalized = value.strip().replace(",", "")
    if normalized in NUMBER_WORDS:
        return NUMBER_WORDS[normalized]
    try:
        number = float(normalized)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _answer_number_mentions(text: str, pattern: str | None) -> list[float]:
    regex = re.compile(pattern) if pattern else NUMBER_RE
    numbers = []
    for match in regex.finditer(text):
        groups = match.groupdict()
        captured = groups.get("value") or groups.get("number")
        if captured is None and match.lastindex:
            captured = match.group(1)
        if captured is None:
            found = NUMBER_RE.search(match.group(0))
            captured = found.group("value") if found else None
        if captured is None:
            continue
        number = _parse_answer_number(captured)
        if number is None:
            return []
        numbers.append(number)
    return numbers


def _answer_numbers_match(spec: dict[str, Any], run: CaseRun) -> bool:
    tolerance = float(spec.get("tolerance", 0))
    if tolerance < 0 or not math.isfinite(tolerance):
        raise ValueError("answer_numbers_from tolerance must be finite and nonnegative")
    matching = _matching_calls(run, spec["tool"], spec.get("args"))
    evidence = []
    for call in matching:
        try:
            evidence.extend(_numbers(_pointer(call.get("output"), spec["path"])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    if not evidence:
        return False
    mentions = _answer_number_mentions(run.text, spec.get("answer_pattern") or spec.get("pattern"))
    return bool(mentions) and all(
        any(abs(mention - expected) <= tolerance for expected in evidence) for mention in mentions
    )


def _row_matches(row: Any, where: dict[str, Any]) -> bool:
    return isinstance(row, dict) and all(row.get(key) in _items(value) for key, value in where.items())


def _entity_values(value: Any, spec: dict[str, Any]) -> list[str]:
    rows = value if isinstance(value, list) else [value]
    where = spec.get("where") or {}
    fields = _items(spec.get("fields"))
    values = []
    for row in rows:
        if where and not _row_matches(row, where):
            continue
        if fields:
            if not isinstance(row, dict):
                continue
            values.extend(
                str(row[field]) for field in fields if field in row and row[field] not in (None, "")
            )
        elif isinstance(row, str):
            values.append(row)
    return values


def _answer_entities_match(spec: dict[str, Any], run: CaseRun) -> bool:
    evidence = []
    output_found = False
    for call in _matching_calls(run, spec["tool"], spec.get("args")):
        try:
            value = _pointer(call.get("output"), spec["path"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        output_found = True
        evidence.extend(_entity_values(value, spec))
    if not output_found:
        return False
    if not evidence:
        empty_pattern = spec.get("empty_answer_pattern")
        if empty_pattern:
            return re.search(empty_pattern, run.text) is not None
        return spec.get("require_nonempty", True) is False

    answer_pattern = spec.get("answer_pattern") or spec.get("pattern")
    if answer_pattern:
        matches = list(re.finditer(answer_pattern, run.text))
        if not matches:
            return False
        mentioned = []
        for match in matches:
            groups = match.groupdict()
            captured = groups.get("entity") or groups.get("value")
            if captured is None and match.lastindex:
                captured = match.group(1)
            mentioned.append(str(captured if captured is not None else match.group(0)).strip())
        known = {value.casefold() for value in evidence}
        return all(value.casefold() in known for value in mentioned)

    found = [value for value in evidence if value.casefold() in run.text.casefold()]
    if spec.get("ordered"):
        if len(found) != len(evidence):
            return False
        positions = [run.text.casefold().find(value.casefold()) for value in evidence]
        if positions != sorted(positions):
            return False
    return bool(found) if spec.get("require_all") is False else len(found) == len(evidence)


def _has_overlay(state: dict[str, Any], overlay: str) -> bool:
    path = UI_OVERLAY_PATHS[overlay]
    value = state.get(path)
    if value is None:
        return False
    if overlay == "heatmap":
        return isinstance(value, dict) and bool(value.get("values"))
    if overlay in ("highlights", "edges"):
        return isinstance(value, list) and bool(value)
    if overlay == "flow":
        return isinstance(value, dict) and (
            bool(value.get("markers"))
            or any(layer.get("segments") for layer in (value.get("floors") or {}).values())
        )
    if overlay == "route":
        return isinstance(value, dict) and (
            bool(value.get("segments")) or bool(value.get("origin") and value.get("destination"))
        )
    if overlay == "journey":
        return isinstance(value, dict) and bool(value.get("stops"))
    if overlay == "compare":
        return value not in (None, "", [])
    return bool(value) if isinstance(value, list | dict | str) else bool(value)


def _expected_ui_clear(expect: dict[str, Any]) -> bool:
    return "ui_clear" in _items(expect.get("ui_any"))


def _nonnegated_mention(text: str, pattern: str) -> bool:
    separators = re.compile(r"[.!?。；;\n,，]")
    for match in re.finditer(pattern, text):
        start = max(
            (separator.end() for separator in separators.finditer(text, 0, match.start())),
            default=0,
        )
        end_match = separators.search(text, match.end())
        end = end_match.start() if end_match else len(text)
        if not NEGATION_RE.search(text[start:end]):
            return True
    return False


def _check(kind: str, expected: Any, run: CaseRun) -> bool:
    if kind == "tools_any":
        return any(c["name"] in _items(expected) and c["ok"] is True for c in run.calls)
    if kind == "tools_all":
        return all(any(c["name"] == tool and c["ok"] is True for c in run.calls) for tool in _items(expected))
    if kind == "tools_none":
        return not any(tool in run.tools for tool in _items(expected))
    if kind == "ui_any":
        return any(c["name"] in _items(expected) and c["ok"] is True for c in run.calls)
    if kind == "ui_overlays":
        overlays = _items(expected)
        if not overlays:
            return False
        if any(overlay not in UI_OVERLAY_PATHS for overlay in overlays):
            raise ValueError(f"unknown UI overlay: {expected}")
        return all(_has_overlay(run.state, overlay) for overlay in overlays)
    if kind == "specialists_any":
        return any(
            c["name"] == "consult_specialist"
            and c["ok"] is True
            and c["args"].get("agent") in _items(expected)
            for c in run.calls
        )
    if kind == "answer_any":
        return any(text in run.text for text in _items(expected))
    if kind == "answer_all":
        return all(text in run.text for text in _items(expected))
    if kind == "answer_none":
        # Progress notes are shown to users as well, so they must not leak tool names either.
        visible = "\n".join([run.text, *run.notes])
        return not any(text in visible for text in _items(expected))
    if kind == "answer_not":
        return not any(re.search(pattern, run.text) for pattern in _items(expected))
    if kind == "answer_patterns":
        return all(re.search(pattern, run.text) is not None for pattern in _items(expected))
    if kind == "answer_positive_any":
        return any(_nonnegated_mention(run.text, pattern) for pattern in _items(expected))
    if kind in ("answer_forbidden_patterns",):
        return not any(re.search(pattern, run.text) for pattern in _items(expected))
    if kind == "answer_numbers_from":
        return all(_answer_numbers_match(spec, run) for spec in _items(expected))
    if kind == "answer_entities_from":
        return all(_answer_entities_match(spec, run) for spec in _items(expected))
    if kind == "state_equals":
        return all(
            _values_match(run.state, {"path": path, "equals": value}) for path, value in expected.items()
        )
    if kind in ("tool_args", "tool_values"):
        key = "args" if kind == "tool_args" else "output"
        return all(
            bool(
                _matching_calls(
                    run,
                    check["tool"],
                    args_checks=[check] if key == "args" else None,
                    output_checks=[check] if key == "output" else None,
                )
            )
            for check in _items(expected)
        )
    if kind == "tool_calls":
        return all(
            any(_call_predicate_matches(call, check) for call in run.calls) for check in _items(expected)
        )
    if kind == "consult_not_regex":
        pattern = re.compile(expected["pattern"])
        agents = set(_items(expected.get("agents")))
        return not any(
            pattern.search(str(call.get("args", {}).get("request", "")))
            for call in run.calls
            if call["name"] == "consult_specialist"
            and (not agents or call.get("args", {}).get("agent") in agents)
        )
    if kind == "egress_not_regex":
        pattern = re.compile(expected["pattern"])
        tools = set(_items(expected.get("tools")))
        agents = set(_items(expected.get("agents")))
        return not any(
            pattern.search(json.dumps(call.get("args", {}), ensure_ascii=False, default=str))
            for call in run.calls
            if _is_egress_call(call)
            and (not tools or call["name"] in tools)
            and (
                not agents
                or call["name"] != "consult_specialist"
                or call.get("args", {}).get("agent") in agents
            )
        )
    raise ValueError(f"unknown check type: {kind}")


def score(case: dict[str, Any], run: CaseRun) -> dict[str, Any]:
    checks = []
    expect = case.get("expect") or {}
    for kind, expected in expect.items():
        if kind not in CHECK_TYPES:
            raise ValueError(f"{case['id']}: unknown check type {kind}")
        checks.append({"check": kind, "expected": expected, "passed": _check(kind, expected, run)})
    for paired in _paired_tool_checks(expect):
        checks.append(
            {
                "check": "tool_call_binding",
                "tool": paired["tool"],
                "expected": paired,
                "passed": bool(
                    _matching_calls(
                        run, paired["tool"], args_checks=paired["args"], output_checks=paired["values"]
                    )
                ),
            }
        )
    if not _expected_ui_clear(expect):
        expected_overlay_tools = {
            tool
            for kind in ("ui_any", "tools_any", "tools_all")
            for tool in _items(expect.get(kind))
            if tool in UI_TOOL_OVERLAYS
        }
        overlay_calls = [
            call for call in run.calls if call["ok"] is True and call["name"] in UI_TOOL_OVERLAYS
        ]
        if expected_overlay_tools:
            overlays = list(dict.fromkeys(UI_TOOL_OVERLAYS[call["name"]] for call in overlay_calls))
            checks.append(
                {
                    "check": "ui_overlay_final_state",
                    "expected": sorted({UI_TOOL_OVERLAYS[tool] for tool in expected_overlay_tools}),
                    "passed": any(call["name"] in expected_overlay_tools for call in overlay_calls)
                    and all(_has_overlay(run.state, overlay) for overlay in overlays),
                }
            )
    checks.extend(
        [
            {"check": "run_finished", "passed": run.finished},
            {"check": "run_complete", "passed": run.finished and not run.incomplete},
            {"check": "answer_nonempty", "passed": bool(run.text.strip())},
            {"check": "tools_completed", "passed": all(c["ok"] is not None for c in run.calls)},
        ]
    )
    passed = run.error is None and all(c["passed"] for c in checks)
    return {
        "id": case["id"],
        "scenario": case.get("scenario", ""),
        "family": case.get("family") or case.get("extends") or case["id"],
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


def _browser_ui_state(state: dict[str, Any], state_paths: Iterable[str] = ()) -> dict[str, Any]:
    projected = {key: copy.deepcopy(state[key]) for key in ("rev", "floor", "selection") if key in state}
    inherited = set(_items(state.get("overlays")))
    changed = set(state_paths)
    visible = []
    for overlay, key in UI_OVERLAY_PATHS.items():
        if key in state:
            if _has_overlay(state, overlay):
                visible.append(overlay)
        elif f"/{key}" not in changed and overlay in inherited:
            visible.append(overlay)
    projected["overlays"] = visible
    return projected


def _is_mutating(case: dict[str, Any]) -> bool:
    return bool(case.get("mutates")) or any(turn.get("mutates") for turn in case.get("turns", []))


def _case_turns(case: dict[str, Any], defaults: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for turn in [case, *case.get("turns", [])]:
        result.append(
            {
                **case,
                **turn,
                "expect": _merge_expect(defaults.get("expect", {}), turn.get("expect", {})),
            }
        )
    return result


def run_cases(
    runtime: Any,
    cases: list[dict[str, Any]],
    defaults: dict[str, Any],
    on_result: Callable[[dict[str, Any]], None] | None = None,
    repeat: int = 1,
    pause_seconds: float = 0,
    error_cooldown_seconds: float = 0,
    allow_mutation: bool = False,
) -> list[dict[str, Any]]:
    results = []
    if repeat < 1:
        raise ValueError("repeat must be positive")
    if pause_seconds < 0 or error_cooldown_seconds < 0:
        raise ValueError("pauses must be nonnegative")
    for attempt in range(1, repeat + 1):
        for case in cases:
            turns = [case, *case.get("turns", [])]
            if _is_mutating(case) and not allow_mutation:
                skipped = {
                    "id": case["id"],
                    "scenario": case.get("scenario", ""),
                    "family": case.get("family") or case.get("extends") or case["id"],
                    "question": case["question"],
                    "questions": [turn["question"] for turn in turns],
                    "passed": None,
                    "checks": [],
                    "run": None,
                    "skipped": True,
                    "skip_reason": "공유 작업공간을 변경하는 사례입니다. 실행하려면 --allow-mutation을 지정하세요.",
                    "attempt": attempt,
                    "turn": 0,
                    "expected_turns": len(turns),
                }
                results.append(skipped)
                if on_result:
                    on_result(skipped)
                continue
            ui_state = copy.deepcopy({**defaults.get("ui_state", {}), **case.get("ui_state", {})})
            thread_id = None
            current_turns = _case_turns(case, defaults)
            for index, turn in enumerate(turns):
                if results:
                    previous_run = results[-1].get("run") or {}
                    delay = (
                        max(pause_seconds, error_cooldown_seconds)
                        if previous_run.get("error")
                        else pause_seconds
                    )
                    if delay:
                        time.sleep(delay)
                current = current_turns[index]
                events = (
                    runtime.run(turn["question"], ui_state, conversation_id=thread_id)
                    if thread_id
                    else runtime.run(turn["question"], ui_state)
                )
                run = collect(case["id"], events, ui_state)
                result = score(current, run)
                result.update(
                    attempt=attempt,
                    turn=index + 1,
                    expected_turns=len(turns),
                )
                results.append(result)
                if on_result:
                    on_result(result)
                ui_state, thread_id = _browser_ui_state(run.state, run.state_paths), run.thread_id
                if not result["passed"] and (run.error or not run.finished):
                    break
    return results


def _is_completed(result: dict[str, Any]) -> bool:
    run = result.get("run") or {}
    return not result.get("skipped") and run.get("finished") is True and run.get("error") is None


def _group_rates(results: list[dict[str, Any]], key: str) -> tuple[list[tuple[str, int, int]], float | None]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if _is_completed(result):
            groups.setdefault(str(result.get(key) or "분류 없음"), []).append(result)
    rates = [
        (name, sum(row.get("passed") is True for row in rows), len(rows))
        for name, rows in sorted(groups.items())
    ]
    macro = sum(passed / total for _, passed, total in rates) / len(rates) if rates else None
    return rates, macro


def summarize(results: list[dict[str, Any]]) -> str:
    lines = [
        "| 결과 | 사례 | 실행 | 턴 | 시나리오 | 시간(초) | 실패한 점검 |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for result in results:
        if result.get("skipped"):
            lines.append(
                f"| 건너뜀 | {result['id']} | {result.get('attempt', 1)} | — | "
                f"{result.get('scenario', '')} | — | {result['skip_reason']} |"
            )
            continue
        run = result.get("run") or {}
        failed = [c["check"] for c in result.get("checks", []) if not c["passed"]]
        if run.get("error"):
            failed.append(f"오류({run.get('error_class') or 'unknown'}): {run['error'][:60]}")
        status = "통과" if result.get("passed") else "실패"
        lines.append(
            f"| {status} | {result['id']} | {result.get('attempt', 1)} | {result.get('turn', 1)} | "
            f"{result.get('scenario', '')} | {run.get('seconds', 0)} | {', '.join(failed)} |"
        )

    executed = [result for result in results if not result.get("skipped")]
    completed = [result for result in executed if _is_completed(result)]
    skipped = [result for result in results if result.get("skipped")]
    passed = sum(result.get("passed") is True for result in executed)
    questions = {str(result["question"]) for result in results if result.get("question")}
    for result in skipped:
        questions.update(str(question) for question in result.get("questions", []))
    lines.extend(
        [
            "",
            f"고유 질문 문장: {len(questions)}개",
            f"실행 가용성(완료 턴): {len(completed)}/{len(executed)} "
            f"(미완료·인프라 오류 {len(executed) - len(completed)}턴)",
            f"완료 턴 규칙 통과: {sum(result.get('passed') is True for result in completed)}/{len(completed)}",
            f"통과 {passed}/{len(executed)} (실패 포함)",
            f"전체 통과(실패 포함): {passed}/{len(executed)}",
            f"건너뜀 사례 실행: {len(skipped)}회",
        ]
    )
    attempts = sorted({r.get("attempt", 1) for r in results})
    for attempt in attempts:
        rows = [r for r in executed if r.get("attempt", 1) == attempt]
        complete_rows = [r for r in rows if _is_completed(r)]
        lines.append(
            f"독립 실행 {attempt}: 완료 {len(complete_rows)}/{len(rows)}, "
            f"완료 턴 규칙 통과 {sum(r.get('passed') is True for r in complete_rows)}/{len(complete_rows)}"
        )

    for label, key in (("시나리오", "scenario"), ("가족", "family")):
        rates, macro = _group_rates(executed, key)
        if macro is not None:
            lines.append(f"{label} 동일 가중 규칙 통과율: {macro:.1%}")
            lines.extend(f"  - {name}: {count}/{total} ({count / total:.1%})" for name, count, total in rates)

    conversations: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for result in executed:
        conversations.setdefault((result["id"], result.get("attempt", 1)), []).append(result)
    conversation_passes = 0
    for rows in conversations.values():
        expected_turns = max((row.get("expected_turns", 1) for row in rows), default=1)
        if len(rows) == expected_turns and all(row.get("passed") is True for row in rows):
            conversation_passes += 1
    lines.append(f"모든 턴 통과 대화: {conversation_passes}/{len(conversations)}")
    case_ids = {case_id for case_id, _ in conversations}
    attempts_by_case: dict[str, set[int]] = {}
    for case_id, attempt in conversations:
        attempts_by_case.setdefault(case_id, set()).add(attempt)
    stable = 0
    for case_id, rows_by_attempt in attempts_by_case.items():
        if rows_by_attempt != set(attempts):
            continue
        if all(
            len(rows) == max((row.get("expected_turns", 1) for row in rows), default=1)
            and all(row.get("passed") is True for row in rows)
            for (row_case, _), rows in conversations.items()
            if row_case == case_id
        ):
            stable += 1
    lines.append(f"모든 독립 실행에서 모든 턴 통과한 사례: {stable}/{len(case_ids)}")
    return "\n".join(lines)


def _browser_base_url(atlas_url: str) -> str:
    parts = urlsplit(atlas_url)
    path = parts.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[:-4]
    return parts._replace(path=path, query="", fragment="").geturl().rstrip("/")


def preflight(
    endpoint: str | None = None,
    atlas_url: str | None = None,
    credential_factory: Callable[[], Any] | None = None,
    http_get: Callable[..., Any] | None = None,
) -> str:
    """Verify local Foundry credentials and an Atlas health endpoint before live evaluation."""
    endpoint = endpoint or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT가 설정되지 않았습니다.")
    if credential_factory is None:
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise RuntimeError(
                "Azure 자격 증명 패키지를 찾을 수 없습니다. agents extra를 설치하세요."
            ) from exc
        credential_factory = DefaultAzureCredential
    credential = None
    try:
        credential = credential_factory()
        token = credential.get_token("https://ai.azure.com/.default")
        if not token:
            raise RuntimeError("빈 토큰")
    except Exception as exc:
        error_name = type(exc).__name__
        raise RuntimeError(f"Azure 자격 증명을 확인하지 못했습니다 ({error_name}).") from None
    finally:
        close = getattr(credential, "close", None)
        if callable(close):
            close()

    atlas_url = atlas_url or os.environ.get("ATLAS_API_URL") or os.environ.get("MCP_SERVER_URL")
    if not atlas_url:
        raise RuntimeError("ATLAS_API_URL 또는 MCP_SERVER_URL을 설정해야 Atlas 상태를 확인할 수 있습니다.")
    base = _browser_base_url(atlas_url)
    opener = http_get or urlopen
    for path in ("readyz", "healthz"):
        response = None
        try:
            response = opener(f"{base}/{path}", timeout=5)
            status = _get(response, "status") or _get(response, "status_code")
            if status is None:
                status = response.getcode()
            if 200 <= int(status) < 300:
                return f"자격 증명 확인 완료, Atlas /{path} 응답 확인"
        except Exception:
            continue
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    hostname = urlsplit(base).hostname or "설정된 Atlas 주소"
    raise RuntimeError(
        f"Azure 자격 증명은 확인했지만 {hostname}의 /readyz 또는 /healthz에 연결하지 못했습니다."
    )


def _canonical_hash(value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def rubric_hashes(path: Path, defaults: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    version = raw.get("version", 1)
    case_rules = [
        {
            "id": case["id"],
            "expect": case.get("expect", {}),
            "turns": [{"expect": turn.get("expect", {})} for turn in case.get("turns", [])],
        }
        for case in cases
    ]
    return {
        "rubric_version": version,
        "rubric_hash": _canonical_hash(
            {"version": version, "defaults": defaults.get("expect", {}), "cases": case_rules}
        ),
        "cases_hash": _canonical_hash({"defaults": defaults, "cases": cases}),
    }


def _git_context() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return {"git_commit": commit, "git_dirty": bool(status.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_dirty": None}


def _endpoint_name(endpoint: str | None = None) -> str | None:
    endpoint = endpoint or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    return urlsplit(endpoint).hostname if endpoint else None


def _agent_inventory(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {}
    inventory = {}
    orchestrator = getattr(runtime, "orchestrator", None)
    specialists = getattr(runtime, "specialists", None)
    versions = getattr(runtime, "agent_versions", None) or getattr(runtime, "versions", None)
    if orchestrator:
        inventory["orchestrator"] = str(orchestrator)
    if specialists:
        inventory["specialists"] = (
            {str(key): str(name) for key, name in sorted(specialists.items())}
            if isinstance(specialists, dict)
            else sorted(str(name) for name in specialists)
        )
    if versions:
        inventory["versions"] = (
            {str(key): str(value) for key, value in versions.items()}
            if isinstance(versions, dict)
            else [str(value) for value in versions]
            if isinstance(versions, list | tuple)
            else str(versions)
        )
    return inventory


def _experiment_envelope(
    cases_path: Path,
    defaults: dict[str, Any],
    cases: list[dict[str, Any]],
    runtime: Any,
    repeat: int,
    pace_seconds: float,
    error_cooldown_seconds: float,
    started_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "mode": "agent_eval",
        "experiment": {
            **_git_context(),
            **rubric_hashes(cases_path, defaults, cases),
            "endpoint_name": _endpoint_name(),
            "agents": _agent_inventory(runtime),
            "started_at": started_at,
            "ended_at": None,
            "repeat": repeat,
            "pace_seconds": pace_seconds,
            "error_cooldown_seconds": error_cooldown_seconds,
        },
        "results": [],
    }


def _run_from_dict(value: dict[str, Any], case_id: str) -> CaseRun:
    fields = CaseRun.__dataclass_fields__
    payload = {key: copy.deepcopy(item) for key, item in value.items() if key in fields}
    payload.setdefault("case_id", case_id)
    return CaseRun(**payload)


def rescore_saved_results(
    document: Any, defaults: dict[str, Any], cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    saved_results = document.get("results", []) if isinstance(document, dict) else document
    cases_by_id = {case["id"]: case for case in cases}
    current_turns = {case_id: _case_turns(case, defaults) for case_id, case in cases_by_id.items()}
    rescored_rows = []
    for saved in saved_results:
        recorded = saved.get("recorded") or {
            "passed": saved.get("passed"),
            "checks": saved.get("checks", []),
        }
        if saved.get("skipped"):
            rescored_rows.append(
                {
                    **saved,
                    "recorded": recorded,
                    "rescored": {
                        "passed": None,
                        "checks": [],
                        "note": "실행 기록이 없어 재채점하지 않았습니다.",
                    },
                }
            )
            continue
        case_id = saved["id"]
        turn_index = int(saved.get("turn", 1)) - 1
        turns = current_turns.get(case_id, [])
        if turn_index < 0 or turn_index >= len(turns):
            rescored = {
                "passed": False,
                "checks": [
                    {
                        "check": "case_available",
                        "passed": False,
                        "expected": f"{case_id} turn {turn_index + 1} exists in current rubric",
                    }
                ],
            }
        else:
            run = _run_from_dict(saved.get("run") or {}, case_id)
            rescored = score(turns[turn_index], run)
        rescored_rows.append(
            {
                **saved,
                "passed": rescored["passed"],
                "checks": rescored["checks"],
                "recorded": recorded,
                "rescored": {"passed": rescored["passed"], "checks": rescored["checks"]},
            }
        )
    return rescored_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", type=Path, default=Path("eval/golden/agent_questions.yaml"))
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--rescore", type=Path, help="re-score saved traces without calling Foundry")
    parser.add_argument("--out", type=Path, help="JSON results path (default eval/results/<timestamp>.json)")
    parser.add_argument("--repeat", type=int, default=1, help="independent runs, not automatic retries")
    parser.add_argument(
        "--pace-seconds",
        "--pause-seconds",
        dest="pace_seconds",
        type=float,
        default=0,
        help="pause between turns; --pause-seconds is an alias",
    )
    parser.add_argument(
        "--error-cooldown-seconds",
        type=float,
        default=0,
        help="pause after a runtime error; preserves the failed result",
    )
    parser.add_argument("--allow-mutation", action="store_true", help="allow cases marked mutates: true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="verify Azure credentials and Atlas health before contacting Foundry",
    )
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
    if args.pace_seconds < 0 or args.error_cooldown_seconds < 0:
        parser.error("pauses must be nonnegative")
    if args.rescore and (args.preflight or args.list):
        parser.error("--rescore cannot be combined with --preflight or --list")
    if args.preflight and args.list:
        parser.error("--preflight cannot be combined with --list")
    if args.rescore:
        try:
            source_document = json.loads(args.rescore.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"could not read saved results: {exc}")
        if args.only:
            wanted = {item.strip() for item in args.only.split(",")}
            saved_results = (
                source_document.get("results", []) if isinstance(source_document, dict) else source_document
            )
            source_document = [result for result in saved_results if result.get("id") in wanted]
        rescored = rescore_saved_results(source_document, defaults, cases)
        out = args.out or args.rescore.with_name(f"{args.rescore.stem}.rescored.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        envelope = _experiment_envelope(
            args.cases,
            defaults,
            cases,
            None,
            repeat=args.repeat,
            pace_seconds=args.pace_seconds,
            error_cooldown_seconds=args.error_cooldown_seconds,
            started_at=datetime.now(UTC).isoformat(),
        )
        envelope["mode"] = "rescore"
        envelope["experiment"]["source_results"] = args.rescore.name
        if isinstance(source_document, dict) and source_document.get("experiment"):
            envelope["recorded_experiment"] = source_document["experiment"]
        envelope["experiment"]["ended_at"] = datetime.now(UTC).isoformat()
        envelope["results"] = rescored
        out.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        print(summarize(rescored))
        print(f"재채점 결과 파일: {out}")
        return 1 if any(r.get("passed") is False for r in rescored) else 0
    if args.list:
        mutating = sum(_is_mutating(case) for case in cases)
        print(
            f"cases={len(cases)} "
            f"turns={sum(1 + len(c.get('turns', [])) for c in cases)} "
            f"unique_utterances={len({t['question'] for c in cases for t in [c, *c.get('turns', [])]})} "
            f"mutating_cases={mutating} repeat={args.repeat}"
        )
        return 0

    if args.preflight:
        try:
            print(f"Preflight: {preflight()}")
        except RuntimeError as exc:
            print(f"Preflight 실패: {exc}", file=sys.stderr)
            return 2

    from cad2anything.services.app import foundry_runtime_factory
    from cad2anything.services.tools import Toolkit, workspace_from_env

    started_at = datetime.now(UTC).isoformat()
    runtime = foundry_runtime_factory(Toolkit(workspace_from_env()))
    runtime.capture_tool_outputs = True
    out = args.out or Path("eval/results") / f"agent-eval-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    completed: list[dict[str, Any]] = []
    envelope = _experiment_envelope(
        args.cases,
        defaults,
        cases,
        runtime,
        repeat=args.repeat,
        pace_seconds=args.pace_seconds,
        error_cooldown_seconds=args.error_cooldown_seconds,
        started_at=started_at,
    )

    def save_result(result: dict[str, Any]) -> None:
        completed.append(result)
        envelope["experiment"]["ended_at"] = datetime.now(UTC).isoformat()
        envelope["results"] = completed
        out.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
        run = result.get("run") or {}
        status = "건너뜀" if result.get("skipped") else ("통과" if result["passed"] else "실패")
        print(
            f"{status} {result['id']} "
            f"(run={result['attempt']} turn={result['turn']}, {run.get('seconds', 0)}s)",
            flush=True,
        )

    results = run_cases(
        runtime,
        cases,
        defaults,
        on_result=save_result,
        repeat=args.repeat,
        pause_seconds=args.pace_seconds,
        error_cooldown_seconds=args.error_cooldown_seconds,
        allow_mutation=args.allow_mutation,
    )
    envelope["experiment"]["ended_at"] = datetime.now(UTC).isoformat()
    envelope["results"] = results
    out.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summarize(results))
    print(f"결과 파일: {out}")
    return 1 if any(r.get("passed") is False for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
