"""
gworkspace.py — Google Workspace tools (Gmail, Calendar, Docs, Sheets).

Each public function is auto-discovered by agent.py and exposed to the LLM
as a callable tool.  Type hints and docstrings are required — they are parsed
by llm.build_tool_schemas() to produce the JSON schema the LLM uses.

SECURITY NOTE:
  The Service Account has its own isolated Google identity.  To access the
  user's personal Gmail/Calendar/Docs/Sheets the user must:
    - For Gmail: set up domain-wide delegation OR use a shared inbox
    - For Calendar: share the calendar with the service account email
    - For Docs/Sheets: share the document with the service account email
  The service account email is in secrets.toml → gcp_service_account.client_email
"""

import base64
import email.mime.text
from datetime import datetime
from typing import Any

import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_DOCS_SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]
_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@st.cache_resource
def _gmail_service():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=_GMAIL_SCOPES
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


@st.cache_resource
def _calendar_service():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=_CALENDAR_SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


@st.cache_resource
def _docs_service():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=_DOCS_SCOPES
    )
    return build("docs", "v1", credentials=creds, cache_discovery=False)


@st.cache_resource
def _sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=_SHEETS_SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, body: str) -> str:
    """
    Send an email via Gmail using the service account.

    The service account must have Gmail send permission or domain-wide
    delegation configured.  Ask the user for the recipient address before
    calling this tool.

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


def read_recent_emails(query: str = "", max_results: int = 10) -> str:
    """
    Read recent emails from the Gmail inbox matching an optional query.

    :param query: Gmail search query string (e.g. 'from:boss@corp.com is:unread').
    :param max_results: Maximum number of emails to return.
    """
    try:
        service = _gmail_service()
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        messages = resp.get("messages", [])
        if not messages:
            return "No emails found."

        results = []
        for m in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=m["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            snippet = msg.get("snippet", "")
            results.append(
                f"From: {headers.get('From', '?')}\n"
                f"Subject: {headers.get('Subject', '?')}\n"
                f"Date: {headers.get('Date', '?')}\n"
                f"Snippet: {snippet}\n"
            )
        return "\n---\n".join(results)
    except Exception as exc:
        return f"Error reading emails: {exc}"


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def create_calendar_event(
    summary: str,
    start_time: str,
    end_time: str,
    calendar_id: str = "primary",
) -> str:
    """
    Create a new event in Google Calendar.

    Times must be ISO 8601 format, e.g. '2025-01-15T14:00:00+02:00'.
    The calendar must be shared with the service account email.

    :param summary: Event title / summary.
    :param start_time: Event start time in ISO 8601 format.
    :param end_time: Event end time in ISO 8601 format.
    :param calendar_id: Calendar ID to create the event in (default 'primary').
    """
    try:
        service = _calendar_service()
        event_body = {
            "summary": summary,
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }
        event = (
            service.events()
            .insert(calendarId=calendar_id, body=event_body)
            .execute()
        )
        return f"Event created: {event.get('htmlLink', 'OK')}"
    except Exception as exc:
        return f"Error creating calendar event: {exc}"


def list_upcoming_events(max_results: int = 10, calendar_id: str = "primary") -> str:
    """
    List upcoming events from a Google Calendar.

    The calendar must be shared with the service account email.

    :param max_results: Maximum number of upcoming events to return.
    :param calendar_id: Calendar ID to query (default 'primary').
    """
    try:
        service = _calendar_service()
        now = datetime.utcnow().isoformat() + "Z"
        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=now,
                maxResults=max_results,
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
            start = e["start"].get("dateTime", e["start"].get("date", "?"))
            lines.append(f"- {start}: {e.get('summary', '(no title)')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing calendar events: {exc}"


# ---------------------------------------------------------------------------
# Google Docs
# ---------------------------------------------------------------------------

def read_google_doc(document_id: str) -> str:
    """
    Read the text content of a Google Doc.

    The document must be shared with the service account email first.
    Ask the user for the Document ID (from the URL: docs.google.com/document/d/<ID>/).

    :param document_id: The Google Docs document ID.
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
        # Truncate very large docs
        if len(full_text) > 12_000:
            full_text = full_text[:12_000] + "\n… [document truncated]"
        return full_text or "(empty document)"
    except Exception as exc:
        return f"Error reading Google Doc: {exc}"


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def append_to_sheet(spreadsheet_id: str, range_name: str, data: list) -> str:
    """
    Append rows of data to a Google Sheet.

    The spreadsheet must be shared with the service account email first.
    Ask the user for the Spreadsheet ID (from the URL) and range before calling.

    :param spreadsheet_id: The Google Sheets spreadsheet ID.
    :param range_name: A1 notation range, e.g. 'Sheet1!A:D'.
    :param data: A list of rows, where each row is a list of cell values.
    """
    try:
        service = _sheets_service()
        body = {"values": data}
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
            f"Appended {updates.get('updatedRows', '?')} row(s) "
            f"to {updates.get('updatedRange', range_name)}."
        )
    except Exception as exc:
        return f"Error appending to Google Sheet: {exc}"


def read_sheet_range(spreadsheet_id: str, range_name: str) -> str:
    """
    Read a range of cells from a Google Sheet.

    The spreadsheet must be shared with the service account email.

    :param spreadsheet_id: The Google Sheets spreadsheet ID.
    :param range_name: A1 notation range, e.g. 'Sheet1!A1:D10'.
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
