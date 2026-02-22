"""
app.py — Streamlit entrypoint: authentication, thread management, and chat UI.

Boot sequence:
  1. Check auth (stop if not logged in).
  2. Run drive_sync.boot_sync() once to pull workspace files from Google Drive.
  3. Spin up the Telegram bot in a background daemon thread (once per container).
  4. Render the chat UI.
"""

import asyncio
import threading

import streamlit as st

import drive_sync
from agent import Agent
from session import Session
import tools as tools_module

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Nanobot",
    page_icon="🤖",
    layout="centered",
)


# ---------------------------------------------------------------------------
# 1. Authentication
# ---------------------------------------------------------------------------

def _check_auth() -> bool:
    """Show login form and return True only when credentials are valid."""
    if st.session_state.get("authenticated"):
        return True

    st.title("Nanobot — Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

    if submitted:
        expected_user = st.secrets["auth"]["username"]
        expected_pass = st.secrets["auth"]["password"]
        if username == expected_user and password == expected_pass:
            st.session_state["authenticated"] = True
            st.session_state["auth_user"] = username
            st.rerun()
        else:
            st.error("Invalid username or password.")

    return False


if not _check_auth():
    st.stop()


# ---------------------------------------------------------------------------
# 2. Boot sync (once per container, not per Streamlit re-run)
# ---------------------------------------------------------------------------

@st.cache_resource
def _boot() -> bool:
    """
    Download workspace files from Google Drive and start the Telegram bot.
    @st.cache_resource ensures this runs exactly once per container lifecycle.
    Returns True on success.
    """
    # Pull AGENTS.md, USER.md, MEMORY.md, HISTORY.md from Drive
    try:
        drive_sync.boot_sync()
    except Exception as exc:
        # Non-fatal — app can still run without Drive on first boot
        st.warning(f"Drive sync warning: {exc}")

    # Sync any skills stored in Drive
    try:
        import skills as skills_module
        skills_module.sync_skills_from_drive()
    except Exception:
        pass

    # Run due cron tasks on boot (optional)
    try:
        system_cfg = dict(st.secrets.get("system", {}))
        run_on_boot = bool(system_cfg.get("cron_run_on_boot", True))
        if run_on_boot:
            import cron_service

            cron_service.run_due_tasks_sync(limit=3)
    except Exception as exc:
        st.warning(f"Cron runner warning: {exc}")

    # Start Telegram bot in a background daemon thread (optional)
    try:
        tg = dict(st.secrets.get("telegram", {}))
        tg_enabled = bool(tg.get("enabled", True))
        tg_token = str(tg.get("token", "")).strip()
        if tg_enabled and tg_token:
            import telegram_bot

            t = threading.Thread(target=telegram_bot.run_bot, daemon=True, name="telegram-bot")
            t.start()
    except Exception as exc:
        # Telegram failures shouldn't crash the web UI
        st.warning(f"Telegram bot failed to start: {exc}")

    return True


_boot()


# ---------------------------------------------------------------------------
# 3. Chat UI
# ---------------------------------------------------------------------------

st.title("🤖 Nanobot")
st.caption("Your personal AI assistant — memories persist across sessions via Google Drive.")

# Initialise per-browser-tab session
username: str = st.session_state.get("auth_user", "web")
session_id = f"web_{username}"

if "session" not in st.session_state:
    st.session_state["session"] = Session(session_id)

if "messages" not in st.session_state:
    # Load chat history from the session for display
    loaded = st.session_state["session"].get_messages()
    st.session_state["messages"] = [
        m for m in loaded if m.get("role") in ("user", "assistant")
    ]

# Sidebar controls
with st.sidebar:
    st.header("Session")
    st.write(f"Logged in as **{username}**")
    st.write(f"Session: `{session_id}`")
    if st.button("Clear conversation"):
        st.session_state["session"].clear()
        st.session_state["messages"] = []
        st.rerun()
    if st.button("Logout"):
        st.session_state.clear()
        st.rerun()

    st.divider()
    st.header("Memory")
    import memory as mem_module
    if st.button("Show MEMORY.md"):
        st.text_area("MEMORY.md", mem_module.read_memory(), height=300)

    st.divider()
    st.header("Integrations")
    tg = dict(st.secrets.get("telegram", {}))
    tg_enabled = bool(tg.get("enabled", True))
    tg_configured = bool(str(tg.get("token", "")).strip())
    if tg_enabled and tg_configured:
        st.write("Telegram: configured")
    elif tg_enabled and not tg_configured:
        st.write("Telegram: enabled but missing token")
    else:
        st.write("Telegram: disabled")

# Render existing conversation
for msg in st.session_state["messages"]:
    role = msg["role"]
    content = msg.get("content") or ""
    if role in ("user", "assistant") and content:
        with st.chat_message(role):
            st.markdown(content)

# Chat input
if prompt := st.chat_input("Message Nanobot…"):
    # Display user message immediately
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state["messages"].append({"role": "user", "content": prompt})

    # Run agent
    with st.chat_message("assistant"):
        progress_box = st.empty()
        progress_lines: list[str] = []

        def _on_progress(event: str) -> None:
            progress_lines.append(event)
            tail = progress_lines[-8:]
            progress_box.markdown("**Progress**\n" + "\n".join(f"- {x}" for x in tail))

        with st.spinner("Thinking…"):
            session: Session = st.session_state["session"]
            agent = Agent(session)
            response = asyncio.run(agent.run(prompt, on_event=_on_progress))

        progress_box.empty()

        st.markdown(response)

    st.session_state["messages"].append({"role": "assistant", "content": response})


st.divider()
with st.expander("Python Code Executor (Unsafe)"):
    st.caption("Approved fast-path executor. Runs code directly in-process. Use only in trusted environments.")
    exec_code = st.text_area(
        "Enter Python code:",
        height=220,
        key="unsafe_python_executor_input",
        value="print('Hello from Nanobot executor')",
    )
    if st.button("Execute Code", key="unsafe_python_execute_button"):
        output = tools_module.python_exec_unsafe(exec_code)
        st.code(output, language="text")
