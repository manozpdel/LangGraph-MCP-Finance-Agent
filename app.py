import asyncio
import threading
import streamlit as st

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from agent_graph import (
    run_agent,
    build_system_prompt,
    build_history_as_messages,
    mcp_login,
    mcp_register,
    fetch_chat_history,
    save_chat_exchange_direct,
)


# Page Config
st.set_page_config(page_title="Expense Tracker AI", layout="centered")


# Helpers

# run async functions from sync context (streamlit doesn't play well with asyncio)
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


# AI sometimes returns content as a list of blocks, this just pulls the text out
def extract_text(content):
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content


# the DB stores messages as a flat list, we need pairs for the sidebar thread view
def raw_to_pairs(raw: list[dict]) -> list[tuple[str, str]]:
    pairs = []
    i = 0
    while i < len(raw):
        msg = raw[i]
        if msg["role"] == "user":
            user_content = msg["content"]
            assistant_content = ""
            if i + 1 < len(raw) and raw[i + 1]["role"] == "assistant":
                assistant_content = raw[i + 1]["content"]
                i += 2
            else:
                i += 1
            pairs.append((user_content, assistant_content))
        else:
            i += 1
    return pairs


def start_new_chat():
    # just reset everything, history stays in the DB/sidebar
    system_prompt = build_system_prompt(st.session_state.username, st.session_state.name)
    st.session_state.history = [SystemMessage(content=system_prompt)]
    st.session_state.active_thread = -1


def load_thread(pairs: list[tuple[str, str]], up_to_idx: int):
    # rebuild the conversation up to the clicked message so user can continue from there
    raw_for_thread = []
    for u, a in pairs[: up_to_idx + 1]:
        raw_for_thread.append({"role": "user", "content": u})
        if a:
            raw_for_thread.append({"role": "assistant", "content": a})

    system_prompt = build_system_prompt(st.session_state.username, st.session_state.name)
    st.session_state.history = build_history_as_messages(raw_for_thread, system_prompt)
    st.session_state.active_thread = up_to_idx


