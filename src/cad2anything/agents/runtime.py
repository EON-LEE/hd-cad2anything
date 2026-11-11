"""Run the Atlas orchestrator on Foundry and stream AG-UI compatible events.

MCP tools, web search, and code interpreter run inside Foundry Agent Service. Function tools (UI actions
and specialist consultation) are executed here and returned as ``function_call_output`` items in the same
conversation. With ``stream=True`` the answer text and server-side tool calls are forwarded as they are
generated, so the chat shows progress and types out the answer like a chat assistant.

The chat's analysis panel shows what the agent is doing: short messages the agent writes right before a
tool call (its plan, or why it changes approach) become ``THINKING_TEXT_MESSAGE_*`` notes instead of answer
text, and every tool call carries a Korean ``method`` (how it computes) and, when done, a ``result``.

A run ends honestly. A progress note is never shown as the answer: when the agent writes no answer (after
tool failures, too many rounds, or the turn budget), the answer is a deterministic Korean list of the steps
that succeeded and failed, marked as incomplete (``RUN_FINISHED.outcome``). A response stream that breaks
after the run produced tool results is resumed once with those results instead of running the tools again.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from cad2anything.agents import ORCHESTRATOR, SPECIALISTS
from cad2anything.agents.labels import tool_label
from cad2anything.agents.progress import parse_tool_output, specialist_result, tool_method, tool_result
from cad2anything.agents.ui_actions import UiActionHandler, screen_state

logger = logging.getLogger(__name__)
# One structured line per run: ids, outcome, timing and counts (never prompts, answers or secrets).
run_logger = logging.getLogger("cad2anything.agents")

MAX_ROUNDS = 10
SPECIALIST_OUTPUT_LIMIT = 6000
# A specialist's sources are kept whole up to this length; its body is shortened instead.
SOURCES_LIMIT = 1500
TEXT_CHUNK = 80
# Text the agent wrote before UI actions only is held until the next response: if the agent writes no more,
# it is the answer; if it writes more, a long or multi-line text starts the answer and a short one is a note.
NOTE_MAX_CHARS = 200
# Token-rate limits (429) are retried with the SDK's backoff, which honors retry-after headers.
CLIENT_MAX_RETRIES = 6
CLIENT_TIMEOUT_S = 180
# The SDK does not retry a stream that breaks mid-way. A specialist is retried once on transient failures
# (connection, timeout, 5xx, a broken stream), then the orchestrator answers without it. An orchestrator
# response that breaks after the run produced tool results is resumed once.
CONSULT_ATTEMPTS = 2
RESUME_ATTEMPTS = 1
# No round, resumption or consultation starts after this many seconds (AGENT_TURN_BUDGET_S overrides it, 0
# disables it). Keep it below the API layer's turn deadline so a long turn ends with an honest partial answer.
TURN_BUDGET_S = 200.0
TURN_BUDGET_ENV = "AGENT_TURN_BUDGET_S"
# Worst case of one turn: requests start only within the turn budget, so the turn ends within the budget plus
# one request — up to (CLIENT_MAX_RETRIES + 1) x CLIENT_TIMEOUT_S before its stream starts, then reads that
# each wait at most CLIENT_TIMEOUT_S. A turn sends at most MAX_ROUNDS + RESUME_ATTEMPTS orchestrator
# requests and CONSULT_ATTEMPTS requests per distinct consultation. With `cancel` (set by the API layer on
# disconnect or at its deadline) the run stops at the next streamed event or step instead.
CONSULT_FAILED = (
    "전문 에이전트의 결과를 받지 못했습니다({error}). 같은 요청을 다시 보내지 마세요. "
    "이미 조회한 도구 결과로 답하고, 이번에는 이 전문 작업(예: 웹 조사)을 하지 못했다고 한 문장으로 밝히세요."
)
CONSULT_RATE_LIMITED = (
    "AI 서비스 요청 한도에 걸려 전문 에이전트의 결과를 받지 못했습니다. 같은 요청을 다시 보내지 마세요. "
    "이미 조회한 도구 결과로 답하고, 요청 한도 때문에 이 전문 작업을 하지 못했다고 한 문장으로 밝히세요."
)
CONSULT_SKIPPED = (
    "답변 시간 제한 때문에 전문 에이전트를 부르지 않았습니다. 같은 요청을 다시 보내지 말고 "
    "이미 조회한 도구 결과로 바로 답하세요."
)
# Specialist requests shorter than this (for example "아니오") carry no task and are rejected unsent.
MIN_CONSULT_REQUEST = 10
CONSULT_REQUEST_ERROR = (
    "request가 비어 있거나 너무 짧아 전문 에이전트를 호출하지 않았습니다. 개정, 층, 대상, 원하는 결과를 담은 "
    "작업 지시 문장을 쓰세요. 단순 조회는 MCP 도구로 직접 처리하세요."
)
FALLBACK_ANSWER = "답변을 만들지 못했어요. 질문을 조금 바꿔서 다시 물어봐 주세요."
# Deterministic answers when the agent wrote none (RUN_FINISHED.outcome), always marked as incomplete.
INCOMPLETE_ANSWERS = {
    "no_answer": "답변을 끝까지 만들지 못했어요(미완료).",
    "tool_failed": "일부 단계가 실패해 답변을 끝까지 만들지 못했어요(미완료).",
    "rounds_exhausted": "분석 단계가 너무 길어져 답변을 끝내지 못하고 멈췄어요(미완료).",
    "time_exhausted": "시간이 오래 걸려 답변을 끝내지 못하고 멈췄어요(미완료).",
}
INCOMPLETE_HINT = (
    "완료한 단계의 결과는 분석 과정에서 볼 수 있어요. 질문을 조금 바꾸거나 나눠서 다시 물어봐 주세요."
)
MAX_LISTED_STEPS = 6
CANCELLED = "요청이 취소됐어요."
RATE_LIMITED = "AI 서비스의 요청 한도에 도달했어요. 잠시 후 다시 시도해 주세요."
RUN_FAILED = "답변을 완료하지 못했어요. 잠시 후 다시 시도해 주세요."
RESUME_NOTE = "응답 연결이 끊겨, 이미 받은 결과로 답을 이어서 만들어요."
UNFINISHED_SUMMARY = "응답이 끊겨 결과를 받지 못함"
UNFINISHED_RESULT = "연결이 끊겨 결과를 받지 못했어요"
RESUME_OUTPUT_LIMIT = 1500
RESUME_TOTAL_LIMIT = 6000
# Tool calls that Foundry runs server-side; the value names tools whose items carry no `name`.
HOSTED_TOOL_ITEMS = {
    "mcp_call": None,
    "web_search_call": "web_search",
    "code_interpreter_call": "code_interpreter",
}
STREAM_FAILURES = ("response.failed", "response.incomplete", "error")
# MCP tools that change the workspace; a resumed response is told not to call them again.
MUTATING_TOOLS = frozenset(
    {
        "revision_process",
        "revision_create_hypothetical",
        "registry_run",
        "render_adhoc_table",
        "layout_generate_alternatives",
        "relayout_plan",
        "relayout_select",
        "relayout_phasing",
        "relayout_report",
    }
)
# Error codes of failed responses that a new request may not hit again.
TRANSIENT_CODES = frozenset(
    {
        "server_error",
        "internal_error",
        "internal_server_error",
        "service_unavailable",
        "timeout",
        "stream_ended",
    }
)
# Exception classes (openai, httpx/httpx2) of connections that broke or timed out.
TRANSPORT_ERRORS = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "TransportError",
        "NetworkError",
        "TimeoutException",
        "ProtocolError",
        "RemoteProtocolError",
        "ReadError",
        "WriteError",
        "ConnectError",
        "ReadTimeout",
        "WriteTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "IncompleteRead",
        "ChunkedEncodingError",
    }
)
# Pins agent versions: a JSON object of agent name -> version, as printed by `cad2anything.agents.deploy`.
AGENT_VERSIONS_ENV = "FOUNDRY_AGENT_VERSIONS"
SOURCE_HEADING = re.compile(
    r"^[ \t]*(?:[#>*\-]+[ \t]*)?(?:\*\*)?[ \t]*(?:출처|참고[ \t]*자료|참고[ \t]*문헌|참고[ \t]*링크|sources?|references?)\b",
    re.IGNORECASE | re.MULTILINE,
)
TRUNCATED = "\n…(길이 제한으로 본문 뒷부분을 생략했어요)"


class AgentStreamError(RuntimeError):
    """A model response that failed, stopped early, or whose stream ended before it completed."""

    def __init__(
        self, message: str, *, transient: bool, code: str | None = None, response_id: str | None = None
    ):
        super().__init__(message)
        self.transient = transient
        self.code = code
        self.response_id = response_id


class RunCancelled(Exception):
    """The caller set the run's cancel event."""


