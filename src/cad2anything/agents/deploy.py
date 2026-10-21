"""Create or update the Foundry prompt agents defined in agents/agents.yaml.

Usage:
    uv run python -m cad2anything.agents.deploy --dry-run
    uv run python -m cad2anything.agents.deploy

Environment: FOUNDRY_PROJECT_ENDPOINT, CHAT_MODEL_DEPLOYMENT, MINI_MODEL_DEPLOYMENT, MCP_SERVER_URL,
and either MCP_CONNECTION_ID (recommended) or MCP_API_KEY (header fallback for local testing).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_FILE = REPO_ROOT / "agents" / "agents.yaml"
UI_TOOLS_FILE = REPO_ROOT / "agents" / "ui_tools.yaml"
MCP_SERVER_LABEL = "cad2anything"


def load_definitions(chat_model: str, mini_model: str, path: Path = AGENTS_FILE) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    definitions = []
    for agent in data["agents"]:
        agent = dict(agent)
        agent["model"] = agent["model"].format(chat_model=chat_model, mini_model=mini_model)
        definitions.append(agent)
    return definitions


def load_ui_tools(path: Path = UI_TOOLS_FILE) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["tools"]


def tool_specs(
    agent: dict[str, Any],
    ui_tools: list[dict[str, Any]],
    mcp_url: str | None,
    mcp_connection_id: str | None,
    mcp_api_key: str | None,
) -> list[dict[str, Any]]:
    """Return SDK-neutral tool specs so the selection logic is testable without Azure."""
    specs: list[dict[str, Any]] = []
    mcp_tools = agent.get("mcp_tools")
    if mcp_tools:
        if not mcp_url:
            raise ValueError(f"{agent['name']} needs MCP_SERVER_URL")
        mcp: dict[str, Any] = {
            "kind": "mcp",
            "server_label": MCP_SERVER_LABEL,
            "server_url": mcp_url,
            "require_approval": "never",
            "allowed_tools": None if mcp_tools == "all" else list(mcp_tools),
        }
        if mcp_connection_id:
            mcp["project_connection_id"] = mcp_connection_id
        elif mcp_api_key:
            mcp["headers"] = {"x-api-key": mcp_api_key}
        specs.append(mcp)
    if agent.get("web_search"):
        specs.append({"kind": "web_search"})
    if agent.get("code_interpreter"):
        specs.append({"kind": "code_interpreter"})
    for tool in ui_tools:
        is_consult = tool["name"] == "consult_specialist"
        if (is_consult and agent.get("consult")) or (not is_consult and agent.get("ui_tools")):
            specs.append({"kind": "function", **tool})
    return specs


def build_tool(spec: dict[str, Any]) -> Any:
    from azure.ai.projects.models import CodeInterpreterTool, FunctionTool, MCPTool, WebSearchTool

    kind = spec["kind"]
    if kind == "mcp":
        kwargs = {k: v for k, v in spec.items() if k != "kind" and v is not None}
        return MCPTool(**kwargs)
    if kind == "web_search":
        return WebSearchTool()
    if kind == "code_interpreter":
        return CodeInterpreterTool()
    return FunctionTool(
        name=spec["name"], description=spec["description"], parameters=spec["parameters"], strict=False
    )


def deploy(dry_run: bool = False) -> dict[str, Any]:
    chat_model = os.environ.get("CHAT_MODEL_DEPLOYMENT", "gpt-6.1-sol")
    mini_model = os.environ.get("MINI_MODEL_DEPLOYMENT", "gpt-5.4-mini")
    mcp_url = os.environ.get("MCP_SERVER_URL")
    mcp_connection_id = os.environ.get("MCP_CONNECTION_ID")
    mcp_api_key = os.environ.get("MCP_API_KEY")
    ui_tools = load_ui_tools()
    plan = []
    for agent in load_definitions(chat_model, mini_model):
        specs = tool_specs(
            agent, ui_tools, mcp_url or "https://example.invalid/mcp", mcp_connection_id, mcp_api_key
        )
        plan.append((agent, specs))
    summary = {
        agent["name"]: {
            "model": agent["model"],
            "reasoning_effort": agent.get("reasoning_effort"),
            "tools": [s.get("name", s["kind"]) for s in specs],
        }
        for agent, specs in plan
    }
    if dry_run:
        return summary
    if not mcp_url:
        raise SystemExit("MCP_SERVER_URL is required to deploy agents")

    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import PromptAgentDefinition, Reasoning
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(
        endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=DefaultAzureCredential()
    )
    for agent, specs in plan:
        options: dict[str, Any] = {}
        if agent.get("reasoning_effort"):
            options["reasoning"] = Reasoning(effort=agent["reasoning_effort"])
        created = project.agents.create_version(
            agent_name=agent["name"],
            definition=PromptAgentDefinition(
                model=agent["model"],
                instructions=agent["instructions"],
                tools=[build_tool(s) for s in specs],
                **options,
            ),
        )
        summary[agent["name"]]["version"] = getattr(created, "version", None)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(deploy(dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
