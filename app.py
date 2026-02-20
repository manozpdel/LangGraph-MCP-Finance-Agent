# app.py

import asyncio
import threading
import streamlit as st

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from agent_graph import run_agent, build_system_prompt, mcp_login, mcp_register


# ─────────────────────────────
# Page Config
# ─────────────────────────────
st.set_page_config(
    page_title="Expense Tracker AI",
    layout="centered"
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

#MainMenu, footer, header { visibility: hidden; }

.stApp {
    background: #ffffff;
    color: #111111;
}

.stTextInput > div > div > input {
    border: 1px solid #d1d5db !important;
    border-radius: 8px !important;
    padding: 0.6rem 0.9rem !important;
    font-size: 0.9rem !important;
    background: #ffffff !important;
    color: #111111 !important;
}
.stTextInput > div > div > input:focus {
    border-color: #111111 !important;
    box-shadow: none !important;
}
.stTextInput label {
    font-size: 0.82rem !important;
    font-weight: 500 !important;
    color: #374151 !important;
}

.stButton > button {
    border-radius: 8px !important;
    font-size: 0.9rem !important;
    font-weight: 500 !important;
    padding: 0.55rem 1.2rem !important;
    transition: all 0.15s;
}
.stButton > button[kind="primary"] {
    background: #111111 !important;
    color: #ffffff !important;
    border: none !important;
    width: 100%;
}
.stButton > button[kind="primary"]:hover {
    background: #333333 !important;
}
.stButton > button[kind="secondary"] {
    background: #ffffff !important;
    color: #111111 !important;
    border: 1px solid #d1d5db !important;
    width: 100%;
}
.stButton > button[kind="secondary"]:hover {
    background: #f9fafb !important;
}

[data-testid="stSidebar"] {
    background: #f9fafb !important;
    border-right: 1px solid #e5e7eb !important;
}

[data-testid="stChatMessage"] {
    border-radius: 10px !important;
    margin-bottom: 0.4rem;
}

[data-testid="stChatInput"] textarea {
    border: 1px solid #d1d5db !important;
    border-radius: 8px !important;
    font-size: 0.9rem !important;
}

.stAlert {
    border-radius: 8px !important;
    font-size: 0.87rem !important;
}

.stSpinner > div {
    border-top-color: #111111 !important;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────
# Helpers
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


def extract_text(content):
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return content


# ─────────────────────────────
# Session State Defaults
# ─────────────────────────────
defaults = {
    "logged_in": False,
    "username": "",
    "password": "",
    "name": "",
    "history": [],
    "guest_history": [],
    "page": "chat",
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────
# GUEST CHAT
# ─────────────────────────────
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
            from langchain_core.messages import SystemMessage as SM
            llm = ChatGroq(model="llama-3.3-70b-versatile")
            guest_system = SM(content=(
                "You are a helpful AI assistant. Chat freely on any topic. "
                "If the user asks to track, save, or manage expenses, politely tell them "
                "they need to login first to use expense tracking features."
            ))
            response = llm.invoke([guest_system] + list(st.session_state.guest_history))

        st.session_state.guest_history.append(response)
        with st.chat_message("assistant"):
            st.markdown(extract_text(response.content))
        st.rerun()


# ─────────────────────────────
# LOGIN PAGE
# ─────────────────────────────
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
                name = username
                for line in result.split("\n"):
                    if "Welcome back" in line:
                        name = line.replace("Welcome back,", "").replace("!", "").strip()
                        break

                st.session_state.logged_in = True
                st.session_state.username = username.strip().lower()
                st.session_state.password = password
                st.session_state.name = name
                st.session_state.history = [
                    SystemMessage(content=build_system_prompt(username.strip().lower(), name))
                ]
                st.session_state.page = "chat"
                st.rerun()
            else:
                st.error(result)

    st.write("")
    st.caption("Don't have an account?")
    if st.button("Create an account", type="secondary", use_container_width=True):
        st.session_state.page = "register"
        st.rerun()


# ─────────────────────────────
# REGISTER PAGE
# ─────────────────────────────
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


# ─────────────────────────────
# LOGGED-IN CHAT
# ─────────────────────────────
def render_chat():
    with st.sidebar:
        st.markdown(f"### {st.session_state.name}")
        st.caption(f"@{st.session_state.username}")
        st.divider()
        st.markdown("**What you can do:**")
        st.caption("""
- "I spent $50 on food"
- "Show my recent expenses"
- "Monthly summary"
- "Set budget to $500"
- "How is my budget?"
- "Show spending trend"
- "Add Netflix $15 on day 5"
- "Actually that was $75"
- Any general question
        """)
        st.divider()
        if st.button("Clear Chat", type="secondary", use_container_width=True):
            st.session_state.history = [
                SystemMessage(content=build_system_prompt(
                    st.session_state.username, st.session_state.name
                ))
            ]
            st.rerun()
        if st.button("Logout", type="secondary", use_container_width=True):
            for key in ["logged_in", "username", "password", "name", "history"]:
                st.session_state.pop(key, None)
            st.session_state.page = "chat"
            st.session_state.guest_history = []
            st.rerun()

    st.title(f"Hey, {st.session_state.name.split()[0]}!")
    st.caption("Chat freely or track your expenses.")

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

        for msg in new_messages:
            if msg not in st.session_state.history:
                st.session_state.history.append(msg)

        for msg in new_messages:
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                with st.chat_message("assistant"):
                    st.markdown(extract_text(msg.content))
                break

        st.rerun()


# ─────────────────────────────
# ROUTER
# ─────────────────────────────
if st.session_state.logged_in:
    render_chat()
elif st.session_state.page == "login":
    render_login()
elif st.session_state.page == "register":
    render_register()
else:
    render_guest_chat()