"""
app.py — Streamlit entrypoint: authentication, thread management, and chat UI.

Boot sequence:
  1. Check auth (stop if not logged in).
  2. Run drive_sync.boot_sync() once to pull workspace files from Google Drive.
  3. Spin up the Telegram bot in a background daemon thread (once per container).
  4. Render the chat UI.
"""

import asyncio
import json
import re
import threading
import time
import uuid
from urllib.parse import urlencode

import httpx
import streamlit as st

import capabilities as capabilities_module
import cron_service
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


@st.cache_resource
def _oauth_pending_store() -> dict[str, dict[str, str]]:
    """
    Shared pending OAuth records keyed by state so callback can recover even
    if browser session state is lost between redirect hops.
    """
    return {}


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
    _oauth_pending_store()[state] = {
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": ruri,
        "user_email": user_email.strip(),
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
    pending_state = str(pending.get("state", "")).strip() if isinstance(pending, dict) else ""
    store = _oauth_pending_store()
    fallback = store.get(state, {}) if state else {}

    if not isinstance(pending, dict) or not pending_state:
        if not isinstance(fallback, dict) or not fallback:
            st.session_state["google_oauth_notice"] = {
                "kind": "error",
                "text": "OAuth callback received, but no pending login request was found.",
            }
            _clear_oauth_query_params()
            st.rerun()
        pending = {
            "client_id": str(fallback.get("client_id", "")).strip(),
            "client_secret": str(fallback.get("client_secret", "")).strip(),
            "redirect_uri": str(fallback.get("redirect_uri", "")).strip(),
            "user_email": str(fallback.get("user_email", "")).strip(),
            "state": state,
        }
        st.session_state["google_oauth_pending"] = pending
        pending_state = state

    if not pending_state or state != pending_state:
        if not isinstance(fallback, dict) or not fallback:
            st.session_state["google_oauth_notice"] = {
                "kind": "error",
                "text": "OAuth state mismatch. Start the login flow again.",
            }
            st.session_state.pop("google_oauth_pending", None)
            _clear_oauth_query_params()
            st.rerun()
        pending = {
            "client_id": str(fallback.get("client_id", "")).strip(),
            "client_secret": str(fallback.get("client_secret", "")).strip(),
            "redirect_uri": str(fallback.get("redirect_uri", "")).strip(),
            "user_email": str(fallback.get("user_email", "")).strip(),
            "state": state,
        }
        st.session_state["google_oauth_pending"] = pending

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
            if state:
                _oauth_pending_store().pop(state, None)
            _clear_oauth_query_params()
            st.rerun()
    except Exception as exc:
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": f"OAuth token exchange error: {exc}",
        }
        st.session_state.pop("google_oauth_pending", None)
        if state:
            _oauth_pending_store().pop(state, None)
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
        if state:
            _oauth_pending_store().pop(state, None)
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
    bind_result = gworkspace_module.google_workspace_set_runtime_oauth(
        client_id=runtime_cfg["client_id"],
        client_secret=runtime_cfg["client_secret"],
        refresh_token=runtime_cfg["refresh_token"],
        user_email=runtime_cfg["user_email"],
        token_uri=runtime_cfg["token_uri"],
    )
    if "set" not in bind_result.lower():
        st.session_state["google_oauth_notice"] = {
            "kind": "error",
            "text": f"OAuth connected, but runtime binding failed: {bind_result}",
        }
        st.session_state.pop("google_oauth_pending", None)
        if state:
            _oauth_pending_store().pop(state, None)
        _clear_oauth_query_params()
        st.rerun()

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
        "text": "Google OAuth connected and bound to Google Workspace tools for this runtime.",
    }
    st.session_state.pop("google_oauth_pending", None)
    if state:
        _oauth_pending_store().pop(state, None)
    st.session_state.pop("google_oauth_diag", None)
    _clear_oauth_query_params()
    st.rerun()


