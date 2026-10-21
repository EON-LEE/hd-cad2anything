"""Run the Atlas orchestrator on Foundry and stream AG-UI compatible events.

MCP tools run inside Foundry Agent Service. Function tools (UI actions and specialist consultation)
are executed here and returned as ``function_call_output`` items in the same conversation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from cad2anything.agents import ORCHESTRATOR, SPECIALISTS
from cad2anything.agents.ui_actions import UiActionHandler

MAX_ROUNDS = 8
SPECIALIST_OUTPUT_LIMIT = 6000
TEXT_CHUNK = 80
# Token-rate limits (429) are retried with the SDK's backoff, which honors retry-after headers.
CLIENT_MAX_RETRIES = 6
CLIENT_TIMEOUT_S = 180
# Specialist requests shorter than this (for example "아니오") carry no task and are rejected unsent.
MIN_CONSULT_REQUEST = 10
CONSULT_REQUEST_ERROR = (
    "request가 비어 있거나 너무 짧아 전문 에이전트를 호출하지 않았습니다. 개정, 층, 대상, 원하는 결과를 담은 "
    "작업 지시 문장을 쓰세요. 단순 조회는 MCP 도구로 직접 처리하세요."
)
# Tool calls that Foundry runs server-side; the value names tools whose items carry no `name`.
HOSTED_TOOL_ITEMS = {
    "mcp_call": None,
    "web_search_call": "web_search",
    "code_interpreter_call": "code_interpreter",
}


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
    if error:
        summary = f"오류: {_short(error)}"
    else:
        summary = _short(_get(item, "output", "") or _get(item, "status", "") or "")
    return {
        "id": _get(item, "id"),
        "tool_name": _get(item, "name") or HOSTED_TOOL_ITEMS[item_type],
        "args": args,
        "summary": summary,
        "error": bool(error),
    }


class AgentRuntime:
    """Bridges Atlas chat requests to Foundry agents."""

    def __init__(
        self,
        openai_client: Any,
        ui_handler: UiActionHandler | None = None,
        orchestrator: str = ORCHESTRATOR,
        specialists: dict[str, str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ):
        self.client = openai_client
        self.ui_handler = ui_handler or UiActionHandler()
        self.orchestrator = orchestrator
        self.specialists = specialists or SPECIALISTS
        self._new_id = id_factory or (lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_project(cls, project_endpoint: str, credential: Any, **kwargs: Any) -> AgentRuntime:
        from azure.ai.projects import AIProjectClient

        project = AIProjectClient(endpoint=project_endpoint, credential=credential)
        client = project.get_openai_client().with_options(
            max_retries=CLIENT_MAX_RETRIES, timeout=CLIENT_TIMEOUT_S
        )
        return cls(client, **kwargs)

    def consult(self, agent_key: str, request: str) -> tuple[str, list[dict[str, Any]]]:
        """Run a specialist agent; return its answer and the server-side tool calls it made."""
        name = self.specialists.get(agent_key)
        if name is None:
            error = json.dumps({"ok": False, "error": f"unknown specialist {agent_key}"}, ensure_ascii=False)
            return error, []
        conversation = self.client.conversations.create()
        response = self.client.responses.create(
            conversation=_get(conversation, "id"), input=request, extra_body=agent_reference(name)
        )
        calls = [
            call for call in map(hosted_tool_call, _get(response, "output", []) or []) if call is not None
        ]
        text = _get(response, "output_text", "") or ""
        return text[:SPECIALIST_OUTPUT_LIMIT], calls

    def run(
        self, message: str, ui_state: dict[str, Any], conversation_id: str | None = None
    ) -> Iterator[dict[str, Any]]:
        run_id = self._new_id()
        if conversation_id is None:
            conversation_id = _get(self.client.conversations.create(), "id")
        yield {"type": "RUN_STARTED", "thread_id": conversation_id, "run_id": run_id}
        try:
            prompt = f"[ui_state] {json.dumps(ui_state, ensure_ascii=False)}\n\n{message}"
            response = self.client.responses.create(
                conversation=conversation_id, input=prompt, extra_body=agent_reference(self.orchestrator)
            )
            texts: list[str] = []
            consults: dict[tuple[str, str], str] = {}
            for _ in range(MAX_ROUNDS):
                outputs: list[dict[str, Any]] = []
                for item in _get(response, "output", []) or []:
                    item_type = _get(item, "type")
                    hosted = hosted_tool_call(item)
                    if hosted is not None:
                        call_id = hosted["id"] or self._new_id()
                        yield {
                            "type": "TOOL_CALL_START",
                            "tool_call_id": call_id,
                            "tool_name": hosted["tool_name"],
                            "args": hosted["args"],
                        }
                        yield {"type": "TOOL_CALL_END", "tool_call_id": call_id, "summary": hosted["summary"]}
                    elif item_type == "function_call":
                        events: list[dict[str, Any]] = []
                        outputs.append(self._handle_function(item, ui_state, events, consults))
                        yield from events
                    elif item_type == "message":
                        text = _message_text(item)
                        if text:
                            texts.append(text)
                if not outputs:
                    break
                texts.clear()
                response = self.client.responses.create(
                    conversation=conversation_id, input=outputs, extra_body=agent_reference(self.orchestrator)
                )
            final_text = "\n".join(texts).strip() or (_get(response, "output_text", "") or "").strip()
            message_id = self._new_id()
            yield {"type": "TEXT_MESSAGE_START", "message_id": message_id, "role": "assistant"}
            for start in range(0, len(final_text), TEXT_CHUNK):
                yield {
                    "type": "TEXT_MESSAGE_CONTENT",
                    "message_id": message_id,
                    "delta": final_text[start : start + TEXT_CHUNK],
                }
            yield {"type": "TEXT_MESSAGE_END", "message_id": message_id}
            yield {"type": "RUN_FINISHED", "thread_id": conversation_id, "run_id": run_id}
        except Exception as exc:  # surfaced to the UI; details stay in server logs
            yield {
                "type": "RUN_ERROR",
                "message": f"에이전트 실행 중 오류가 발생했습니다: {type(exc).__name__}",
            }
            raise

    def _handle_function(
        self,
        item: Any,
        ui_state: dict[str, Any],
        events: list[dict[str, Any]],
        consults: dict[tuple[str, str], str] | None = None,
    ) -> dict[str, Any]:
        name = _get(item, "name")
        call_id = _get(item, "call_id")
        args = _safe_json(_get(item, "arguments"))
        events.append({"type": "TOOL_CALL_START", "tool_call_id": call_id, "tool_name": name, "args": args})
        if name == "consult_specialist":
            agent_key = str(args.get("agent", ""))
            request = str(args.get("request", "")).strip()
            key = (agent_key, request)
            if len(request) < MIN_CONSULT_REQUEST:
                output = json.dumps({"ok": False, "error": CONSULT_REQUEST_ERROR}, ensure_ascii=False)
                summary = "요청 내용이 없어 상담하지 않음"
            elif consults is not None and key in consults:
                output = consults[key]
                summary = f"{agent_key} 같은 요청의 앞선 응답 재사용"
            else:
                output, nested = self.consult(agent_key, request)
                if consults is not None:
                    consults[key] = output
                for call in nested:
                    nested_id = call["id"] or self._new_id()
                    events.append(
                        {
                            "type": "TOOL_CALL_START",
                            "tool_call_id": nested_id,
                            "tool_name": call["tool_name"],
                            "args": call["args"],
                            "parent_tool_call_id": call_id,
                            "agent": self.specialists.get(agent_key, agent_key),
                        }
                    )
                    events.append(
                        {"type": "TOOL_CALL_END", "tool_call_id": nested_id, "summary": call["summary"]}
                    )
                summary = f"{agent_key} 응답 {len(output)}자, 도구 호출 {len(nested)}회"
        else:
            result = self.ui_handler.handle(name, args, ui_state)
            output = result.output
            summary = _short(output, 120)
            if result.patches:
                events.append({"type": "STATE_DELTA", "delta": result.patches})
        events.append({"type": "TOOL_CALL_END", "tool_call_id": call_id, "summary": summary})
        return {"type": "function_call_output", "call_id": call_id, "output": output}


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
