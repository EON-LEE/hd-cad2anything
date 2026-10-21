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
        return cls(project.get_openai_client(), **kwargs)

    def consult(self, agent_key: str, request: str) -> str:
        name = self.specialists.get(agent_key)
        if name is None:
            return json.dumps({"ok": False, "error": f"unknown specialist {agent_key}"}, ensure_ascii=False)
        conversation = self.client.conversations.create()
        response = self.client.responses.create(
            conversation=_get(conversation, "id"), input=request, extra_body=agent_reference(name)
        )
        text = _get(response, "output_text", "") or ""
        return text[:SPECIALIST_OUTPUT_LIMIT]

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
            for _ in range(MAX_ROUNDS):
                outputs: list[dict[str, Any]] = []
                for item in _get(response, "output", []) or []:
                    item_type = _get(item, "type")
                    if item_type == "mcp_call":
                        call_id = _get(item, "id") or self._new_id()
                        yield {
                            "type": "TOOL_CALL_START",
                            "tool_call_id": call_id,
                            "tool_name": _get(item, "name"),
                            "args": _safe_json(_get(item, "arguments")),
                        }
                        error = _get(item, "error")
                        summary = f"오류: {_short(error)}" if error else _short(_get(item, "output", ""))
                        yield {"type": "TOOL_CALL_END", "tool_call_id": call_id, "summary": summary}
                    elif item_type == "function_call":
                        events: list[dict[str, Any]] = []
                        outputs.append(self._handle_function(item, ui_state, events))
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
        self, item: Any, ui_state: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        name = _get(item, "name")
        call_id = _get(item, "call_id")
        args = _safe_json(_get(item, "arguments"))
        events.append({"type": "TOOL_CALL_START", "tool_call_id": call_id, "tool_name": name, "args": args})
        if name == "consult_specialist":
            output = self.consult(args.get("agent", ""), args.get("request", ""))
            summary = f"{args.get('agent')} 응답 {len(output)}자"
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
