# agent_graph.py

import os
import logging
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

mcp_api_key = os.getenv("FASTMCP_KEY")

# ─────────────────────────────
# MCP Server Configuration
# ─────────────────────────────
SERVERS = {
    "expense": {
        "transport": "streamable_http",
        "url": "https://hidden-violet-butterfly.fastmcp.app/mcp",
        "headers": {
            "Authorization": f"Bearer {mcp_api_key}"
        }
    }
}


def build_system_prompt(user_id: str) -> str:
    return (
        f"You are a helpful AI expense tracking assistant.\n\n"
        f"The current user is: '{user_id}'\n\n"
        f"IMPORTANT: The tools are already pre-configured for user '{user_id}'. "
        f"Do NOT pass user_id to any tool — it is injected automatically.\n\n"

        f"## RULES — follow strictly:\n\n"

        f"### Adding expenses:\n"
        f"- When user says they spent money, call add_expense with amount and category.\n"
        f"- Never add an expense if the user is asking to correct or update a previous one.\n\n"

        f"### Updating expenses:\n"
        f"- If the user says 'actually', 'correction', 'update', 'change', 'fix', 'wrong amount', "
        f"or refers to a previous expense — this is an UPDATE, NOT a new expense.\n"
        f"- Step 1: Call get_expenses to fetch recent expenses and find the correct expense_id.\n"
        f"- Step 2: Call update_expense with that expense_id and the corrected amount/category.\n"
        f"- NEVER call add_expense for a correction. NEVER.\n\n"

        f"### Deleting expenses:\n"
        f"- Step 1: Call get_expenses to find the expense_id.\n"
        f"- Step 2: Call delete_expense with that id.\n\n"

        f"### General:\n"
        f"- For budget, summary, trends — call the appropriate tool directly.\n"
        f"- For normal conversation, reply without tools.\n"
    )


# ─────────────────────────────
# LangGraph State
# ─────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# ─────────────────────────────
# JSON Schema type → Python type mapping
# ─────────────────────────────
PYTHON_TYPE_MAP = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_model_without_user_id(tool_name: str, tool) -> Any:
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
        if field_name == "user_id":
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


def inject_user_id_into_tools(mcp_tools, user_id: str):
    wrapped = []

    for tool in mcp_tools:
        original_coroutine = tool.coroutine
        new_schema = _build_model_without_user_id(tool.name, tool)

        def make_coroutine(fn, uid):
            async def wrapper(**kwargs):
                kwargs = {k: v for k, v in kwargs.items() if v is not None}
                kwargs["user_id"] = uid
                return await fn(**kwargs)
            return wrapper

        new_tool = StructuredTool(
            name=tool.name,
            description=tool.description,
            coroutine=make_coroutine(original_coroutine, user_id),
            args_schema=new_schema,
        )
        wrapped.append(new_tool)
        logger.info(f"Wrapped tool '{tool.name}' with user_id='{user_id}'")

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


async def build_app(user_id: str):
    client = MultiServerMCPClient(SERVERS)
    raw_tools = await client.get_tools()
    logger.info(f"Fetched {len(raw_tools)} tools from MCP server")

    tools = inject_user_id_into_tools(raw_tools, user_id)
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


# ─────────────────────────────
# Public Function for UI
# ─────────────────────────────
async def run_agent(history, user_id: str):
    app = await build_app(user_id)
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