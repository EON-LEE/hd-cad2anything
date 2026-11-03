"""Run the Atlas orchestrator on Foundry and stream AG-UI compatible events.

MCP tools, web search, and code interpreter run inside Foundry Agent Service. Function tools (UI actions
and specialist consultation) are executed here and returned as ``function_call_output`` items in the same
conversation. With ``stream=True`` the answer text and server-side tool calls are forwarded as they are
generated, so the chat shows progress and types out the answer like a chat assistant.

The chat's analysis panel shows what the agent is doing: short messages the agent writes right before a
tool call (its plan, or why it changes approach) become ``THINKING_TEXT_MESSAGE_*`` notes instead of answer
text, and every tool call carries a Korean ``method`` (how it computes) and, when done, a ``result``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Generator, Iterator
from typing import Any

from cad2anything.agents import ORCHESTRATOR, SPECIALISTS
from cad2anything.agents.labels import tool_label
from cad2anything.agents.progress import parse_tool_output, specialist_result, tool_method, tool_result
from cad2anything.agents.ui_actions import UiActionHandler

logger = logging.getLogger(__name__)

MAX_ROUNDS = 10
SPECIALIST_OUTPUT_LIMIT = 6000
TEXT_CHUNK = 80
# A message that is followed by a tool call in the same response is the agent's progress note (its plan or
# a change of approach), shown in the chat's analysis panel instead of the answer. Longer text, or text with
# a line break, is answer text and streams as it arrives.
NOTE_MAX_CHARS = 200
# Token-rate limits (429) are retried with the SDK's backoff, which honors retry-after headers.
CLIENT_MAX_RETRIES = 6
CLIENT_TIMEOUT_S = 180
# A stream that breaks mid-way is not retried by the SDK: retry a specialist once, then let the
# orchestrator answer without it instead of failing the whole answer.
CONSULT_ATTEMPTS = 2
CONSULT_FAILED = (
    "전문 에이전트 응답이 중간에 끊겨 결과를 받지 못했습니다({error}). 같은 요청을 다시 보내지 마세요. "
    "이미 조회한 도구 결과로 답하고, 이번에는 이 전문 작업(예: 웹 조사)을 하지 못했다고 한 문장으로 밝히세요."
)
# Specialist requests shorter than this (for example "아니오") carry no task and are rejected unsent.
MIN_CONSULT_REQUEST = 10
CONSULT_REQUEST_ERROR = (
    "request가 비어 있거나 너무 짧아 전문 에이전트를 호출하지 않았습니다. 개정, 층, 대상, 원하는 결과를 담은 "
    "작업 지시 문장을 쓰세요. 단순 조회는 MCP 도구로 직접 처리하세요."
)
FALLBACK_ANSWER = "답변을 만들지 못했어요. 질문을 조금 바꿔서 다시 물어봐 주세요."
# Tool calls that Foundry runs server-side; the value names tools whose items carry no `name`.
HOSTED_TOOL_ITEMS = {
    "mcp_call": None,
    "web_search_call": "web_search",
    "code_interpreter_call": "code_interpreter",
}
STREAM_FAILURES = ("response.failed", "response.incomplete", "error")


def agent_reference(name: str) -> dict[str, Any]:
    return {"agent_reference": {"name": name, "type": "agent_reference"}}


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _message_text(item: Any) -> str:
    parts = []
    for content in _get(item, "content", []) or []:
        if _get(content, "type") in ("output_text", "text"):
            parts.append(_get(content, "text", ""))
    return "".join(parts)


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


class _TextRouter:
    """Routes an agent's message text to progress notes or to the answer.

    A message is held until the next output item: a tool call makes it a note (the agent said what it is
    about to do), anything else makes it answer text. A message longer than ``NOTE_MAX_CHARS`` or with a line
    break is answer text at once, so long answers still stream. Each method returns ``(kind, text)`` pairs:
    "note", "answer_start" (the first text of an answer message) or "answer" (more of the same message).
    """

    def __init__(self) -> None:
        self._pending: list[str] | None = None
        self._answer = False

    def message(self) -> list[tuple[str, str]]:
        """A new message starts: an undecided previous message was not followed by a tool."""
        actions = self._flush("answer")
        self._pending, self._answer = [], False
        return actions

    def text(self, delta: str) -> list[tuple[str, str]]:
        if self._answer:
            return [("answer", delta)]
        if self._pending is None:
            self._pending = []
        self._pending.append(delta)
        joined = "".join(self._pending)
        if len(joined) > NOTE_MAX_CHARS or "\n" in joined.strip():
            self._pending, self._answer = None, True
            return [("answer_start", joined)]
        return []

    def tool(self) -> list[tuple[str, str]]:
        """A tool call follows: the undecided message was a progress note."""
        actions = self._flush("note")
        self._answer = False
        return actions

    def done(self) -> list[tuple[str, str]]:
        """The response ended: the undecided message was the answer."""
        actions = self._flush("answer")
        self._answer = False
        return actions

    def _flush(self, kind: str) -> list[tuple[str, str]]:
        text = "".join(self._pending or [])
        self._pending = None
        if not text.strip():
            return []
        return [("note", text.strip())] if kind == "note" else [("answer_start", text)]


class AgentRuntime:
    """Bridges Atlas chat requests to Foundry agents."""

    def __init__(
        self,
        openai_client: Any,
        ui_handler: UiActionHandler | None = None,
        orchestrator: str = ORCHESTRATOR,
        specialists: dict[str, str] | None = None,
        id_factory: Callable[[], str] | None = None,
        stream: bool = False,
        capture_tool_outputs: bool = False,
    ):
        self.client = openai_client
        self.ui_handler = ui_handler or UiActionHandler()
        self.orchestrator = orchestrator
        self.specialists = specialists or SPECIALISTS
        self.stream = stream
        self.capture_tool_outputs = capture_tool_outputs
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_project(cls, project_endpoint: str, credential: Any, **kwargs: Any) -> AgentRuntime:
        from azure.ai.projects import AIProjectClient

        project = AIProjectClient(endpoint=project_endpoint, credential=credential)
        client = project.get_openai_client().with_options(
            max_retries=CLIENT_MAX_RETRIES, timeout=CLIENT_TIMEOUT_S
        )
        kwargs.setdefault("stream", True)
        return cls(client, **kwargs)

    # One agent turn ----------------------------------------------------------------------------------
    def _respond(self, conversation_id: str, request: Any, agent: str) -> Iterator[tuple[str, Any]]:
        """Run one agent turn as events in output order: ("message", item) when a message starts, ("text",
        delta), ("tool_start" | "tool_end", item) for server-side tools, ("call", item) when a function call
        is added, then ("done", response)."""
        kwargs = {"conversation": conversation_id, "input": request, "extra_body": agent_reference(agent)}
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
                elif item_type == "function_call":
                    yield "call", item
            yield "done", response
            return
        final = None
        for event in self.client.responses.create(**kwargs, stream=True):
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
                elif item_type == "message" and added:
                    yield "message", item
                elif item_type == "function_call" and added:
                    yield "call", item
            elif event_type == "response.completed":
                final = _get(event, "response")
            elif event_type in STREAM_FAILURES:
                detail = _get(event, "message") or _get(_get(event, "response"), "error") or event_type
                raise RuntimeError(f"agent response failed: {_short(detail, 300)}")
        if final is None:
            raise RuntimeError("agent stream ended without a completed response")
        yield "done", final

    @staticmethod
    def _route(router: _TextRouter, kind: str) -> list[tuple[str, str]]:
        """Text routing for a non-text ``_respond`` event."""
        if kind == "message":
            return router.message()
        if kind in ("tool_start", "call"):
            return router.tool()
        if kind == "done":
            return router.done()
        return []

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

    # Specialists -------------------------------------------------------------------------------------
    def _consult(
        self, agent_key: str, request: str, parent_id: str
    ) -> Generator[dict[str, Any], None, tuple[str, int]]:
        """Run a specialist agent, streaming its tool calls under ``parent_id``; return (answer, calls).

        A failed run is retried once in a new conversation. If it fails again, the answer is an ``ok: false``
        result telling the orchestrator to answer without the specialist."""
        name = self.specialists.get(agent_key)
        if name is None:
            return json.dumps(
                {"ok": False, "error": f"unknown specialist {agent_key}"}, ensure_ascii=False
            ), 0
        calls = 0
        error: Exception | None = None
        for attempt in range(1, CONSULT_ATTEMPTS + 1):
            texts: list[str] = []
            response = None
            started: set[str] = set()
            router = _TextRouter()
            try:
                conversation = self.client.conversations.create()
                for kind, payload in self._respond(_get(conversation, "id"), request, name):
                    actions = router.text(payload) if kind == "text" else self._route(router, kind)
                    for action, text in actions:
                        if action == "note":
                            yield from self._note_events(text, parent_id=parent_id, agent=name)
                        else:
                            texts.append(text if action == "answer" or not texts else f"\n\n{text}")
                    if kind == "done":
                        response = payload
                    elif kind in ("tool_start", "tool_end"):
                        events = self._hosted_events(kind, payload, started, parent_id=parent_id, agent=name)
                        calls += sum(1 for event in events if event["type"] == "TOOL_CALL_END")
                        yield from events
            except Exception as exc:  # the orchestrator can still answer from its own tool results
                error = exc
                logger.warning(
                    "specialist %s failed (attempt %d/%d): %r", name, attempt, CONSULT_ATTEMPTS, exc
                )
                continue
            text = "".join(texts).strip() or (_get(response, "output_text", "") or "")
            return text[:SPECIALIST_OUTPUT_LIMIT], calls
        message = CONSULT_FAILED.format(error=type(error).__name__)
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False), calls

    # Conversation ------------------------------------------------------------------------------------
    def run(
        self, message: str, ui_state: dict[str, Any], conversation_id: str | None = None
    ) -> Iterator[dict[str, Any]]:
        run_id = self._new_id()
        if conversation_id is None:
            conversation_id = _get(self.client.conversations.create(), "id")
        yield {"type": "RUN_STARTED", "thread_id": conversation_id, "run_id": run_id}
        message_id: str | None = None
        wrote_text = False
        notes: list[str] = []
        try:
            pending: Any = f"[ui_state] {json.dumps(ui_state, ensure_ascii=False)}\n\n{message}"
            consults: dict[tuple[str, str], str] = {}
            response = None
            for _ in range(MAX_ROUNDS):
                started: set[str] = set()
                router = _TextRouter()
                for kind, payload in self._respond(conversation_id, pending, self.orchestrator):
                    actions = router.text(payload) if kind == "text" else self._route(router, kind)
                    for action, text in actions:
                        if action == "note":
                            notes.append(text)
                            yield from self._note_events(text)
                            continue
                        if message_id is None:
                            message_id = self._new_id()
                            yield {
                                "type": "TEXT_MESSAGE_START",
                                "message_id": message_id,
                                "role": "assistant",
                            }
                        if action == "answer_start" and wrote_text:
                            yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": message_id, "delta": "\n\n"}
                        wrote_text = True
                        yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": message_id, "delta": text}
                    if kind == "done":
                        response = payload
                    elif kind in ("tool_start", "tool_end"):
                        yield from self._hosted_events(kind, payload, started)
                calls = [
                    item
                    for item in _get(response, "output", []) or []
                    if _get(item, "type") == "function_call"
                ]
                if not calls:
                    break
                outputs = []
                for item in calls:
                    outputs.append((yield from self._handle_function(item, ui_state, consults)))
                pending = outputs
            if not wrote_text:
                # Normally unreachable; a short answer written before the last tool call ends up as a note.
                final_text = (
                    (_get(response, "output_text", "") or "").strip()
                    or (notes[-1] if notes else "")
                    or FALLBACK_ANSWER
                )
                message_id = message_id or self._new_id()
                yield {"type": "TEXT_MESSAGE_START", "message_id": message_id, "role": "assistant"}
                for start in range(0, len(final_text), TEXT_CHUNK):
                    chunk = final_text[start : start + TEXT_CHUNK]
                    yield {"type": "TEXT_MESSAGE_CONTENT", "message_id": message_id, "delta": chunk}
            yield {"type": "TEXT_MESSAGE_END", "message_id": message_id}
            yield {"type": "RUN_FINISHED", "thread_id": conversation_id, "run_id": run_id}
        except Exception as exc:  # surfaced to the UI; details stay in server logs
            limited = getattr(exc, "status_code", None) == 429 or "rate limit" in str(exc).lower()
            yield {
                "type": "RUN_ERROR",
                "message": (
                    "AI 서비스의 요청 한도에 도달했어요. 잠시 후 다시 시도해 주세요."
                    if limited
                    else "답변을 완료하지 못했어요. 잠시 후 다시 시도해 주세요."
                ),
            }
            raise

    def _handle_function(
        self, item: Any, ui_state: dict[str, Any], consults: dict[tuple[str, str], str]
    ) -> Generator[dict[str, Any], None, dict[str, Any]]:
        name = _get(item, "name")
        call_id = _get(item, "call_id")
        args = _safe_json(_get(item, "arguments"))
        method = tool_method(name, args)
        yield {
            "type": "TOOL_CALL_START",
            "tool_call_id": call_id,
            "tool_name": name,
            "args": args,
            "label": tool_label(name, args),
            "method": method,
        }
        ok = True
        if name == "consult_specialist":
            agent_key = str(args.get("agent", ""))
            request = str(args.get("request", "")).strip()
            key = (agent_key, request)
            if len(request) < MIN_CONSULT_REQUEST:
                output = json.dumps({"ok": False, "error": CONSULT_REQUEST_ERROR}, ensure_ascii=False)
                summary, ok = "요청 내용이 없어 상담하지 않음", False
                result = None
            elif key in consults:
                output = consults[key]
                summary = f"{agent_key} 같은 요청의 앞선 응답 재사용"
                result = specialist_result(agent_key, _output_ok(output))
            else:
                output, calls = yield from self._consult(agent_key, request, call_id)
                consults[key] = output
                ok = _output_ok(output)
                summary = (
                    f"{agent_key} 응답 {len(output)}자, 도구 호출 {calls}회"
                    if ok
                    else f"{agent_key} 응답 실패(다시 시도 후), 도구 호출 {calls}회"
                )
                result = specialist_result(agent_key, ok)
        else:
            handled = self.ui_handler.handle(name, args, ui_state)
            output = handled.output
            summary = _short(output, 120)
            ok = _output_ok(output)
            result = tool_result(name, args, output)
            if handled.patches:
                yield {"type": "STATE_DELTA", "delta": handled.patches}
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