# Session State Defaults
# set up all the keys we need upfront so we don't get KeyErrors later
defaults = {
    "logged_in": False,
    "username": "",
    "password": "",
    "name": "",
    "history": [],
    "guest_history": [],
    "page": "chat",
    "raw_history": [],
    "active_thread": -1,  # -1 means new chat, anything else is an index into pairs
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# Guest Chat
def render_guest_chat():
    with st.sidebar:
        st.markdown("### Expense Tracker AI")
        st.caption("Your smart money companion")
        st.divider()
        if st.button("Login", type="primary", use_container_width=True):
            st.session_state.page = "login"
            st.rerun()
        if st.button("Register", type="secondary", use_container_width=True):
            st.session_state.page = "register"
            st.rerun()
        st.divider()
        st.caption("Login to unlock expense tracking, budgets, and spending insights.")

    st.title("Expense Tracker AI")
    st.caption("You are chatting as a guest. Login to save and track your expenses.")
    st.info("Expense tracking features require login. Feel free to chat on any topic!")

    # show whatever messages we have so far
    for msg in st.session_state.guest_history:
        if isinstance(msg, HumanMessage):
            with st.chat_message("user"):
                st.markdown(msg.content)
        elif isinstance(msg, AIMessage):
            if not getattr(msg, "tool_calls", None):
                with st.chat_message("assistant"):
                    st.markdown(extract_text(msg.content))

    user_text = st.chat_input("Ask me anything...")
    if user_text:
        with st.chat_message("user"):
            st.markdown(user_text)
        st.session_state.guest_history.append(HumanMessage(content=user_text))

        with st.spinner("Thinking..."):
            from langchain_groq import ChatGroq
            llm = ChatGroq(model="llama-3.3-70b-versatile")

            # simple system prompt for guests, nudges them to login for expense stuff
            guest_system = SystemMessage(content=(
                "You are a helpful AI assistant. Chat freely on any topic. "
                "If the user asks to track, save, or manage expenses, politely tell them "
                "they need to login first to use expense tracking features."
            ))
            response = llm.invoke([guest_system] + list(st.session_state.guest_history))

        st.session_state.guest_history.append(response)
        with st.chat_message("assistant"):
            st.markdown(extract_text(response.content))
        st.rerun()


# Login Page
def render_login():
    with st.sidebar:
        st.markdown("### Expense Tracker AI")
        st.divider()
        if st.button("Back to Chat", type="secondary", use_container_width=True):
            st.session_state.page = "chat"
            st.rerun()

    st.title("Login")
    st.caption("Welcome back — login to continue")
    st.write("")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", type="primary", use_container_width=True)

    if submitted:
        if not username or not password:
            st.error("Please fill in all fields.")
        else:
            with st.spinner("Logging in..."):
                result = run_async(mcp_login(username.strip().lower(), password))

            if "Login successful" in result or "Welcome back" in result:
                # try to pull the actual name from the response message
                name = username
                for line in result.split("\n"):
                    if "Welcome back" in line:
                        name = line.replace("Welcome back,", "").replace("!", "").strip()
                        break

                with st.spinner("Loading your history..."):
                    raw_history = run_async(
                        fetch_chat_history(username.strip().lower(), password, limit=200)
                    )

                st.session_state.logged_in = True
                st.session_state.username = username.strip().lower()
                st.session_state.password = password
                st.session_state.name = name
                st.session_state.raw_history = raw_history

                # start fresh, old chats are accessible via sidebar
                system_prompt = build_system_prompt(username.strip().lower(), name)
                st.session_state.history = [SystemMessage(content=system_prompt)]
                st.session_state.active_thread = -1

                st.session_state.page = "chat"
                st.rerun()
            else:
                st.error(result)

    st.write("")
    st.caption("Don't have an account?")
    if st.button("Create an account", type="secondary", use_container_width=True):
        st.session_state.page = "register"
        st.rerun()


# Register Page
def render_register():
    with st.sidebar:
        st.markdown("### Expense Tracker AI")
        st.divider()
        if st.button("Back to Chat", type="secondary", use_container_width=True):
            st.session_state.page = "chat"
            st.rerun()

    st.title("Create Account")
    st.caption("Sign up to start tracking your expenses")
    st.write("")

    with st.form("register_form"):
        full_name = st.text_input("Full Name")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        confirm = st.text_input("Confirm Password", type="password")
        submitted = st.form_submit_button("Create Account", type="primary", use_container_width=True)

    if submitted:
        if not full_name or not username or not password or not confirm:
            st.error("Please fill in all fields.")
        elif password != confirm:
            st.error("Passwords do not match.")
        elif len(password) < 6:
            st.error("Password must be at least 6 characters.")
        else:
            with st.spinner("Creating your account..."):
                result = run_async(mcp_register(full_name.strip(), username.strip().lower(), password))

            if "Welcome" in result and "created" in result:
                st.success("Account created successfully! Please login to continue.")
                st.session_state.page = "login"
                st.rerun()
            else:
                st.error(result)

    st.write("")
    st.caption("Already have an account?")
    if st.button("Login instead", type="secondary", use_container_width=True):
        st.session_state.page = "login"
        st.rerun()


# Logged-in Chat
def render_chat():
    raw = st.session_state.get("raw_history", [])
    pairs = raw_to_pairs(raw)
    active = st.session_state.get("active_thread", -1)

    with st.sidebar:
        st.markdown(f"### {st.session_state.name}")
        st.caption(f"@{st.session_state.username}")
        st.divider()

        # highlight the new chat button when we're actually in a new chat
        if active == -1:
            if st.button("✏️  New Chat", type="primary", use_container_width=True):
                start_new_chat()
                st.rerun()
        else:
            if st.button("✏️  New Chat", type="secondary", use_container_width=True):
                start_new_chat()
                st.rerun()

        if pairs:
            st.write("")
            st.caption("Previous chats")
            for idx, (user_msg, _) in enumerate(pairs):
                # truncate long messages so the sidebar doesn't get ugly
                preview = "💬  " + user_msg[:36] + ("…" if len(user_msg) > 36 else "")
                is_active = (active == idx)
                btn_type = "primary" if is_active else "secondary"
                if st.button(preview, key=f"thread_{idx}", type=btn_type, use_container_width=True):
                    load_thread(pairs, idx)
                    st.rerun()

        st.divider()
        if st.button("Logout", type="secondary", use_container_width=True):
            for key in ["logged_in", "username", "password", "name",
                        "history", "raw_history", "active_thread"]:
                st.session_state.pop(key, None)
            st.session_state.page = "chat"
            st.session_state.guest_history = []
            st.rerun()

    st.title("Expense Tracker AI")
    if active == -1:
        st.caption("Start a new conversation below.")
    else:
        st.caption("Viewing a previous thread — continue the conversation below.")

    # render the current conversation
    for msg in st.session_state.history:
        if isinstance(msg, HumanMessage):
            with st.chat_message("user"):
                st.markdown(msg.content)
        elif isinstance(msg, AIMessage):
            if not getattr(msg, "tool_calls", None):
                with st.chat_message("assistant"):
                    st.markdown(extract_text(msg.content))

    user_text = st.chat_input("Ask me anything or track an expense...")

    if user_text:
        with st.chat_message("user"):
            st.markdown(user_text)

        st.session_state.history.append(HumanMessage(content=user_text))

        with st.spinner("Thinking..."):
            new_messages = run_async(run_agent(
                st.session_state.history,
                st.session_state.username,
                st.session_state.password,
                st.session_state.name
            ))

        # add new messages to history and grab the last assistant reply for saving
        assistant_reply = ""
        for msg in new_messages:
            if msg not in st.session_state.history:
                st.session_state.history.append(msg)
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                assistant_reply = extract_text(msg.content)

        # show only the first real assistant message (skip tool call messages)
        for msg in new_messages:
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                with st.chat_message("assistant"):
                    st.markdown(extract_text(msg.content))
                break

        if assistant_reply:
            # save to DB so it shows up in sidebar next time
            run_async(save_chat_exchange_direct(
                st.session_state.username,
                st.session_state.password,
                user_text,
                assistant_reply
            ))

            # keep local raw_history in sync so we don't need a full page reload
            st.session_state.raw_history.append({"role": "user", "content": user_text})
            st.session_state.raw_history.append({"role": "assistant", "content": assistant_reply})

            # move active thread pointer to the latest exchange
            updated_pairs = raw_to_pairs(st.session_state.raw_history)
            st.session_state.active_thread = len(updated_pairs) - 1

        st.rerun()


# Router
# simple routing based on login state and which page we're on
if st.session_state.logged_in:
    render_chat()
elif st.session_state.page == "login":
    render_login()
elif st.session_state.page == "register":
    render_register()
else:
    render_guest_chat()