def _render_google_oauth_panel() -> None:
    oauth_cfg = dict(st.secrets.get("google_oauth", {}))

    identity_text = gworkspace_module.google_workspace_identity()
    connected = identity_text.startswith("Mode: OAuth user credentials")
    st.write(f"Google OAuth: {'connected' if connected else 'not connected'}")
    with st.expander("Active Google Identity", expanded=False):
        st.code(identity_text, language="text")

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
        placeholder="https://<your-app>.streamlit.app/",
    )
    if not redirect_uri.strip():
        st.warning("OAuth redirect URI is required to start the web flow.")

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

    c1, c2 = st.columns(2)
    if c1.button("Verify Workspace Access", use_container_width=True):
        diag = gworkspace_module.google_workspace_oauth_diagnostics()
        st.session_state["google_oauth_diag"] = diag
        if "gmail_profile_ok=True" in diag:
            st.session_state["google_oauth_notice"] = {
                "kind": "success",
                "text": "Workspace verification succeeded (Gmail profile is reachable).",
            }
        else:
            st.session_state["google_oauth_notice"] = {
                "kind": "error",
                "text": "Workspace verification failed. Open diagnostics below.",
            }
        st.rerun()

    if c2.button("Disconnect Runtime OAuth", use_container_width=True):
        gworkspace_module.google_workspace_clear_runtime_oauth()
        st.session_state.pop("google_oauth_runtime", None)
        st.session_state.pop("google_oauth_diag", None)
        st.session_state["google_oauth_notice"] = {
            "kind": "success",
            "text": "Runtime OAuth disconnected.",
        }
        st.rerun()

    diag_text = str(st.session_state.get("google_oauth_diag", "")).strip()
    if diag_text:
        with st.expander("OAuth Diagnostics", expanded=True):
            st.code(diag_text, language="text")


