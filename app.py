# app.py

import asyncio
import threading
import streamlit as st

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent_graph import run_agent, build_system_prompt


# ─────────────────────────────
# Safe Async Runner
# ─────────────────────────────
def run_async(coro):
    result = None
    exception = None

    def thread_target():
        nonlocal result, exception
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(coro)
        except Exception as e:
            exception = e
        finally:
            loop.close()

    t = threading.Thread(target=thread_target)
    t.start()
    t.join()

    if exception:
        raise exception
    return result


# ─────────────────────────────
# Helper: Extract Text from LLM Content
# ─────────────────────────────
def extract_text(content):
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content


# ─────────────────────────────
# Page Config
# ─────────────────────────────
st.set_page_config(page_title="💸 Expense Tracker AI", page_icon="💸")
st.title("💸 Expense Tracker AI")


# ─────────────────────────────
# Sidebar — User Identity
# ─────────────────────────────
with st.sidebar:
    st.header("👤 User Settings")

    user_id = st.text_input(
        "Your User ID",
        value=st.session_state.get("user_id", ""),
        placeholder="e.g. alice, bob, user123",
        help="All your expenses are stored under this ID."
    )

    if user_id:
        st.session_state["user_id"] = user_id.strip()
        st.success(f"Logged in as: **{user_id.strip()}**")
    else:
        st.warning("Please enter a User ID to start tracking.")

    st.divider()
    st.markdown("**Available Commands (just chat naturally):**")
    st.markdown("""
-  Add expense: *"I spent $50 on food"*
-  View expenses: *"Show my recent expenses"*
- Monthly summary: *"Summary for this month"*
- Set budget: *"Set my budget to $500"*
- Check budget: *"How's my budget looking?"*
- Spending trend: *"Show my spending trend"*
- Recurring: *"Add $100 Netflix on day 5 every month"*
- Update: *"Update expense #3 to $75"*
- Delete: *"Delete expense #2"*
""")

    st.divider()
    if st.button("🗑️ Clear Chat History"):
        st.session_state.pop("history", None)
        st.rerun()


# ─────────────────────────────
# Block UI if no user_id
# ─────────────────────────────
if not st.session_state.get("user_id"):
    st.info("👈 Please enter your **User ID** in the sidebar to get started.")
    st.stop()

current_user_id = st.session_state["user_id"]


# ─────────────────────────────
# Initialize / Reset History on User Change
# ─────────────────────────────
if (
    "history" not in st.session_state
    or st.session_state.get("history_user_id") != current_user_id
):
    st.session_state.history = [SystemMessage(content=build_system_prompt(current_user_id))]
    st.session_state["history_user_id"] = current_user_id


# ─────────────────────────────
# Display Chat History
# ─────────────────────────────
for msg in st.session_state.history:

    if isinstance(msg, HumanMessage):
        with st.chat_message("user"):
            st.markdown(msg.content)

    elif isinstance(msg, AIMessage):
        if not getattr(msg, "tool_calls", None):
            with st.chat_message("assistant"):
                st.markdown(extract_text(msg.content))


# ─────────────────────────────
# User Input
# ─────────────────────────────
user_text = st.chat_input(f"Message as {current_user_id}…")

if user_text:

    with st.chat_message("user"):
        st.markdown(user_text)

    st.session_state.history.append(HumanMessage(content=user_text))

    with st.spinner("Thinking…"):
        new_messages = run_async(run_agent(st.session_state.history, current_user_id))

    for msg in new_messages:
        if msg not in st.session_state.history:
            st.session_state.history.append(msg)

    for msg in new_messages:
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            with st.chat_message("assistant"):
                st.markdown(extract_text(msg.content))
            break

    st.rerun()