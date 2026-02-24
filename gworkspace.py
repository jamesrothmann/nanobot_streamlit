"""
gworkspace.py - Google Workspace tools (Gmail, Calendar, Drive, Docs, Sheets).

These public functions are auto-discovered by agent.py and exposed to the LLM
as callable tools. Keep signatures simple and docstrings clear because they are
converted into tool schemas at runtime.

Delegation model:
- Preferred: OAuth user credentials in `[google_oauth]` (desktop flow bootstrap
  once, then persistent refresh token in secrets).
- Fallback: service account with optional domain-wide delegation.
"""

from __future__ import annotations

import base64
import email.mime.text
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2 import credentials as oauth_credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]
_DOCS_SCOPES = [
    "https://www.googleapis.com/auth/documents",
]
_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]
_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
]

_OAUTH_ONBOARDING_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
_DEVICE_CODE_ENDPOINT = "https://oauth2.googleapis.com/device/code"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_OAUTH_PENDING: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Auth + service builders
# ---------------------------------------------------------------------------

def _service_account_info() -> dict[str, Any]:
    raw = st.secrets.get("gcp_service_account", {})
    return dict(raw) if isinstance(raw, dict) else {}


def _google_oauth_info() -> dict[str, Any]:
    raw = st.secrets.get("google_oauth", {})
    cfg = dict(raw) if isinstance(raw, dict) else {}

    # Runtime overrides let the sidebar OAuth flow activate immediately
    # without requiring a redeploy before testing integrations.
    runtime_cfg: dict[str, Any] = {}
    try:
        runtime_raw = st.session_state.get("google_oauth_runtime", {})
        runtime_cfg = dict(runtime_raw) if isinstance(runtime_raw, dict) else {}
    except Exception:
        runtime_cfg = {}
    if runtime_cfg:
        cfg.update(runtime_cfg)
    return cfg


def _oauth_enabled() -> bool:
    cfg = _google_oauth_info()
    if not bool(cfg.get("enabled", False)):
        return False
    required = ("client_id", "client_secret", "refresh_token")
    return all(str(cfg.get(k, "")).strip() for k in required)


def _build_oauth_creds(scopes: list[str]) -> oauth_credentials.Credentials:
    cfg = _google_oauth_info()
    user_info: dict[str, str] = {
        "type": "authorized_user",
        "client_id": str(cfg.get("client_id", "")).strip(),
        "client_secret": str(cfg.get("client_secret", "")).strip(),
        "refresh_token": str(cfg.get("refresh_token", "")).strip(),
        "token_uri": str(cfg.get("token_uri", "https://oauth2.googleapis.com/token")).strip(),
    }
    access_token = str(cfg.get("access_token", "")).strip()
    if access_token:
        user_info["token"] = access_token

    creds = oauth_credentials.Credentials.from_authorized_user_info(user_info, scopes=scopes)
    if not creds.valid:
        creds.refresh(Request())
    return creds


def _delegated_user() -> str:
    info = _service_account_info()
    delegated = str(info.get("delegated_user", "")).strip()
    if delegated:
        return delegated
    try:
        delegated = str(st.secrets.get("system", {}).get("google_workspace_user", "")).strip()
    except Exception:
        delegated = ""
    return delegated


def _build_creds(scopes: list[str]) -> Any:
    if _oauth_enabled():
        return _build_oauth_creds(scopes)

    info = _service_account_info()
    if not info:
        raise ValueError(
            "No Google credentials configured. Set [google_oauth] or [gcp_service_account] in secrets."
        )
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    delegated = _delegated_user()
    if delegated:
        creds = creds.with_subject(delegated)
    return creds


