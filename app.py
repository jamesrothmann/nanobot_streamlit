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
import time
import uuid
from urllib.parse import urlencode

import httpx
import streamlit as st

import drive_sync
import gworkspace as gworkspace_module
import memory as mem_module
from agent import Agent
from session import Session

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Nanobot",
    page_icon="🤖",
    layout="centered",
    initial_sidebar_state="collapsed",
)

_GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _query_param(key: str) -> str:
    value = st.query_params.get(key, "")
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def _clear_oauth_query_params() -> None:
    for key in ("code", "state", "scope", "error", "error_description", "prompt"):
        if key in st.query_params:
            del st.query_params[key]


def _start_google_oauth_web_flow(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    user_email: str,
) -> tuple[bool, str]:
    cid = client_id.strip()
    csec = client_secret.strip()
    ruri = redirect_uri.strip()

    if not cid or not csec or not ruri:
        return False, "Client ID, client secret, and redirect URI are required."

    state = uuid.uuid4().hex
    st.session_state["google_oauth_pending"] = {
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": ruri,
        "user_email": user_email.strip(),
        "state": state,
        "created_at": time.time(),
    }
    params = {
        "client_id": cid,
        "redirect_uri": ruri,
        "response_type": "code",
        "scope": " ".join(_GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    st.session_state["google_oauth_auth_url"] = f"{_GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"
    return True, "Google OAuth started. Open the consent screen to continue."


def _handle_google_oauth_callback() -> None:
    code = _query_param("code")
    state = _query_param("state")
    oauth_error = _query_param("error")

    if oauth_error:
        error_desc = _query_param("error_description")
        detail = f" ({error_desc})" if error_desc else ""
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": f"OAuth failed: {oauth_error}{detail}",
        }
        _clear_oauth_query_params()
        st.rerun()

    if not code:
        return

    pending = st.session_state.get("google_oauth_pending")
    if not isinstance(pending, dict):
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": "OAuth callback received, but no pending login request was found.",
        }
        _clear_oauth_query_params()
        st.rerun()

    expected_state = str(pending.get("state", "")).strip()
    if not expected_state or state != expected_state:
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": "OAuth state mismatch. Start the login flow again.",
        }
        st.session_state.pop("google_oauth_pending", None)
        _clear_oauth_query_params()
        st.rerun()

    payload = {
        "client_id": str(pending.get("client_id", "")).strip(),
        "client_secret": str(pending.get("client_secret", "")).strip(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": str(pending.get("redirect_uri", "")).strip(),
    }

    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.post(_GOOGLE_TOKEN_ENDPOINT, data=payload)
        if resp.status_code >= 400:
            st.session_state["google_oauth_notice"] = {
                "kind": "error",
                "text": f"OAuth token exchange failed ({resp.status_code}): {resp.text}",
            }
            st.session_state.pop("google_oauth_pending", None)
            _clear_oauth_query_params()
            st.rerun()
    except Exception as exc:
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": f"OAuth token exchange error: {exc}",
        }
        st.session_state.pop("google_oauth_pending", None)
        _clear_oauth_query_params()
        st.rerun()

    token_data = resp.json()
    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": (
                "No refresh token returned. Revoke existing app access in your Google account, "
                "then retry and approve consent again."
            ),
        }
        st.session_state.pop("google_oauth_pending", None)
        _clear_oauth_query_params()
        st.rerun()

    runtime_cfg = {
        "enabled": True,
        "client_id": str(pending.get("client_id", "")).strip(),
        "client_secret": str(pending.get("client_secret", "")).strip(),
        "refresh_token": refresh_token,
        "token_uri": _GOOGLE_TOKEN_ENDPOINT,
        "user_email": str(pending.get("user_email", "")).strip(),
    }
    st.session_state["google_oauth_runtime"] = runtime_cfg
    lines = [
        "[google_oauth]",
        "enabled = true",
        f'client_id = "{runtime_cfg["client_id"]}"',
        f'client_secret = "{runtime_cfg["client_secret"]}"',
        f'refresh_token = "{runtime_cfg["refresh_token"]}"',
        f'token_uri = "{runtime_cfg["token_uri"]}"',
    ]
    if runtime_cfg["user_email"]:
        lines.append(f'user_email = "{runtime_cfg["user_email"]}"')
    st.session_state["google_oauth_secrets_block"] = "\n".join(lines)
    st.session_state["google_oauth_notice"] = {
        "kind": "success",
        "text": "Google OAuth connected. Runtime credentials are active for this session.",
    }
    st.session_state.pop("google_oauth_pending", None)
    try:
        gworkspace_module.google_workspace_clear_cached_services()
    except Exception:
        pass
    _clear_oauth_query_params()
    st.rerun()