def _extract_tool_names(messages: list[dict]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    for msg in messages:
        if msg.get("role") != "tool":
            continue
        name = str(msg.get("name", "")).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if names:
        return names

    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = str(fn.get("name", "")).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _capture_latest_run(
    session: Session,
    start_index: int,
    user_prompt: str,
    response: str,
    origin: str,
) -> None:
    new_entries = session.get_messages_since(start_index)
    st.session_state["latest_agent_run"] = {
        "run_id": str(uuid.uuid4())[:10],
        "origin": origin,
        "user_prompt": user_prompt,
        "response": response,
        "tool_names": _extract_tool_names(new_entries),
        "created_at": time.time(),
    }


def _default_capability_name(prompt: str) -> str:
    tokens = [tok for tok in re.sub(r"[^a-zA-Z0-9\s]", " ", prompt or "").split() if tok.strip()]
    if not tokens:
        return "New Capability"
    return " ".join(tokens[:6]).title()


def _parse_defaults_json(raw: str) -> tuple[dict[str, str], str]:
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return {}, f"Invalid defaults JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, "Defaults must be a JSON object, for example: {\"project\": \"nanobot\"}"
    return {str(k): str(v) for k, v in parsed.items()}, ""


def _render_capabilities_panel(session_id: str) -> None:
    latest = st.session_state.get("latest_agent_run", {})
    if isinstance(latest, dict) and latest:
        latest_run_id = str(latest.get("run_id", "")).strip()
        if latest_run_id and st.session_state.get("cap_draft_source_run_id") != latest_run_id:
            st.session_state["cap_draft_source_run_id"] = latest_run_id
            st.session_state["cap_draft_name"] = _default_capability_name(str(latest.get("user_prompt", "")))
            st.session_state["cap_draft_template"] = str(latest.get("user_prompt", "")).strip()
            st.session_state["cap_draft_description"] = f"Promoted from {latest.get('origin', 'chat')}."
            st.session_state["cap_draft_defaults"] = "{}"

        st.caption(f"Latest run source: `{latest.get('origin', 'chat')}`")
        tools_used = latest.get("tool_names", [])
        if isinstance(tools_used, list) and tools_used:
            st.caption("Tools used: " + ", ".join(str(x) for x in tools_used))
        st.caption("Latest prompt")
        st.code(str(latest.get("user_prompt", "")), language="text")
    else:
        st.caption("Run a task in chat first, then promote it to a reusable capability.")

    st.markdown("**Promote to Capability**")
    st.text_input("Capability name", key="cap_draft_name")
    st.text_area(
        "Prompt template (`{{variable}}` supported)",
        key="cap_draft_template",
        height=120,
    )
    st.text_input("Description (optional)", key="cap_draft_description")
    st.text_area(
        "Default variables JSON (optional)",
        key="cap_draft_defaults",
        height=70,
        help='Example: {"project":"nanobot","minutes":"30"}',
    )
    if st.button("Save Capability", use_container_width=True):
        name = str(st.session_state.get("cap_draft_name", "")).strip()
        template = str(st.session_state.get("cap_draft_template", "")).strip()
        description = str(st.session_state.get("cap_draft_description", "")).strip()
        defaults_raw = str(st.session_state.get("cap_draft_defaults", "")).strip()
        defaults, err = _parse_defaults_json(defaults_raw)
        if err:
            st.session_state["cap_notice"] = {"kind": "error", "text": err}
            st.rerun()
        if not name or not template:
            st.session_state["cap_notice"] = {
                "kind": "error",
                "text": "Capability name and prompt template are required.",
            }
            st.rerun()

        source_prompt = ""
        source_tools: list[str] = []
        if isinstance(latest, dict):
            source_prompt = str(latest.get("user_prompt", "")).strip()
            source_tools = [str(x) for x in latest.get("tool_names", []) or []]

        saved = capabilities_module.create_capability(
            name=name,
            template=template,
            description=description,
            defaults=defaults,
            source_prompt=source_prompt,
            source_tools=source_tools,
        )
        st.session_state["cap_notice"] = {
            "kind": "success",
            "text": f"Saved capability: {saved.get('name', 'Unnamed')} ({saved.get('id', '')})",
        }
        st.rerun()

    notice = st.session_state.get("cap_notice")
    if isinstance(notice, dict) and notice.get("text"):
        if notice.get("kind") == "success":
            st.success(str(notice.get("text", "")))
        else:
            st.error(str(notice.get("text", "")))

    capabilities = capabilities_module.list_capabilities()
    if not capabilities:
        st.info("No capabilities saved yet.")
        return

    st.divider()
    st.markdown("**Run Capability**")
    by_id = {
        str(item.get("id", "")).strip(): item
        for item in capabilities
        if str(item.get("id", "")).strip()
    }
    capability_ids = list(by_id.keys())
    selected_state = str(st.session_state.get("cap_selected_id", "")).strip()
    if selected_state and selected_state not in capability_ids:
        st.session_state["cap_selected_id"] = capability_ids[0]
    selected_id = st.selectbox(
        "Choose capability",
        options=capability_ids,
        format_func=lambda cid: f"{by_id[cid].get('name', 'Unnamed')} ({cid})",
        key="cap_selected_id",
    )
    selected = by_id[selected_id]
    template = str(selected.get("template", "")).strip()
    defaults_raw = selected.get("defaults", {})
    defaults = dict(defaults_raw) if isinstance(defaults_raw, dict) else {}
    variables = capabilities_module.template_vars(template)

    values: dict[str, str] = {}
    for var in variables:
        val = st.text_input(
            f"Variable: {var}",
            value=str(defaults.get(var, "")),
            key=f"cap_var_{selected_id}_{var}",
        )
        values[var] = val

    merged = dict(defaults)
    merged.update({k: v for k, v in values.items() if str(v).strip()})
    rendered_prompt, missing = capabilities_module.render_template(template, merged)

    c1, c2 = st.columns(2)
    if c1.button("Dry Run", use_container_width=True, key=f"cap_dry_{selected_id}"):
        st.session_state["cap_preview"] = {
            "id": selected_id,
            "prompt": rendered_prompt,
            "missing": missing,
        }
        st.rerun()
    if c2.button("Prepare Run", use_container_width=True, key=f"cap_prepare_{selected_id}"):
        if missing:
            st.session_state["cap_notice"] = {
                "kind": "error",
                "text": f"Missing variables: {', '.join(missing)}",
            }
            st.rerun()
        st.session_state["cap_pending_run"] = {
            "id": selected_id,
            "name": str(selected.get("name", "Unnamed")),
            "prompt": rendered_prompt,
        }
        st.rerun()

    preview = st.session_state.get("cap_preview", {})
    if isinstance(preview, dict) and preview.get("id") == selected_id:
        preview_missing = preview.get("missing", [])
        if isinstance(preview_missing, list) and preview_missing:
            st.warning("Missing variables: " + ", ".join(str(x) for x in preview_missing))
        st.code(str(preview.get("prompt", "")), language="text")

    pending = st.session_state.get("cap_pending_run", {})
    if isinstance(pending, dict) and pending.get("id") == selected_id:
        st.warning("Run is prepared. Confirm to execute this capability.")
        st.code(str(pending.get("prompt", "")), language="text")
        if st.button("Confirm Run", use_container_width=True, key=f"cap_confirm_{selected_id}"):
            prompt = str(pending.get("prompt", "")).strip()
            if not prompt:
                st.session_state["cap_notice"] = {
                    "kind": "error",
                    "text": "Prepared prompt is empty.",
                }
                st.rerun()

            with st.spinner("Running capability..."):
                session: Session = st.session_state["session"]
                start_index = len(session)
                agent = Agent(session)
                response = asyncio.run(agent.run(prompt))

            st.session_state["messages"].append({"role": "user", "content": prompt})
            st.session_state["messages"].append({"role": "assistant", "content": response})
            _capture_latest_run(
                session=st.session_state["session"],
                start_index=start_index,
                user_prompt=prompt,
                response=response,
                origin=f"capability:{pending.get('name', 'Unnamed')}",
            )
            st.session_state["cap_notice"] = {
                "kind": "success",
                "text": f"Executed capability: {pending.get('name', 'Unnamed')}",
            }
            st.session_state.pop("cap_pending_run", None)
            st.rerun()

    with st.expander("Schedule Capability (Optional)", expanded=False):
        interval_minutes = int(
            st.number_input(
                "Every N minutes",
                min_value=1,
                value=60,
                step=1,
                key=f"cap_schedule_interval_{selected_id}",
            )
        )
        default_task_name = f"Capability: {selected.get('name', 'Unnamed')}"
        task_name = st.text_input(
            "Task name",
            value=default_task_name,
            key=f"cap_schedule_name_{selected_id}",
        )
        if st.button("Create Schedule", use_container_width=True, key=f"cap_schedule_create_{selected_id}"):
            scheduled_prompt, missing_sched = capabilities_module.render_template(template, merged)
            if missing_sched:
                st.session_state["cap_notice"] = {
                    "kind": "error",
                    "text": f"Cannot schedule with missing variables: {', '.join(missing_sched)}",
                }
                st.rerun()
            task = cron_service.create_task(
                name=task_name.strip() or default_task_name,
                prompt=scheduled_prompt,
                interval_minutes=interval_minutes,
                session_id=session_id,
            )
            st.session_state["cap_notice"] = {
                "kind": "success",
                "text": f"Scheduled task created: {task.get('id', '')}",
            }
            st.rerun()

    if st.button("Delete Capability", use_container_width=True, key=f"cap_delete_{selected_id}"):
        ok = capabilities_module.delete_capability(selected_id)
        st.session_state["cap_notice"] = {
            "kind": "success" if ok else "error",
            "text": (
                f"Deleted capability: {selected.get('name', 'Unnamed')}"
                if ok
                else "Capability not found; nothing deleted."
            ),
        }
        st.session_state.pop("cap_pending_run", None)
        st.rerun()


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
                try:
                    gworkspace_module.google_workspace_clear_runtime_oauth()
                except Exception:
                    pass
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

        with st.expander("Capabilities", expanded=False):
            _render_capabilities_panel(session_id)

# Render existing conversation
for msg in st.session_state["messages"]:
    role = msg["role"]
    content = msg.get("content") or ""
    if role in ("user", "assistant") and content:
        with st.chat_message(role):
            st.markdown(content)

# Chat input
if prompt := st.chat_input("Message Nanobot…"):
    runtime_cfg = st.session_state.get("google_oauth_runtime", {})
    if isinstance(runtime_cfg, dict) and str(runtime_cfg.get("refresh_token", "")).strip():
        try:
            gworkspace_module.google_workspace_set_runtime_oauth(
                client_id=str(runtime_cfg.get("client_id", "")),
                client_secret=str(runtime_cfg.get("client_secret", "")),
                refresh_token=str(runtime_cfg.get("refresh_token", "")),
                user_email=str(runtime_cfg.get("user_email", "")),
                token_uri=str(runtime_cfg.get("token_uri", _GOOGLE_TOKEN_ENDPOINT)),
            )
        except Exception:
            pass

    session: Session = st.session_state["session"]
    start_index = len(session)

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
            agent = Agent(session)
            response = asyncio.run(agent.run(prompt, on_event=_on_progress))

        progress_box.empty()

        st.markdown(response)

    st.session_state["messages"].append({"role": "assistant", "content": response})
    _capture_latest_run(
        session=session,
        start_index=start_index,
        user_prompt=prompt,
        response=response,
        origin="chat",
    )