@st.cache_resource
def _gmail_service():
    creds = _build_creds(_GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


@st.cache_resource
def _calendar_service():
    creds = _build_creds(_CALENDAR_SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


@st.cache_resource
def _docs_service():
    creds = _build_creds(_DOCS_SCOPES)
    return build("docs", "v1", credentials=creds, cache_discovery=False)


@st.cache_resource
def _sheets_service():
    creds = _build_creds(_SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


@st.cache_resource
def _drive_service():
    creds = _build_creds(_DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def google_workspace_clear_cached_services() -> str:
    """
    Clear cached Google API clients so auth changes take effect immediately.
    """
    for service_fn in (_gmail_service, _calendar_service, _docs_service, _sheets_service, _drive_service):
        try:
            service_fn.clear()
        except Exception:
            pass
    return "Google Workspace service cache cleared."


def google_workspace_identity() -> str:
    """
    Return the active Google identity context (service account + delegated user).

    Use this to diagnose access issues and verify impersonation mode.
    """
    if _oauth_enabled():
        oauth_cfg = _google_oauth_info()
        user_hint = str(oauth_cfg.get("user_email", "")).strip() or "(not set)"
        return (
            "Mode: OAuth user credentials\n"
            f"User hint: {user_hint}\n"
            "Auth source: [google_oauth] in secrets\n"
            "Behavior: Works as your user account across Gmail/Calendar/Drive/Docs/Sheets."
        )

    info = _service_account_info()
    sa_email = str(info.get("client_email", ""))
    delegated = _delegated_user()
    if delegated:
        return (
            f"Service account: {sa_email}\n"
            f"Delegated user: {delegated}\n"
            "Mode: Domain-wide delegation (impersonation)"
        )
    return (
        f"Service account: {sa_email or '(missing)'}\n"
        "Delegated user: (not configured)\n"
        "Mode: Shared-resource only (share files/calendars/mailbox with service account)"
    )


# ---------------------------------------------------------------------------
# OAuth onboarding (chat-driven, no local script required)
# ---------------------------------------------------------------------------

def _oauth_scope_string() -> str:
    return " ".join(_OAUTH_ONBOARDING_SCOPES)


def _oauth_client_credentials(client_id: str, client_secret: str) -> tuple[str, str]:
    cfg = _google_oauth_info()
    cid = client_id.strip() or str(cfg.get("client_id", "")).strip()
    csec = client_secret.strip() or str(cfg.get("client_secret", "")).strip()
    return cid, csec


def google_oauth_onboarding_start(
    client_id: str = "",
    client_secret: str = "",
    user_email: str = "",
) -> str:
    """
    Start one-time Google OAuth onboarding using the device flow.

    This is designed for Streamlit Cloud where running local scripts is not convenient.
    It returns a verification URL + user code and an onboarding_id for follow-up.

    :param client_id: OAuth client ID (optional if already in [google_oauth]).
    :param client_secret: OAuth client secret (optional if already in [google_oauth]).
    :param user_email: Optional user email hint for generated secrets block.
    """
    cid, csec = _oauth_client_credentials(client_id, client_secret)
    if not cid or not csec:
        return (
            "Missing OAuth client credentials. Provide client_id/client_secret in this tool call "
            "or set them in [google_oauth] secrets first."
        )

    payload = {
        "client_id": cid,
        "scope": _oauth_scope_string(),
    }

    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.post(_DEVICE_CODE_ENDPOINT, data=payload)
        if resp.status_code >= 400:
            return f"OAuth device start failed ({resp.status_code}): {resp.text}"
        data = resp.json()
    except Exception as exc:
        return f"OAuth onboarding start error: {exc}"

    onboarding_id = str(uuid.uuid4())[:8]
    created = time.time()
    _OAUTH_PENDING[onboarding_id] = {
        "client_id": cid,
        "client_secret": csec,
        "user_email": user_email.strip(),
        "device_code": str(data.get("device_code", "")),
        "user_code": str(data.get("user_code", "")),
        "verification_url": str(data.get("verification_url", "")),
        "verification_uri_complete": str(data.get("verification_uri_complete", "")),
        "expires_in": int(data.get("expires_in", 1800) or 1800),
        "interval": int(data.get("interval", 5) or 5),
        "created_at": created,
    }

    return (
        "Google OAuth onboarding started.\n"
        f"onboarding_id: {onboarding_id}\n"
        f"verification_url: {data.get('verification_url', '')}\n"
        f"user_code: {data.get('user_code', '')}\n"
        f"verification_uri_complete: {data.get('verification_uri_complete', '')}\n\n"
        "Next step:\n"
        "1) Open verification_url (or verification_uri_complete)\n"
        "2) Approve access\n"
        f"3) Ask me to run google_oauth_onboarding_finish(onboarding_id='{onboarding_id}')"
    )


def google_oauth_onboarding_status(onboarding_id: str) -> str:
    """
    Check onboarding flow state by onboarding_id.

    :param onboarding_id: ID returned from google_oauth_onboarding_start.
    """
    flow = _OAUTH_PENDING.get(onboarding_id.strip())
    if flow is None:
        return f"Unknown onboarding_id: {onboarding_id}"
    age = int(max(0, time.time() - float(flow.get("created_at", 0))))
    ttl = int(flow.get("expires_in", 1800))
    remaining = max(0, ttl - age)
    return (
        f"onboarding_id: {onboarding_id}\n"
        f"user_code: {flow.get('user_code', '')}\n"
        f"verification_url: {flow.get('verification_url', '')}\n"
        f"seconds_remaining: {remaining}"
    )


def google_oauth_onboarding_finish(onboarding_id: str, wait_seconds: int = 120) -> str:
    """
    Complete onboarding and return copy/paste secrets block with refresh token.

    Polls token endpoint for up to wait_seconds.

    :param onboarding_id: ID returned from google_oauth_onboarding_start.
    :param wait_seconds: Max seconds to poll for approval completion.
    """
    flow = _OAUTH_PENDING.get(onboarding_id.strip())
    if flow is None:
        return f"Unknown onboarding_id: {onboarding_id}"

    now = time.time()
    created = float(flow.get("created_at", 0))
    expires_in = int(flow.get("expires_in", 1800))
    if now > created + expires_in:
        _OAUTH_PENDING.pop(onboarding_id.strip(), None)
        return "OAuth onboarding expired. Start again with google_oauth_onboarding_start."

    interval = max(1, int(flow.get("interval", 5)))
    deadline = now + max(5, int(wait_seconds))
    device_code = str(flow.get("device_code", ""))
    cid = str(flow.get("client_id", ""))
    csec = str(flow.get("client_secret", ""))

    token_data: dict[str, Any] | None = None
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        while time.time() <= deadline:
            payload = {
                "client_id": cid,
                "client_secret": csec,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
            try:
                resp = client.post(_TOKEN_ENDPOINT, data=payload)
            except Exception as exc:
                return f"OAuth onboarding finish error: {exc}"

            if resp.status_code < 400:
                token_data = resp.json()
                break

            try:
                err = resp.json().get("error", "")
            except Exception:
                err = ""

            if err == "authorization_pending":
                time.sleep(interval)
                continue
            if err == "slow_down":
                interval += 2
                time.sleep(interval)
                continue
            if err == "access_denied":
                return "OAuth access denied by user."
            if err == "expired_token":
                _OAUTH_PENDING.pop(onboarding_id.strip(), None)
                return "OAuth onboarding expired. Start again."
            return f"OAuth token exchange failed ({resp.status_code}): {resp.text}"

    if token_data is None:
        return (
            "Still waiting for approval. Complete consent in browser and call "
            f"google_oauth_onboarding_finish(onboarding_id='{onboarding_id}') again."
        )

    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        return (
            "No refresh token returned. Revoke existing app access in Google Account "
            "permissions, then restart onboarding and grant consent again."
        )

    user_email = str(flow.get("user_email", "")).strip()
    _OAUTH_PENDING.pop(onboarding_id.strip(), None)
    lines = [
        "[google_oauth]",
        "enabled = true",
        f'client_id = "{cid}"',
        f'client_secret = "{csec}"',
        f'refresh_token = "{refresh_token}"',
        f'token_uri = "{_TOKEN_ENDPOINT}"',
    ]
    if user_email:
        lines.append(f'user_email = "{user_email}"')

    return (
        "OAuth onboarding complete.\n\n"
        "Copy and paste this into Streamlit secrets, then redeploy/restart:\n\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _safe_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _json_rows(data_json: str) -> list[list[Any]]:
    """
    Parse a JSON string into list[list[Any]] for Sheets values.
    """
    try:
        parsed = json.loads(data_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError("JSON must be a list of rows.")
    rows: list[list[Any]] = []
    for row in parsed:
        if isinstance(row, list):
            rows.append(row)
        else:
            rows.append([row])
    return rows


def _decode_email_body(payload: dict[str, Any]) -> str:
    """
    Best-effort decode of Gmail payload body text from message JSON.
    """
    # Single-part body
    body_data = payload.get("body", {}).get("data")
    if body_data:
        try:
            return base64.urlsafe_b64decode(body_data.encode("utf-8")).decode("utf-8", errors="ignore")
        except Exception:
            pass

    # Multi-part body
    for part in payload.get("parts", []) or []:
        mime = part.get("mimeType", "")
        if mime in ("text/plain", "text/html"):
            data = part.get("body", {}).get("data")
            if data:
                try:
                    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
                except Exception:
                    continue
    return ""


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, body: str) -> str:
    """
    Send an email via Gmail.

    :param to: Recipient email address.
    :param subject: Email subject line.
    :param body: Plain-text email body.
    """
    try:
        service = _gmail_service()
        mime_msg = email.mime.text.MIMEText(body)
        mime_msg["To"] = to
        mime_msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent to {to} with subject: {subject!r}"
    except Exception as exc:
        return f"Error sending email: {exc}"


def draft_email(to: str, subject: str, body: str) -> str:
    """
    Create a Gmail draft.

    :param to: Draft recipient.
    :param subject: Draft subject.
    :param body: Draft body.
    """
    try:
        service = _gmail_service()
        mime_msg = email.mime.text.MIMEText(body)
        mime_msg["To"] = to
        mime_msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return f"Draft created: {draft.get('id', 'OK')}"
    except Exception as exc:
        return f"Error creating draft: {exc}"


def read_recent_emails(query: str = "", max_results: int = 10) -> str:
    """
    Read recent inbox emails with optional Gmail query.

    :param query: Gmail search query (e.g. 'from:boss@corp.com is:unread').
    :param max_results: Maximum number of emails to return.
    """
    try:
        service = _gmail_service()
        limit = _safe_int(max_results, 1, 25)
        resp = service.users().messages().list(userId="me", q=query, maxResults=limit).execute()
        messages = resp.get("messages", [])
        if not messages:
            return "No emails found."

        blocks: list[str] = []
        for item in messages:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=item["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            blocks.append(
                "\n".join(
                    [
                        f"Message ID: {msg.get('id', '')}",
                        f"Thread ID: {msg.get('threadId', '')}",
                        f"From: {headers.get('From', '?')}",
                        f"Subject: {headers.get('Subject', '?')}",
                        f"Date: {headers.get('Date', '?')}",
                        f"Snippet: {msg.get('snippet', '')}",
                    ]
                )
            )
        return "\n\n---\n\n".join(blocks)
    except Exception as exc:
        return f"Error reading emails: {exc}"


def read_email_thread(thread_id: str, max_messages: int = 20) -> str:
    """
    Read messages in a Gmail thread including decoded plain body text.

    :param thread_id: Gmail thread ID.
    :param max_messages: Maximum thread messages to return.
    """
    try:
        service = _gmail_service()
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs = thread.get("messages", [])[: _safe_int(max_messages, 1, 50)]
        if not msgs:
            return "Thread has no messages."

        blocks: list[str] = []
        for msg in msgs:
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            body_text = _decode_email_body(msg.get("payload", {})).strip()
            body_text = body_text[:4000] if body_text else "(no decoded body)"
            blocks.append(
                "\n".join(
                    [
                        f"Message ID: {msg.get('id', '')}",
                        f"From: {headers.get('From', '?')}",
                        f"To: {headers.get('To', '?')}",
                        f"Subject: {headers.get('Subject', '?')}",
                        f"Date: {headers.get('Date', '?')}",
                        f"Body:\n{body_text}",
                    ]
                )
            )
        return "\n\n---\n\n".join(blocks)
    except Exception as exc:
        return f"Error reading email thread: {exc}"


def mark_email_read(message_id: str) -> str:
    """
    Mark a Gmail message as read.

    :param message_id: Gmail message ID.
    """
    try:
        service = _gmail_service()
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return f"Marked as read: {message_id}"
    except Exception as exc:
        return f"Error marking message read: {exc}"


def list_gmail_labels() -> str:
    """
    List Gmail labels for the active mailbox.
    """
    try:
        service = _gmail_service()
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        if not labels:
            return "No Gmail labels found."
        return "\n".join(
            f"- {l.get('name', '')} (id={l.get('id', '')}, type={l.get('type', '')})"
            for l in labels
        )
    except Exception as exc:
        return f"Error listing labels: {exc}"


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def list_calendars(max_results: int = 20) -> str:
    """
    List calendars visible to the active identity.

    :param max_results: Maximum number of calendars to list.
    """
    try:
        service = _calendar_service()
        resp = service.calendarList().list(maxResults=_safe_int(max_results, 1, 100)).execute()
        items = resp.get("items", [])
        if not items:
            return "No calendars found."
        return "\n".join(
            f"- {c.get('summary', '(no title)')} | id={c.get('id', '')} | tz={c.get('timeZone', '')}"
            for c in items
        )
    except Exception as exc:
        return f"Error listing calendars: {exc}"


def create_calendar_event(
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
) -> str:
    """
    Create a Google Calendar event.

    Times must be ISO 8601 (e.g. '2026-02-21T14:00:00-05:00').

    :param summary: Event title.
    :param start_time: Event start datetime in ISO 8601.
    :param end_time: Event end datetime in ISO 8601.
    :param calendar_id: Calendar ID (default 'primary').
    :param description: Optional event description.
    :param location: Optional event location.
    """
    try:
        service = _calendar_service()
        event_body = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        return (
            f"Event created: {event.get('id', '')}\n"
            f"Link: {event.get('htmlLink', '')}"
        )
    except Exception as exc:
        return f"Error creating calendar event: {exc}"


def list_upcoming_events(max_results: int = 10, calendar_id: str = "primary") -> str:
    """
    List upcoming events in a Google Calendar.

    :param max_results: Maximum number of events to return.
    :param calendar_id: Calendar ID to query.
    """
    try:
        service = _calendar_service()
        now = datetime.now(timezone.utc).isoformat()
        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=now,
                maxResults=_safe_int(max_results, 1, 50),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])
        if not events:
            return "No upcoming events found."

        lines = []
        for e in events:
            start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
            lines.append(
                f"- {start} | id={e.get('id', '')} | {e.get('summary', '(no title)')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing calendar events: {exc}"


def update_calendar_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
) -> str:
    """
    Update fields on an existing calendar event.

    :param event_id: Event ID to update.
    :param calendar_id: Calendar ID.
    :param summary: Optional replacement summary.
    :param start_time: Optional replacement start ISO datetime.
    :param end_time: Optional replacement end ISO datetime.
    :param description: Optional replacement description.
    :param location: Optional replacement location.
    """
    try:
        service = _calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        if summary:
            event["summary"] = summary
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        if start_time:
            event["start"] = {"dateTime": start_time}
        if end_time:
            event["end"] = {"dateTime": end_time}

        updated = service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        return (
            f"Event updated: {updated.get('id', '')}\n"
            f"Link: {updated.get('htmlLink', '')}"
        )
    except Exception as exc:
        return f"Error updating calendar event: {exc}"


def delete_calendar_event(event_id: str, calendar_id: str = "primary") -> str:
    """
    Delete a calendar event.

    :param event_id: Event ID to delete.
    :param calendar_id: Calendar ID.
    """
    try:
        service = _calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return f"Event deleted: {event_id}"
    except Exception as exc:
        return f"Error deleting calendar event: {exc}"


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------

def list_drive_files(query: str = "", max_results: int = 20) -> str:
    """
    List Google Drive files for the active identity.

    :param query: Optional Drive search query (q syntax).
    :param max_results: Maximum files to return.
    """
    try:
        service = _drive_service()
        q = query.strip() or "trashed=false"
        resp = (
            service.files()
            .list(
                q=q,
                pageSize=_safe_int(max_results, 1, 100),
                fields="files(id,name,mimeType,modifiedTime,webViewLink,parents)",
                spaces="drive",
            )
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            return "No Drive files found."
        lines = []
        for f in files:
            lines.append(
                f"- {f.get('name', '(no name)')} | id={f.get('id', '')} | "
                f"mime={f.get('mimeType', '')} | modified={f.get('modifiedTime', '')} | "
                f"link={f.get('webViewLink', '')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing Drive files: {exc}"


def read_drive_file_text(file_id: str, max_chars: int = 12000) -> str:
    """
    Read text from a Drive file (including Google Docs/Sheets export).

    :param file_id: Drive file ID.
    :param max_chars: Maximum characters to return.
    """
    try:
        service = _drive_service()
        meta = service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", "")

        if mime == "application/vnd.google-apps.document":
            data = service.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = data.decode("utf-8", errors="ignore")
        elif mime == "application/vnd.google-apps.spreadsheet":
            data = service.files().export(fileId=file_id, mimeType="text/csv").execute()
            text = data.decode("utf-8", errors="ignore")
        elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
            data = service.files().get_media(fileId=file_id).execute()
            text = data.decode("utf-8", errors="ignore")
        else:
            return (
                f"Unsupported mime type for text read: {mime}\n"
                "Use Drive export/download manually for binary formats."
            )

        text = text[:max_chars] if len(text) > max_chars else text
        return f"File: {name}\nMime: {mime}\n\n{text}"
    except Exception as exc:
        return f"Error reading Drive file: {exc}"


def create_drive_folder(name: str, parent_id: str = "") -> str:
    """
    Create a folder in Google Drive.

    :param name: Folder name.
    :param parent_id: Optional parent folder ID.
    """
    try:
        service = _drive_service()
        body: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            body["parents"] = [parent_id]
        folder = service.files().create(body=body, fields="id,name,webViewLink").execute()
        return (
            f"Folder created: {folder.get('name', '')}\n"
            f"id: {folder.get('id', '')}\n"
            f"link: {folder.get('webViewLink', '')}"
        )
    except Exception as exc:
        return f"Error creating Drive folder: {exc}"


def upload_drive_text_file(
    name: str,
    content: str,
    parent_id: str = "",
    mime_type: str = "text/plain",
) -> str:
    """
    Upload or create a text file in Google Drive.

    :param name: File name.
    :param content: Text content to upload.
    :param parent_id: Optional parent folder ID.
    :param mime_type: MIME type for upload (default text/plain).
    """
    try:
        service = _drive_service()
        body: dict[str, Any] = {"name": name}
        if parent_id:
            body["parents"] = [parent_id]
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
        file = service.files().create(
            body=body,
            media_body=media,
            fields="id,name,mimeType,webViewLink",
        ).execute()
        return (
            f"File uploaded: {file.get('name', '')}\n"
            f"id: {file.get('id', '')}\n"
            f"mime: {file.get('mimeType', '')}\n"
            f"link: {file.get('webViewLink', '')}"
        )
    except Exception as exc:
        return f"Error uploading Drive file: {exc}"


def share_drive_file(
    file_id: str,
    email_address: str,
    role: str = "writer",
    send_notification: bool = False,
) -> str:
    """
    Share a Drive file with a user.

    :param file_id: Drive file ID.
    :param email_address: User email to grant access.
    :param role: reader, commenter, or writer.
    :param send_notification: Whether to email notification.
    """
    try:
        service = _drive_service()
        perm = {
            "type": "user",
            "role": role,
            "emailAddress": email_address,
        }
        created = (
            service.permissions()
            .create(
                fileId=file_id,
                body=perm,
                sendNotificationEmail=send_notification,
                fields="id",
            )
            .execute()
        )
        return f"Permission granted (id={created.get('id', '')}) to {email_address} as {role}."
    except Exception as exc:
        return f"Error sharing Drive file: {exc}"


# ---------------------------------------------------------------------------
# Google Docs
# ---------------------------------------------------------------------------

def read_google_doc(document_id: str) -> str:
    """
    Read text content from a Google Doc.

    :param document_id: Google Docs document ID.
    """
    try:
        service = _docs_service()
        doc = service.documents().get(documentId=document_id).execute()
        content = doc.get("body", {}).get("content", [])

        text_parts: list[str] = []
        for block in content:
            paragraph = block.get("paragraph")
            if not paragraph:
                continue
            for elem in paragraph.get("elements", []):
                text_run = elem.get("textRun")
                if text_run:
                    text_parts.append(text_run.get("content", ""))

        full_text = "".join(text_parts)
        if len(full_text) > 12_000:
            full_text = full_text[:12_000] + "\n... [document truncated]"
        title = doc.get("title", "(untitled)")
        return f"Title: {title}\n\n{full_text or '(empty document)'}"
    except Exception as exc:
        return f"Error reading Google Doc: {exc}"


def create_google_doc(title: str, content: str = "") -> str:
    """
    Create a new Google Doc, optionally with initial content.

    :param title: Document title.
    :param content: Optional initial plain text content.
    """
    try:
        service = _docs_service()
        doc = service.documents().create(body={"title": title}).execute()
        doc_id = doc.get("documentId", "")
        if content and doc_id:
            service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": content}}]},
            ).execute()
        return f"Doc created: {doc_id}\nTitle: {title}\nLink: https://docs.google.com/document/d/{doc_id}/edit"
    except Exception as exc:
        return f"Error creating Google Doc: {exc}"


def append_google_doc(document_id: str, text: str) -> str:
    """
    Append text to the end of a Google Doc.

    :param document_id: Google Docs document ID.
    :param text: Text to append.
    """
    try:
        service = _docs_service()
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
        ).execute()
        return f"Text appended to doc: {document_id}"
    except Exception as exc:
        return f"Error appending to Google Doc: {exc}"


def replace_google_doc_text(document_id: str, search_text: str, replacement_text: str) -> str:
    """
    Replace all exact matches of text in a Google Doc.

    :param document_id: Google Docs document ID.
    :param search_text: Exact text to find.
    :param replacement_text: Replacement text.
    """
    try:
        service = _docs_service()
        resp = service.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": search_text, "matchCase": True},
                            "replaceText": replacement_text,
                        }
                    }
                ]
            },
        ).execute()
        count = (
            resp.get("replies", [{}])[0]
            .get("replaceAllText", {})
            .get("occurrencesChanged", 0)
        )
        return f"Replaced {count} occurrence(s) in doc {document_id}."
    except Exception as exc:
        return f"Error replacing text in Google Doc: {exc}"


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def create_spreadsheet(title: str, first_sheet_name: str = "Sheet1") -> str:
    """
    Create a Google Spreadsheet.

    :param title: Spreadsheet title.
    :param first_sheet_name: Name of the first worksheet tab.
    """
    try:
        service = _sheets_service()
        body = {"properties": {"title": title}, "sheets": [{"properties": {"title": first_sheet_name}}]}
        ss = service.spreadsheets().create(body=body).execute()
        spreadsheet_id = ss.get("spreadsheetId", "")
        return (
            f"Spreadsheet created: {spreadsheet_id}\n"
            f"Link: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        )
    except Exception as exc:
        return f"Error creating spreadsheet: {exc}"


def append_to_sheet(spreadsheet_id: str, range_name: str, data_json: str) -> str:
    """
    Append rows to a Google Sheet range.

    :param spreadsheet_id: Spreadsheet ID.
    :param range_name: A1 range, e.g. 'Sheet1!A:D'.
    :param data_json: JSON list of rows, e.g. [[\"a\",1],[\"b\",2]].
    """
    try:
        rows = _json_rows(data_json)
        service = _sheets_service()
        body = {"values": rows}
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                body=body,
            )
            .execute()
        )
        updates = result.get("updates", {})
        return (
            f"Appended {updates.get('updatedRows', '?')} row(s) to "
            f"{updates.get('updatedRange', range_name)}."
        )
    except Exception as exc:
        return f"Error appending to Google Sheet: {exc}"


