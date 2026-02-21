# agent_graph.py
# core agent logic — handles MCP tool loading, auth, chat history, and the langgraph agent loop

import os
import json
import logging
from typing import Annotated, TypedDict, Sequence, Any, Optional

import pydantic
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

mcp_api_key = os.getenv("FASTMCP_KEY")


# point to our hosted FastMCP server
SERVERS = {
    "expense": {
        "transport": "streamable_http",
        "url": "https://unlikely-blue-starfish.fastmcp.app/mcp",
        "headers": {
            "Authorization": f"Bearer {mcp_api_key}"
        }
    }
}


async def get_mcp_tools() -> list:
    # fresh client every time — keeps things simple and avoids stale connections
    client = MultiServerMCPClient(SERVERS)
    return await client.get_tools()


def _find_tool(tools: list, name: str):
    return next((t for t in tools if t.name == name), None)


def _extract_mcp_result(result) -> str:
    # MCP tools return a tuple like:
    # ([{'type': 'text', 'text': '...'}], {'structured_content': ...})
    # we just want the text string out of it
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list) and result:
        result = result[0]
    if isinstance(result, dict):
        return result.get("text", str(result))
    return str(result)


#Auth

# these are called directly from the UI during login/register,
# not exposed to the LLM as agent tools

async def mcp_register(name: str, username: str, password: str) -> str:
    tools = await get_mcp_tools()
    tool = _find_tool(tools, "register_user")
    if not tool:
        return "Registration tool not available"
    result = await tool.coroutine(name=name, username=username, password=password)
    return _extract_mcp_result(result)


async def mcp_login(username: str, password: str) -> str:
    tools = await get_mcp_tools()
    tool = _find_tool(tools, "login_user")
    if not tool:
        return "Login tool not available"
    result = await tool.coroutine(username=username, password=password)
    return _extract_mcp_result(result)


#Chat History

# also called directly from the UI, not agent tools

async def fetch_chat_history(username: str, password: str, limit: int = 100) -> list[dict]:
    # returns messages as [{"role": "user"/"assistant", "content": "..."}]
    # in chronological order, ready to be fed into the agent
    tools = await get_mcp_tools()
    tool = _find_tool(tools, "get_chat_history_raw")
    if not tool:
        return []

    result = await tool.coroutine(username=username, password=password, limit=limit)
    text = _extract_mcp_result(result)

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # if parsing fails just return empty, not worth crashing over
        return []


async def save_chat_exchange_direct(username: str, password: str, user_message: str, assistant_message: str) -> bool:
    # save both sides of the conversation after each turn
    tools = await get_mcp_tools()
    tool = _find_tool(tools, "save_chat_exchange")
    if not tool:
        return False

    result = await tool.coroutine(
        username=username,
        password=password,
        user_message=user_message,
        assistant_message=assistant_message
    )
    text = _extract_mcp_result(result)
    logger.info(f"saved chat exchange for '{username}': {text}")
    return "saved" in text.lower()


async def clear_chat_history_direct(username: str, password: str) -> bool:
    tools = await get_mcp_tools()
    tool = _find_tool(tools, "clear_chat_history")
    if not tool:
        return False

    result = await tool.coroutine(username=username, password=password)
    text = _extract_mcp_result(result)
    logger.info(f"cleared chat history for '{username}': {text}")
    return "cleared" in text.lower()


#System Prompt

def build_system_prompt(username: str, name: str) -> str:
    # this gets injected at the top of every conversation
    # the rules here are important — without them the LLM tends to add duplicate expenses
    # when the user says things like "actually it was $50 not $40"
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
        f"- Step 2: Call update_expense with that expense_id and the corrected values.\n"
        f"- NEVER call add_expense for a correction.\n\n"

        f"### Deleting expenses:\n"
        f"- Step 1: Call get_expenses to find the expense_id.\n"
        f"- Step 2: Call delete_expense with that id.\n\n"

        f"### General:\n"
        f"- For budget, summary, trends — call the appropriate tool directly.\n"
        f"- For all other topics, respond naturally without using any tools.\n"
    )


def build_history_as_messages(raw_history: list[dict], system_prompt: str) -> list[BaseMessage]:
    # converts the flat list from the DB into proper langchain message objects
    messages = [SystemMessage(content=system_prompt)]
    for msg in raw_history:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))
    return messages


# LangGraph State 

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]


# maps JSON schema types to Python types for building pydantic models dynamically
PYTHON_TYPE_MAP = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_model_without_credentials(tool_name: str, tool) -> Any:
    # we strip username/password from every tool's schema before handing them to the LLM
    # so the model never has to know or pass credentials — we inject them ourselves
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

    # skip auth and chat history tools — the LLM doesn't need these,
    # they're handled directly by the UI
    skip_tools = {
        "register_user", "login_user", "change_password",
        "save_chat_message", "save_chat_exchange",
        "get_chat_history", "get_chat_history_raw", "clear_chat_history"
    }

    for tool in mcp_tools:
        if tool.name in skip_tools:
            continue

        new_schema = _build_model_without_credentials(tool.name, tool)

        def make_coroutine(t, uname, pwd):
            async def wrapper(**kwargs):
                # drop None values before injecting credentials
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
        logger.info(f"wrapped tool '{tool.name}' for user '{username}'")

    return wrapped


#Agent

llm = ChatGroq(model="llama-3.3-70b-versatile")


def make_call_model(llm_with_tools):
    async def call_model(state: AgentState):
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}
    return call_model


def should_continue(state: AgentState):
    # if the LLM wants to call a tool, route to the tool node
    # otherwise we're done
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and getattr(last_message, "tool_calls", None):
        return "tools"
    return END


async def build_app(username: str, password: str, name: str):
    raw_tools = await get_mcp_tools()
    logger.info(f"fetched {len(raw_tools)} tools from MCP server")

    tools = inject_credentials_into_tools(raw_tools, username, password)
    llm_with_tools = llm.bind_tools(tools)

    # standard react-style agent loop:
    # call model -> if tool needed, run it -> call model again -> repeat until done
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
    app = workflow.compile()
    print(app)
    return app


async def run_agent(history, username: str, password: str, name: str):
    app = await build_app(username, password, name)
    final_state = None

    async for state in app.astream(
        {"messages": history},
        {"recursion_limit": 10},
    ):
        final_state = state

    if not final_state:
        return []

    # pull messages from whichever node ran last
    if "agent" in final_state:
        return final_state["agent"]["messages"]
    elif "tools" in final_state:
        return final_state["tools"]["messages"]

    return []