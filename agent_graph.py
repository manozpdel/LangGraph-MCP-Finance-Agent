# agent_graph.py

import os
import logging
import requests as _requests
from typing import Annotated, TypedDict, Sequence, Any, Optional

import pydantic
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import StructuredTool

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

mcp_api_key  = os.getenv("FASTMCP_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ─────────────────────────────
# MCP Server Configuration
# ─────────────────────────────
SERVERS = {
    "expense": {
        "transport": "streamable_http",
        "url": "https://unlikely-blue-starfish.fastmcp.app/mcp",
        "headers": {
            "Authorization": f"Bearer {mcp_api_key}"
        }
    }
}

_SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}


# ─────────────────────────────
# Auth — Direct Supabase REST
# ─────────────────────────────
async def mcp_register(name: str, username: str, password: str) -> str:
    username = username.strip().lower()
    res = _requests.get(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=_SB_HEADERS,
        params={"username": f"eq.{username}"}
    )
    if res.ok and res.json():
        return f"Username '{username}' is already taken"

    res = _requests.post(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=_SB_HEADERS,
        json={"name": name.strip(), "username": username, "password": password}
    )
    result = res.json()
    if isinstance(result, list) and result and "id" in result[0]:
        return f"Welcome, {name}! Your account has been created. You can now login with username '{username}'"
    return f"Registration failed: {result}"


async def mcp_login(username: str, password: str) -> str:
    username = username.strip().lower()
    res = _requests.get(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=_SB_HEADERS,
        params={"username": f"eq.{username}", "password": f"eq.{password}"}
    )
    if res.ok and res.json():
        user = res.json()[0]
        return (
            f"Login successful!\n"
            f"Welcome back, {user['name']}!\n"
            f"User ID: {user['id']}\n"
            f"Username: {user['username']}"
        )
    return "Invalid username or password"


# ─────────────────────────────
# System Prompt
# ─────────────────────────────
def build_system_prompt(username: str, name: str) -> str:
    return (
        f"You are a helpful AI assistant. You can chat about any topic freely.\n\n"
        f"The current logged-in user is: {name} (username: '{username}')\n\n"
        f"When the user wants to track expenses, use the available tools. "
        f"IMPORTANT: The tools are already pre-configured with the user's credentials. "
        f"Do NOT pass username or password to any tool — they are injected automatically.\n\n"

        f"## EXPENSE TRACKING RULES:\n\n"

        f"### Adding expenses:\n"
        f"- When user says they spent money, call add_expense with amount and category.\n"
        f"- Never add an expense if the user is asking to correct or update a previous one.\n\n"

        f"### Updating expenses:\n"
        f"- If the user says 'actually', 'correction', 'update', 'change', 'fix', 'wrong amount', "
        f"or refers to a previous expense — this is an UPDATE, NOT a new expense.\n"
        f"- Step 1: Call get_expenses to fetch recent expenses and find the correct expense_id.\n"
        f"- Step 2: Call update_expense with that expense_id and the corrected amount/category.\n"
        f"- NEVER call add_expense for a correction.\n\n"

        f"### Deleting expenses:\n"
        f"- Step 1: Call get_expenses to find the expense_id.\n"
        f"- Step 2: Call delete_expense with that id.\n\n"

        f"### General:\n"
        f"- For budget, summary, trends — call the appropriate tool directly.\n"
        f"- For all other topics, respond naturally without using any tools.\n"
    )


# ─────────────────────────────
# LangGraph State
# ─────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# ─────────────────────────────
# JSON Schema type to Python type mapping
# ─────────────────────────────
PYTHON_TYPE_MAP = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_model_without_credentials(tool_name: str, tool) -> Any:
    if isinstance(tool.args_schema, dict):
        schema = tool.args_schema
    elif hasattr(tool.args_schema, "model_json_schema"):
        schema = tool.args_schema.model_json_schema()
    elif hasattr(tool.args_schema, "schema"):
        schema = tool.args_schema.schema()
    else:
        schema = {}

    properties = schema.get("properties", {})
    required_fields = schema.get("required", [])

    fields = {}
    for field_name, field_info in properties.items():
        if field_name in ("username", "password"):
            continue

        py_type = PYTHON_TYPE_MAP.get(field_info.get("type", "string"), str)
        description = field_info.get("description", "")

        if field_name in required_fields:
            fields[field_name] = (py_type, pydantic.Field(..., description=description))
        else:
            default = field_info.get("default", None)
            fields[field_name] = (
                Optional[py_type],
                pydantic.Field(default=default, description=description)
            )

    return pydantic.create_model(tool_name + "Input", **fields)


def inject_credentials_into_tools(mcp_tools, username: str, password: str):
    wrapped = []
    skip_tools = {"register_user", "login_user", "change_password"}

    for tool in mcp_tools:
        if tool.name in skip_tools:
            continue

        new_schema = _build_model_without_credentials(tool.name, tool)

        # Capture tool in closure correctly
        def make_coroutine(t, uname, pwd):
            async def wrapper(**kwargs):
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
                kwargs["username"] = uname
                kwargs["password"] = pwd
                return await t.coroutine(**kwargs)
            return wrapper

        new_tool = StructuredTool(
            name=tool.name,
            description=tool.description,
            coroutine=make_coroutine(tool, username, password),
            args_schema=new_schema,
        )
        wrapped.append(new_tool)
        logger.info(f"Wrapped tool '{tool.name}' with credentials for '{username}'")

    return wrapped


# ─────────────────────────────
# LLM
# ─────────────────────────────
llm = ChatGroq(model="llama-3.3-70b-versatile")


def make_call_model(llm_with_tools):
    async def call_model(state: AgentState):
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}
    return call_model


def should_continue(state: AgentState):
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and getattr(last_message, "tool_calls", None):
        return "tools"
    return END


async def build_app(username: str, password: str, name: str):
    client = MultiServerMCPClient(SERVERS)
    raw_tools = await client.get_tools()
    logger.info(f"Fetched {len(raw_tools)} tools from MCP server")

    tools = inject_credentials_into_tools(raw_tools, username, password)
    llm_with_tools = llm.bind_tools(tools)

    workflow = StateGraph(AgentState)
    workflow.add_node("agent", make_call_model(llm_with_tools))
    workflow.add_node("tools", ToolNode(tools))
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", END: END},
    )
    workflow.add_edge("tools", "agent")
    return workflow.compile()


async def run_agent(history, username: str, password: str, name: str):
    app = await build_app(username, password, name)
    final_state = None

    async for state in app.astream(
        {"messages": history},
        {"recursion_limit": 10},
    ):
        final_state = state

    if final_state:
        if "agent" in final_state:
            return final_state["agent"]["messages"]
        elif "tools" in final_state:
            return final_state["tools"]["messages"]

    return []