def update_sheet_range(spreadsheet_id: str, range_name: str, data_json: str) -> str:
    """
    Overwrite values in a Google Sheet range.

    :param spreadsheet_id: Spreadsheet ID.
    :param range_name: A1 range, e.g. 'Sheet1!A1:C10'.
    :param data_json: JSON list of rows, e.g. [[\"a\",1],[\"b\",2]].
    """
    try:
        rows = _json_rows(data_json)
        service = _sheets_service()
        body = {"values": rows}
        result = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="USER_ENTERED",
                body=body,
            )
            .execute()
        )
        return (
            f"Updated {result.get('updatedRows', '?')} row(s), "
            f"{result.get('updatedCells', '?')} cell(s)."
        )
    except Exception as exc:
        return f"Error updating Google Sheet: {exc}"


def clear_sheet_range(spreadsheet_id: str, range_name: str) -> str:
    """
    Clear values in a Google Sheet range.

    :param spreadsheet_id: Spreadsheet ID.
    :param range_name: A1 range to clear.
    """
    try:
        service = _sheets_service()
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            body={},
        ).execute()
        return f"Cleared range: {range_name}"
    except Exception as exc:
        return f"Error clearing sheet range: {exc}"


def read_sheet_range(spreadsheet_id: str, range_name: str) -> str:
    """
    Read a range of cells from a Google Sheet.

    :param spreadsheet_id: Spreadsheet ID.
    :param range_name: A1 range, e.g. 'Sheet1!A1:D10'.
    """
    try:
        service = _sheets_service()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            return "(no data in range)"
        return "\n".join("\t".join(str(c) for c in row) for row in rows)
    except Exception as exc:
        return f"Error reading Google Sheet: {exc}"