def failure_kind(exc: BaseException | None) -> str:
    """Classify a failed request: "rate_limit", "auth", "transient" (connection, timeout, 5xx, a broken
    stream), "rejected" (other 4xx, a failed response) or "other" (anything else, such as a bug)."""
    if exc is None:
        return "other"
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", None) or "").lower()
    names = {cls.__name__ for cls in type(exc).__mro__}
    if status == 429 or "RateLimitError" in names or "rate_limit" in code or "rate limit" in str(exc).lower():
        return "rate_limit"
    if isinstance(exc, AgentStreamError):
        return "transient" if exc.transient else "rejected"
    if isinstance(status, int):
        if status in (401, 403):
            return "auth"
        return "transient" if status == 408 or status >= 500 else "rejected"
    if names & TRANSPORT_ERRORS or isinstance(exc, ConnectionError | TimeoutError):
        return "transient"
    return "other"


def agent_reference(name: str, version: str | None = None) -> dict[str, Any]:
    reference = {"name": name, "type": "agent_reference"}
    if version:
        reference["version"] = str(version)
    return {"agent_reference": reference}


def agent_versions_from_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Agent versions to pin, from ``FOUNDRY_AGENT_VERSIONS`` (empty when unset: the latest versions are used)."""
    raw = (os.environ if environ is None else environ).get(AGENT_VERSIONS_ENV, "").strip()
    if not raw:
        return {}
    error = f'{AGENT_VERSIONS_ENV} must be a JSON object of agent name to version, e.g. {{"atlas-orchestrator": "3"}}'
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(error) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(v, str | int) and not isinstance(v, bool) and str(v).strip() for v in parsed.values()
    ):
        raise ValueError(error)
    return {str(name): str(version).strip() for name, version in parsed.items()}


def turn_budget_from_env(environ: Mapping[str, str] | None = None) -> float | None:
    """The turn budget in seconds from ``AGENT_TURN_BUDGET_S`` (None when it is 0 or negative: no budget)."""
    raw = (os.environ if environ is None else environ).get(TURN_BUDGET_ENV, "").strip()
    if not raw:
        return TURN_BUDGET_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        logger.warning("invalid %s; using %ss", TURN_BUDGET_ENV, TURN_BUDGET_S)
        return TURN_BUDGET_S
    return value if value > 0 else None


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _message_text(item: Any) -> str:
    content = _get(item, "content", []) or []
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if _get(part, "type") in ("output_text", "text", "input_text"):
            parts.append(_get(part, "text", "") or "")
    return "".join(parts)


def _message_sources(item: Any) -> list[tuple[str, str]]:
    """(title, url) of the web citations (``url_citation`` annotations) in a message."""
    sources = []
    content = _get(item, "content", []) or []
    for part in [] if isinstance(content, str) else content:
        for annotation in _get(part, "annotations", []) or []:
            url = _get(annotation, "url")
            if _get(annotation, "type") == "url_citation" and url:
                sources.append((str(_get(annotation, "title") or "").strip(), str(url)))
    return sources


def with_sources(text: str, sources: Iterable[tuple[str, str]], limit: int = SPECIALIST_OUTPUT_LIMIT) -> str:
    """A specialist answer for the orchestrator: the body, shortened (and marked) when it is too long, then its
    sources whole — the trailing source section the specialist wrote and the web citations it does not list."""
    body, tail = text.strip(), ""
    for match in reversed(list(SOURCE_HEADING.finditer(body))):
        if "http" in body[match.start() :]:
            body, tail = body[: match.start()].rstrip(), body[match.start() :].strip()
            break
    links, seen = [], set()
    for title, url in sources:
        if url in seen or url in tail:
            continue
        seen.add(url)
        links.append(f"- {title} — {url}" if title and title != url else f"- {url}")
    block = "\n".join([tail or "출처:", *links]) if links else tail
    if len(block) > SOURCES_LIMIT:
        lines, kept, size = block.splitlines(), [], 0
        for line in lines:
            if size + len(line) + 1 > SOURCES_LIMIT - 40:
                break
            kept.append(line)
            size += len(line) + 1
        block = "\n".join([*kept, f"- …(출처 {len(lines) - len(kept)}줄 생략)"])
    room = limit - (len(block) + 2 if block else 0)
    if len(body) > room:
        cut = body[: max(0, room - len(TRUNCATED))]
        boundary = max(cut.rfind("\n"), cut.rfind(". ") + 1)
        if boundary > len(cut) // 2:
            cut = cut[:boundary]
        body = cut.rstrip() + TRUNCATED
    return f"{body}\n\n{block}" if block and body else body or block


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _output_ok(output: str) -> bool:
    parsed = parse_tool_output(output)
    return not (isinstance(parsed, dict) and parsed.get("ok") is False)


def hosted_tool_call(item: Any) -> dict[str, Any] | None:
    """Describe a server-side tool call item (MCP, web search, code interpreter), else None."""
    item_type = _get(item, "type")
    if item_type not in HOSTED_TOOL_ITEMS:
        return None
    if item_type == "web_search_call":
        query = _get(_get(item, "action"), "query")
        args: dict[str, Any] = {"query": query} if query else {}
    elif item_type == "code_interpreter_call":
        args = {"code": _short(_get(item, "code", "") or "", 500)}
    else:
        args = _safe_json(_get(item, "arguments"))
    error = _get(item, "error")
    output = _get(item, "output", "") or ""
    summary = f"오류: {_short(error)}" if error else _short(output or _get(item, "status", "") or "")
    return {
        "id": _get(item, "id"),
        "tool_name": _get(item, "name") or HOSTED_TOOL_ITEMS[item_type],
        "args": args,
        "summary": summary,
        "output": output,
        # Tools report recoverable errors as an `ok: false` result; the agent then retries.
        "error": bool(error) or not _output_ok(output),
    }


def _stream_failure(event: Any) -> AgentStreamError:
    """The error for a ``response.failed`` / ``response.incomplete`` / ``error`` stream event."""
    event_type = _get(event, "type")
    response = _get(event, "response")
    error = _get(response, "error") if response is not None else None
    code = _get(event, "code") or _get(error, "code")
    if event_type == "response.incomplete":
        code = code or _get(_get(response, "incomplete_details"), "reason")
    detail = _get(event, "message") or _get(error, "message") or code or event_type
    transient = event_type != "response.incomplete" and str(code or "").lower() in TRANSIENT_CODES
    return AgentStreamError(
        f"agent response failed: {_short(detail, 300)}",
        transient=transient,
        code=str(code) if code else None,
        response_id=_get(response, "id"),
    )


class _TextRouter:
    """Routes one response's message text to progress notes or to the answer.

    A message the agent wrote before calling a tool is a progress note; otherwise it is the answer. Messages
    the model labels (``phase``: "commentary" or "final_answer") follow the label, so a final answer streams
    as it is generated. Unlabelled messages are held until the next output item decides: a server-side tool
    makes them notes, the end of the response makes them the answer, and function calls make them notes
    unless every call is a UI action. Text before UI actions only becomes ``tentative``: the runtime keeps it
    until the next response shows whether the agent writes more (see ``AgentRuntime._emit_text``).

    ``feed`` returns ``(kind, text)`` pairs: "note", "answer_start" (the first text of an answer message) or
    "answer" (more of the same message).
    """

    def __init__(self) -> None:
        self._held: list[str] = []
        self._text: list[str] | None = None
        self._phase: str | None = None
        self._live = False
        self._calls: list[str] = []
        self.tentative: str | None = None
        self.saw_message = False

    def feed(self, kind: str, payload: Any) -> list[tuple[str, str]]:
        if kind == "message":
            return self._end_message() + self._start(payload)
        if kind == "text":
            return self._delta(payload)
        if kind == "message_done":
            return self._end_message(payload)
        if kind in ("tool_start", "tool_end"):
            return self._end_message() + self._release("note")
        if kind == "call":
            actions = self._end_message()
            self._calls.append(str(_get(payload, "name") or ""))
            return actions
        if kind == "done":
            return self._done(payload)
        return []

    def _start(self, item: Any) -> list[tuple[str, str]]:
        self.saw_message = True
        self._text, self._phase = [], _get(item, "phase")
        if self._phase == "final_answer":
            self._live = True
            return self._release("answer")
        return []

    def _delta(self, delta: str) -> list[tuple[str, str]]:
        if not delta:
            return []
        if self._text is None:  # text without a message start
            self.saw_message = True
            self._text, self._phase = [], None
        self._text.append(delta)
        if self._live:
            return [("answer_start" if len(self._text) == 1 else "answer", delta)]
        return []

    def _end_message(self, item: Any = None) -> list[tuple[str, str]]:
        if self._text is None:
            return []
        streamed = "".join(self._text)
        text = streamed or (_message_text(item) if item is not None else "")
        phase = self._phase or (_get(item, "phase") if item is not None else None)
        live = self._live
        self._text, self._phase, self._live = None, None, False
        if not text.strip() or (live and streamed):
            return []  # nothing to say, or a final answer that already streamed
        if live:
            return [("answer_start", text.strip())]
        if phase == "commentary":
            return [("note", text.strip())]
        self._held.append(text)
        return self._release("answer") if phase == "final_answer" else []

    def _release(self, kind: str) -> list[tuple[str, str]]:
        texts = [text.strip() for text in self._held if text.strip()]
        self._held = []
        if not texts:
            return []
        if kind == "note":
            return [("note", text) for text in texts]
        return [("answer_start", "\n\n".join(texts))]

    def _done(self, response: Any) -> list[tuple[str, str]]:
        actions = self._end_message()
        if self._calls:
            if self._held and all(name.startswith("ui_") for name in self._calls):
                texts = [text.strip() for text in self._held if text.strip()]
                self._held = []
                self.tentative = "\n\n".join(texts) or None
                return actions
            return actions + self._release("note")
        actions += self._release("answer")
        if not self.saw_message:
            text = (_get(response, "output_text", "") or "").strip()
            if text:
                actions.append(("answer_start", text))
        return actions


@dataclass
class _Step:
    """A top-level tool call that finished, for the incomplete-answer summary and the run log."""

    label: str
    ok: bool


@dataclass
class _Attempt:
    """One response attempt: the server-side tool calls it started, and whether answer text left."""

    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    started: set[str] = field(default_factory=set)
    answered: bool = False


@dataclass
class _Run:
    run_id: str
    started: float
    cancel: threading.Event | None = None
    thread_id: str | None = None
    rounds: int = 0
    resumed: int = 0
    consults: int = 0
    steps: list[_Step] = field(default_factory=list)
    consulted: dict[tuple[str, str], str] = field(default_factory=dict)
    message_id: str | None = None
    wrote_text: bool = False
    # Text the agent wrote before UI actions only (see _TextRouter), and whether those actions succeeded.
    pending: str | None = None
    pending_ok: bool = True
    # Why the rounds stopped before the agent was done: "rounds_exhausted" or "time_exhausted".
    stopped: str | None = None
    logged: bool = False


def _listed(steps: Iterable[_Step]) -> str:
    labels = list(dict.fromkeys(step.label for step in steps))
    shown = ", ".join(labels[:MAX_LISTED_STEPS])
    return shown + (f" 외 {len(labels) - MAX_LISTED_STEPS}개" if len(labels) > MAX_LISTED_STEPS else "")


def incomplete_answer(outcome: str, steps: list[_Step]) -> str:
    """The deterministic answer of a run that ended without one: what was done and what failed."""
    lines = [INCOMPLETE_ANSWERS.get(outcome, INCOMPLETE_ANSWERS["no_answer"])]
    if done := _listed(step for step in steps if step.ok):
        lines.append(f"- 완료한 단계: {done}")
    if failed := _listed(step for step in steps if not step.ok):
        lines.append(f"- 실패한 단계: {failed}")
    lines.append(INCOMPLETE_HINT if steps else FALLBACK_ANSWER)
    return "\n".join(lines)


def _input_items(request: Any) -> list[Any]:
    return [{"role": "user", "content": request}] if isinstance(request, str) else list(request or [])


def _resume_message(attempt: _Attempt) -> str:
    """The note that resumes a broken response: results to reuse, and tools not to call again."""
    finished = [call for call in attempt.calls.values() if call.get("finished")]
    unfinished = [call for call in attempt.calls.values() if not call.get("finished")]
    lines = ["[연결 재개] 앞 응답이 중간에 끊겼어요. 앞에서 받은 도구 결과로 이어서 답해 주세요."]
    room = RESUME_TOTAL_LIMIT
    if finished:
        lines.append("아래 도구는 이미 실행했으니 다시 호출하지 말고 이 결과를 쓰세요.")
    for call in finished:
        line = f"- {call['name']}({_short(call['args'], 200)}) 결과: {_short(call.get('output') or '', RESUME_OUTPUT_LIMIT)}"
        if len(line) > room:
            line = f"- {call['name']}: 결과가 길어 생략했어요. 같은 도구를 다시 호출하지 마세요."
        room -= len(line)
        lines.append(line)
    risky = sorted({call["name"] for call in unfinished if call["name"] in MUTATING_TOOLS})
    others = sorted({call["name"] for call in unfinished if call["name"] not in MUTATING_TOOLS})
    if risky:
        lines.append(
            f"결과를 받지 못한 {', '.join(risky)}는 이미 실행됐을 수 있어요. 데이터를 바꾸는 도구이니 다시 호출하지 마세요."
        )
    if others:
        lines.append(f"결과를 받지 못한 조회 도구({', '.join(others)})는 필요하면 다시 호출해도 돼요.")
    lines.append("필요한 조회와 화면 표시는 해도 되고, 마지막에 최종 답변을 써 주세요.")
    return "\n".join(lines)


class AgentRuntime:
    """Bridges Atlas chat requests to Foundry agents.

    ``agent_versions`` pins agent versions (name -> version, see ``FOUNDRY_AGENT_VERSIONS``); unpinned agents
    use their latest version. ``turn_budget_s`` stops starting new rounds and consultations after that many
    seconds (None: no budget)."""

    def __init__(
        self,
        openai_client: Any,
        ui_handler: UiActionHandler | None = None,
        orchestrator: str = ORCHESTRATOR,
        specialists: dict[str, str] | None = None,
        id_factory: Callable[[], str] | None = None,
        stream: bool = False,
        capture_tool_outputs: bool = False,
        agent_versions: Mapping[str, str] | None = None,
        turn_budget_s: float | None = TURN_BUDGET_S,
    ):
        self.client = openai_client
        self.ui_handler = ui_handler or UiActionHandler()
        self.orchestrator = orchestrator
        self.specialists = specialists or SPECIALISTS
        self.stream = stream
        self.capture_tool_outputs = capture_tool_outputs
        self.agent_versions = {str(name): str(version) for name, version in (agent_versions or {}).items()}
        self.turn_budget_s = turn_budget_s
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_project(cls, project_endpoint: str, credential: Any, **kwargs: Any) -> AgentRuntime:
        from azure.ai.projects import AIProjectClient

        project = AIProjectClient(endpoint=project_endpoint, credential=credential)
        client = project.get_openai_client().with_options(
            max_retries=CLIENT_MAX_RETRIES, timeout=CLIENT_TIMEOUT_S
        )
        kwargs.setdefault("stream", True)
        kwargs.setdefault("agent_versions", agent_versions_from_env())
        kwargs.setdefault("turn_budget_s", turn_budget_from_env())
        return cls(client, **kwargs)

    def _reference(self, agent: str) -> dict[str, Any]:
        return agent_reference(agent, self.agent_versions.get(agent))

    def _agent_meta(self, agent: str) -> dict[str, Any]:
        version = self.agent_versions.get(agent)
        return {"agent": agent, **({"agent_version": version} if version else {})}

    def _over_budget(self, run: _Run) -> bool:
        return self.turn_budget_s is not None and time.monotonic() - run.started > self.turn_budget_s

    @staticmethod
    def _check_cancel(run: _Run | None) -> None:
        if run is not None and run.cancel is not None and run.cancel.is_set():
            raise RunCancelled()

    # One agent turn ----------------------------------------------------------------------------------
    def _respond(self, conversation_id: str, request: Any, agent: str) -> Iterator[tuple[str, Any]]:
        """Run one agent turn as events in output order: ("message", item) when a message starts, ("text",
        delta), ("message_done", item) when it ends, ("tool_start" | "tool_end", item) for server-side tools,
        ("call", item) when a function call is added, then ("done", response).

        A failed or unfinished response raises ``AgentStreamError``. The stream is closed when the consumer
        stops early."""
        kwargs = {"conversation": conversation_id, "input": request, "extra_body": self._reference(agent)}
        if not self.stream:
            response = self.client.responses.create(**kwargs)
            for item in _get(response, "output", []) or []:
                item_type = _get(item, "type")
                if item_type in HOSTED_TOOL_ITEMS:
                    yield "tool_start", item
                    yield "tool_end", item
                elif item_type == "message":
                    yield "message", item
                    if text := _message_text(item):
                        yield "text", text
                    yield "message_done", item
                elif item_type == "function_call":
                    yield "call", item
            yield "done", response
            return
        stream = self.client.responses.create(**kwargs, stream=True)
        final = None
        try:
            for event in stream:
                event_type = _get(event, "type")
                if event_type == "response.output_text.delta":
                    if delta := _get(event, "delta", ""):
                        yield "text", delta
                elif event_type in ("response.output_item.added", "response.output_item.done"):
                    item = _get(event, "item")
                    item_type = _get(item, "type")
                    added = event_type.endswith("added")
                    if item_type in HOSTED_TOOL_ITEMS:
                        yield ("tool_start" if added else "tool_end"), item
                    elif item_type == "message":
                        yield ("message" if added else "message_done"), item
                    elif item_type == "function_call" and added:
                        yield "call", item
                elif event_type == "response.completed":
                    final = _get(event, "response")
                elif event_type in STREAM_FAILURES:
                    raise _stream_failure(event)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        if final is None:
            raise AgentStreamError(
                "agent stream ended without a completed response", transient=True, code="stream_ended"
            )
        yield "done", final

    def _note_events(
        self, text: str, parent_id: str | None = None, agent: str | None = None
    ) -> list[dict[str, Any]]:
        """A progress note as an AG-UI thinking message (nested under a specialist call when ``parent_id``)."""
        note_id = self._new_id()
        nested = {"parent_tool_call_id": parent_id, "agent": agent} if parent_id else {}
        return [
            {"type": "THINKING_TEXT_MESSAGE_START", "message_id": note_id, **nested},
            {"type": "THINKING_TEXT_MESSAGE_CONTENT", "message_id": note_id, "delta": text},
            {"type": "THINKING_TEXT_MESSAGE_END", "message_id": note_id},
        ]

    def _hosted_events(
        self,
        kind: str,
        item: Any,
        started: set[str],
        parent_id: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        call = hosted_tool_call(item)
        if call is None or (kind == "tool_start" and not call["id"]):
            return []  # without an id the start cannot be matched; it is reported on completion
        call_id = call["id"] or self._new_id()
        nested = {"parent_tool_call_id": parent_id, "agent": agent} if parent_id else {}
        label = tool_label(call["tool_name"], call["args"])
        method = tool_method(call["tool_name"], call["args"])
        events: list[dict[str, Any]] = []
        if call_id not in started:
            started.add(call_id)
            events.append(
                {
                    "type": "TOOL_CALL_START",
                    "tool_call_id": call_id,
                    "tool_name": call["tool_name"],
                    "args": call["args"],
                    "label": label,
                    "method": method,
                    **nested,
                }
            )
        if kind == "tool_end":
            events.append(
                {
                    "type": "TOOL_CALL_END",
                    "tool_call_id": call_id,
                    "summary": call["summary"],
                    "label": label,
                    "method": method,
                    "result": tool_result(call["tool_name"], call["args"], call["output"]),
                    "ok": not call["error"],
                    **(
                        {"output": parse_tool_output(call["output"]), "args": call["args"]}
                        if self.capture_tool_outputs
                        else {}
                    ),
                }
            )
        return events

    def _tracked_hosted(
        self,
        kind: str,
        item: Any,
        attempt: _Attempt,
        run: _Run | None = None,
        parent_id: str | None = None,
        agent: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """``_hosted_events`` that records the calls of ``attempt`` (and, with ``run``, the top-level steps)."""
        call = hosted_tool_call(item)
        for event in self._hosted_events(kind, item, attempt.started, parent_id=parent_id, agent=agent):
            record = attempt.calls.setdefault(
                event["tool_call_id"],
                {"name": call["tool_name"], "args": call["args"], "label": event["label"], "finished": False},
            )
            if event["type"] == "TOOL_CALL_END":
                record.update(finished=True, output=call["output"])
                if run is not None:
                    run.steps.append(_Step(event["label"], event["ok"]))
            yield event

    def _close_unfinished(self, attempt: _Attempt, run: _Run | None = None) -> Iterator[dict[str, Any]]:
        """End the tool calls a broken response started as failed, so no step stays "running"."""
        for call_id, call in attempt.calls.items():
            if call["finished"] or call.get("closed"):
                continue
            call["closed"] = True
            if run is not None:
                run.steps.append(_Step(call["label"], False))
            yield {
                "type": "TOOL_CALL_END",
                "tool_call_id": call_id,
                "summary": UNFINISHED_SUMMARY,
                "label": call["label"],
                "method": tool_method(call["name"], call["args"]),
                "result": UNFINISHED_RESULT,
                "ok": False,
            }

    # Specialists -------------------------------------------------------------------------------------
    def _consult(
        self, agent_key: str, request: str, parent_id: str, run: _Run | None = None
    ) -> Generator[dict[str, Any], None, tuple[str, int]]:
        """Run a specialist agent, streaming its tool calls under ``parent_id``; return (answer, calls).

        Transient failures (connection, timeout, 5xx, a broken stream) are retried once in a new conversation,
        after the tool calls of the failed attempt are ended as failed. Rate limits (already retried by the
        SDK), authorization and request errors are not retried. A failure becomes an ``ok: false`` result that
        tells the orchestrator to answer without the specialist. The answer keeps its sources (see
        ``with_sources``)."""
        name = self.specialists.get(agent_key)
        if name is None:
            return json.dumps(
                {"ok": False, "error": f"unknown specialist {agent_key}"}, ensure_ascii=False
            ), 0
        calls = 0
        error: Exception | None = None
        for number in range(1, CONSULT_ATTEMPTS + 1):
            if number > 1 and run is not None and self._over_budget(run):
                break
            texts: list[str] = []
            response = None
            attempt = _Attempt()
            router = _TextRouter()
            try:
                conversation = self.client.conversations.create()
                with closing(self._respond(_get(conversation, "id"), request, name)) as events:
                    for kind, payload in events:
                        self._check_cancel(run)
                        for action, text in router.feed(kind, payload):
                            if action == "note":
                                yield from self._note_events(text, parent_id=parent_id, agent=name)
                            else:
                                texts.append(text if action == "answer" or not texts else f"\n\n{text}")
                        if kind == "done":
                            response = payload
                        elif kind in ("tool_start", "tool_end"):
                            for event in self._tracked_hosted(
                                kind, payload, attempt, parent_id=parent_id, agent=name
                            ):
                                if event["type"] == "TOOL_CALL_END":
                                    calls += 1
                                yield event
            except RunCancelled:
                yield from self._close_unfinished(attempt)
                raise
            except Exception as exc:  # the orchestrator can still answer from its own tool results
                error = exc
                yield from self._close_unfinished(attempt)
                failure = failure_kind(exc)
                logger.warning(
                    "specialist %s failed (attempt %d/%d): %s, %s",
                    name,
                    number,
                    CONSULT_ATTEMPTS,
                    type(exc).__name__,
                    failure,
                )
                if failure != "transient":
                    break
                continue
            text = "".join(texts).strip() or (_get(response, "output_text", "") or "").strip()
            sources = [
                source
                for item in _get(response, "output", []) or []
                if _get(item, "type") == "message"
                for source in _message_sources(item)
            ]
            return with_sources(text, sources), calls
        if failure_kind(error) == "rate_limit":
            message = CONSULT_RATE_LIMITED
        else:
            message = CONSULT_FAILED.format(error=type(error).__name__)
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False), calls

    # Conversation ------------------------------------------------------------------------------------
    def run(
        self,
        message: str,
        ui_state: dict[str, Any],
        conversation_id: str | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Answer one chat message as AG-UI events.

        ``RUN_FINISHED.outcome`` is "answered" when the agent wrote the answer. Otherwise the answer lists the
        steps that succeeded and failed, ``incomplete`` is true and ``outcome`` says why: "no_answer",
        "tool_failed", "rounds_exhausted" or "time_exhausted". A failure yields ``RUN_ERROR`` and re-raises.

        ``cancel``: when the caller sets it (the client left, or the API deadline passed), the run stops at the
        next streamed event or step, yields one ``RUN_ERROR`` with ``code: "cancelled"`` and returns without
        raising; a caller that already ended its stream may drop that event.
        """
        if not isinstance(ui_state, dict):
            ui_state = {}
        run = _Run(run_id=self._new_id(), started=time.monotonic(), cancel=cancel, thread_id=conversation_id)
        try:
            self._check_cancel(run)
            if conversation_id is None:
                conversation_id = run.thread_id = _get(self.client.conversations.create(), "id")
            yield {
                "type": "RUN_STARTED",
                "thread_id": conversation_id,
                "run_id": run.run_id,
                **self._agent_meta(self.orchestrator),
            }
            request: Any = f"[ui_state] {json.dumps(screen_state(ui_state), ensure_ascii=False)}\n\n{message}"
            for index in range(MAX_ROUNDS):
                if index and self._over_budget(run):
                    run.stopped = "time_exhausted"
                    break
                run.rounds += 1
                calls = yield from self._round(run, conversation_id, request)
                if not calls:
                    break
                outputs = []
                for item in calls:
                    self._check_cancel(run)
                    outputs.append((yield from self._handle_function(item, ui_state, run)))
                request = outputs
            else:
                run.stopped = "rounds_exhausted"
            yield from self._finish(run)
        except RunCancelled:
            self._log_run(run, "cancelled")
            yield {"type": "RUN_ERROR", "message": CANCELLED, "code": "cancelled"}
        except Exception as exc:  # surfaced to the UI; details stay in server logs
            self._log_run(run, "error", exc)
            failure = failure_kind(exc)
            yield {
                "type": "RUN_ERROR",
                "message": RATE_LIMITED if failure == "rate_limit" else RUN_FAILED,
                "code": failure,
                "error_type": type(exc).__name__,
            }
            raise
        finally:
            self._log_run(run, "closed")  # the consumer stopped reading before the run ended

    def _round(
        self, run: _Run, conversation_id: str, request: Any
    ) -> Generator[dict[str, Any], None, list[Any]]:
        """One orchestrator response: streams its notes, answer text and server-side tool calls, and returns
        its function calls.

        A transient failure after the run produced tool results is resumed once (``RESUME_ATTEMPTS``): the new
        request carries this round's input when the conversation lacks it, plus a note with the results of
        the tools that finished, and asks the agent not to run those (or unfinished mutating tools) again.
        Function tools are never executed twice: their outputs are reused."""
        resumed = 0
        while True:
            attempt = _Attempt()
            try:
                return (yield from self._stream_round(run, conversation_id, request, attempt))
            except RunCancelled:
                yield from self._close_unfinished(attempt, run)
                raise
            except Exception as exc:
                produced = bool(run.steps)  # tool results of this run, before unfinished calls become steps
                yield from self._close_unfinished(attempt, run)
                if not (
                    resumed < RESUME_ATTEMPTS
                    and produced
                    and not attempt.answered
                    and failure_kind(exc) == "transient"
                    and not self._over_budget(run)
                ):
                    raise
                resumed += 1
                run.resumed += 1
                logger.warning(
                    "orchestrator response failed (%s); resuming with the tool results so far",
                    type(exc).__name__,
                )
                yield from self._note_events(RESUME_NOTE)
                request = self._resume_input(conversation_id, request, attempt)

    def _stream_round(
        self, run: _Run, conversation_id: str, request: Any, attempt: _Attempt
    ) -> Generator[dict[str, Any], None, list[Any]]:
        router = _TextRouter()
        response = None
        with closing(self._respond(conversation_id, request, self.orchestrator)) as events:
            for kind, payload in events:
                self._check_cancel(run)
                for action, text in router.feed(kind, payload):
                    yield from self._emit_text(run, attempt, action, text)
                if kind == "done":
                    response = payload
                elif kind in ("tool_start", "tool_end"):
                    yield from self._flush_pending(run, "note")
                    yield from self._tracked_hosted(kind, payload, attempt, run)
        if router.tentative:
            if run.pending is None:
                run.pending, run.pending_ok = router.tentative, True
            else:
                run.pending = f"{run.pending}\n\n{router.tentative}"
        return [item for item in _get(response, "output", []) or [] if _get(item, "type") == "function_call"]

    def _emit_text(self, run: _Run, attempt: _Attempt, action: str, text: str) -> Iterator[dict[str, Any]]:
        if action == "note":
            yield from self._flush_pending(run, "note")
            yield from self._note_events(text)
            return
        if action == "answer_start" and run.pending is not None:
            # The agent wrote more after its UI actions: long held text starts the answer, short text was a note.
            starts_answer = len(run.pending) > NOTE_MAX_CHARS or "\n" in run.pending.strip()
            yield from self._flush_pending(run, "answer" if starts_answer else "note")
        attempt.answered = True
        yield from self._answer_events(run, text, paragraph=action == "answer_start")

    def _flush_pending(self, run: _Run, kind: str) -> Iterator[dict[str, Any]]:
        text, run.pending = run.pending, None
        if text is None:
            return
        if kind == "note":
            yield from self._note_events(text)
        else:
            yield from self._answer_events(run, text, paragraph=True)

    def _answer_events(self, run: _Run, text: str, paragraph: bool) -> Iterator[dict[str, Any]]:
        if run.message_id is None:
            run.message_id = self._new_id()
            yield {"type": "TEXT_MESSAGE_START", "message_id": run.message_id, "role": "assistant"}
        if paragraph and run.wrote_text:
            yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": run.message_id, "delta": "\n\n"}
        run.wrote_text = True
        for start in range(0, len(text), TEXT_CHUNK):
            chunk = text[start : start + TEXT_CHUNK]
            yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": run.message_id, "delta": chunk}

    def _resume_input(self, conversation_id: str, request: Any, attempt: _Attempt) -> list[Any]:
        items = [] if self._conversation_has(conversation_id, request) else _input_items(request)
        return [*items, {"role": "user", "content": _resume_message(attempt)}]

    def _conversation_has(self, conversation_id: str, request: Any) -> bool:
        """Whether the conversation already holds this round's input (a broken response may have stored it)."""
        try:
            page = self.client.conversations.items.list(conversation_id, limit=20, order="desc")
            data = _get(page, "data")
            items = list(data) if data is not None else list(itertools.islice(page, 20))
        except Exception as exc:  # unknown: send the input again
            logger.info(
                "conversation items unavailable (%s); sending the round input again", type(exc).__name__
            )
            return False
        if isinstance(request, str):
            latest = next(
                (i for i in items if _get(i, "type") == "message" and _get(i, "role") == "user"), None
            )
            return latest is not None and _message_text(latest).strip() == request.strip()
        wanted = {_get(i, "call_id") for i in request if _get(i, "type") == "function_call_output"}
        seen = {_get(i, "call_id") for i in items if _get(i, "type") == "function_call_output"}
        return bool(wanted) and wanted <= seen

    def _finish(self, run: _Run) -> Iterator[dict[str, Any]]:
        if run.pending is not None:
            # Text written before the last UI actions is the answer when they worked and nothing followed.
            answer = not run.wrote_text and run.pending_ok and run.stopped is None
            yield from self._flush_pending(run, "answer" if answer else "note")
        outcome = "answered"
        if not run.wrote_text:
            outcome = run.stopped or (
                "tool_failed" if any(not step.ok for step in run.steps) else "no_answer"
            )
            yield from self._answer_events(run, incomplete_answer(outcome, run.steps), paragraph=True)
        yield {"type": "TEXT_MESSAGE_END", "message_id": run.message_id}
        self._log_run(run, outcome)
        finished = {
            "type": "RUN_FINISHED",
            "thread_id": run.thread_id,
            "run_id": run.run_id,
            "outcome": outcome,
        }
        if outcome != "answered":
            finished["incomplete"] = True
        yield finished

    def _log_run(self, run: _Run, outcome: str, exc: BaseException | None = None) -> None:
        if run.logged:
            return
        run.logged = True
        record: dict[str, Any] = {
            "thread_id": run.thread_id,
            "run_id": run.run_id,
            "outcome": outcome,
            "duration_ms": round((time.monotonic() - run.started) * 1000),
            "rounds": run.rounds,
            "tool_calls": len(run.steps),
            "failed_tool_calls": sum(1 for step in run.steps if not step.ok),
            "consults": run.consults,
            "resumed": run.resumed,
            "agent": self.orchestrator,
            "agent_version": self.agent_versions.get(self.orchestrator),
        }
        if exc is not None:
            record.update(
                error_type=type(exc).__name__,
                failure=failure_kind(exc),
                status_code=getattr(exc, "status_code", None),
                request_id=getattr(exc, "request_id", None) or getattr(exc, "response_id", None),
                error_code=getattr(exc, "code", None),
            )
        level = (
            logging.INFO
            if outcome == "answered"
            else logging.ERROR
            if outcome == "error"
            else logging.WARNING
        )
        run_logger.log(level, "agent_run %s", json.dumps(record, ensure_ascii=False, default=str))

    def _handle_function(
        self, item: Any, ui_state: dict[str, Any], run: _Run
    ) -> Generator[dict[str, Any], None, dict[str, Any]]:
        name = str(_get(item, "name") or "")
        call_id = _get(item, "call_id")
        args = _safe_json(_get(item, "arguments"))
        method = tool_method(name, args)
        label = tool_label(name, args)
        if not name.startswith("ui_"):
            yield from self._flush_pending(run, "note")  # the agent went on working: held text was a note
        yield {
            "type": "TOOL_CALL_START",
            "tool_call_id": call_id,
            "tool_name": name,
            "args": args,
            "label": label,
            "method": method,
        }
        if name == "consult_specialist":
            output, ok, summary, result = yield from self._consultation(args, call_id, run)
        else:
            handled = self.ui_handler.handle(name, args, ui_state)
            output = handled.output
            summary = _short(output, 120)
            ok = _output_ok(output)
            result = tool_result(name, args, output)
            if handled.patches:
                yield {"type": "STATE_DELTA", "delta": handled.patches}
            if run.pending is not None:
                run.pending_ok = run.pending_ok and ok
        run.steps.append(_Step(label, ok))
        yield {
            "type": "TOOL_CALL_END",
            "tool_call_id": call_id,
            "summary": summary,
            "method": method,
            "result": result,
            "ok": ok,
            **({"output": parse_tool_output(output)} if self.capture_tool_outputs else {}),
        }
        return {"type": "function_call_output", "call_id": call_id, "output": output}

    def _consultation(
        self, args: dict[str, Any], call_id: str, run: _Run
    ) -> Generator[dict[str, Any], None, tuple[str, bool, str, str | None]]:
        """A consult_specialist call: (output, ok, summary, result). Repeated requests reuse the first answer."""
        agent_key = str(args.get("agent", ""))
        request = str(args.get("request", "")).strip()
        key = (agent_key, request)
        if len(request) < MIN_CONSULT_REQUEST:
            output = json.dumps({"ok": False, "error": CONSULT_REQUEST_ERROR}, ensure_ascii=False)
            return output, False, "요청 내용이 없어 상담하지 않음", None
        if key in run.consulted:
            output = run.consulted[key]
            ok = _output_ok(output)
            summary = f"{agent_key} 같은 요청의 앞선 응답 재사용" + ("" if ok else "(실패한 응답)")
            return output, ok, summary, specialist_result(agent_key, ok)
        if self._over_budget(run):
            output = json.dumps({"ok": False, "error": CONSULT_SKIPPED}, ensure_ascii=False)
            return output, False, "시간 제한으로 상담하지 않음", specialist_result(agent_key, False)
        run.consults += 1
        output, calls = yield from self._consult(agent_key, request, call_id, run)
        run.consulted[key] = output
        ok = _output_ok(output)
        summary = (
            f"{agent_key} 응답 {len(output)}자, 도구 호출 {calls}회"
            if ok
            else f"{agent_key} 응답 실패, 도구 호출 {calls}회"
        )
        return output, ok, summary, specialist_result(agent_key, ok)