def _render_google_oauth_panel() -> None:
    oauth_cfg = dict(st.secrets.get("google_oauth", {}))
    runtime_cfg = st.session_state.get("google_oauth_runtime", {})

    connected = False
    if isinstance(runtime_cfg, dict):
        connected = bool(str(runtime_cfg.get("refresh_token", "")).strip())
    if not connected:
        required = ("enabled", "client_id", "client_secret", "refresh_token")
        connected = (
            bool(oauth_cfg.get("enabled", False))
            and all(str(oauth_cfg.get(key, "")).strip() for key in required[1:])
        )

    st.write(f"Google OAuth: {'connected' if connected else 'not connected'}")
    notice = st.session_state.get("google_oauth_notice")
    if isinstance(notice, dict) and notice.get("text"):
        text = str(notice["text"])
        if notice.get("kind") == "success":
            st.success(text)
        else:
            st.error(text)

    default_client_id = str(oauth_cfg.get("client_id", "")).strip()
    default_client_secret = str(oauth_cfg.get("client_secret", "")).strip()
    default_user_email = str(oauth_cfg.get("user_email", "")).strip()
    default_redirect_uri = str(oauth_cfg.get("redirect_uri", "")).strip()

    user_email = st.text_input("Google user email (optional)", value=default_user_email, key="oauth_user_email")
    client_id = st.text_input("OAuth web client ID", value=default_client_id, key="oauth_client_id")
    client_secret = st.text_input(
        "OAuth web client secret",
        value=default_client_secret,
        type="password",
        key="oauth_client_secret",
    )
    redirect_uri = st.text_input(
        "OAuth redirect URI",
        value=default_redirect_uri,
        help="Must exactly match an authorized redirect URI in your Google OAuth web client.",
        key="oauth_redirect_uri",
    )
    if st.button("Start Google OAuth", use_container_width=True):
        ok, message = _start_google_oauth_web_flow(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            user_email=user_email,
        )
        st.session_state["google_oauth_notice"] = {
            "kind": "success" if ok else "error",
            "text": message,
        }
        st.rerun()

    auth_url = str(st.session_state.get("google_oauth_auth_url", "")).strip()
    pending = st.session_state.get("google_oauth_pending")
    if isinstance(pending, dict) and auth_url:
        st.link_button("Open Google Consent Screen", auth_url, use_container_width=True)
        st.caption("After approving access, you'll be redirected back and connected automatically.")

    secrets_block = str(st.session_state.get("google_oauth_secrets_block", "")).strip()
    if secrets_block:
        st.caption("Persist this by pasting into Streamlit secrets:")
        st.code(secrets_block, language="toml")


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
_handle_google_oauth_callback()


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
    panels_open = st.toggle("Show sidebar panels", value=True, key="sidebar_panels_open")
    st.caption("Use the arrow in the top-left corner to collapse/expand the full sidebar.")

    if panels_open:
        with st.expander("Session", expanded=True):
            st.write(f"Logged in as **{username}**")
            st.write(f"Session: `{session_id}`")
            if st.button("Clear conversation", use_container_width=True):
                st.session_state["session"].clear()
                st.session_state["messages"] = []
                st.rerun()
            if st.button("Logout", use_container_width=True):
                st.session_state.clear()
                st.rerun()

        with st.expander("Memory", expanded=False):
            if st.button("Show MEMORY.md", use_container_width=True):
                st.text_area("MEMORY.md", mem_module.read_memory(), height=300)

        with st.expander("Integrations", expanded=True):
            tg = dict(st.secrets.get("telegram", {}))
            tg_enabled = bool(tg.get("enabled", True))
            tg_configured = bool(str(tg.get("token", "")).strip())
            if tg_enabled and tg_configured:
                st.write("Telegram: configured")
            elif tg_enabled and not tg_configured:
                st.write("Telegram: enabled but missing token")
            else:
                st.write("Telegram: disabled")
            st.divider()
            st.subheader("Google OAuth")
            _render_google_oauth_panel()

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
