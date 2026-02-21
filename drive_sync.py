"""
drive_sync.py — Google Drive wrapper for persistent state management.

Strategy:
  - On boot: download AGENTS.md, USER.md, MEMORY.md, HISTORY.md to /tmp/workspace/
  - On read:  serve from local /tmp/workspace/ cache
  - On write: write to cache AND immediately overwrite the Drive file
"""

import io
import os
from pathlib import Path
from typing import Optional

import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload

# Local cache directory — ephemeral, but that's fine; Drive is the source of truth
WORKSPACE = Path("/tmp/workspace")
WORKSPACE.mkdir(parents=True, exist_ok=True)

# Files that are always synced from Drive on boot
BOOT_FILES = ["AGENTS.md", "USER.md", "MEMORY.md", "HISTORY.md"]

SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


@st.cache_resource
def _drive_service():
    """Build and cache a Google Drive service client."""
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id() -> str:
    return st.secrets["system"]["gdrive_workspace_folder_id"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_file_id(filename: str) -> Optional[str]:
    """Return the Drive file ID for a filename in the workspace folder, or None."""
    service = _drive_service()
    folder = _folder_id()
    resp = (
        service.files()
        .list(
            q=f"name='{filename}' and '{folder}' in parents and trashed=false",
            fields="files(id, name)",
            spaces="drive",
        )
        .execute()
    )
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _download_file(file_id: str) -> bytes:
    """Download raw bytes for a Drive file by ID."""
    service = _drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _upload_or_update(filename: str, content: str) -> str:
    """
    Upload a new file to the workspace folder, or update an existing one.
    Returns the Drive file ID.
    """
    service = _drive_service()
    folder = _folder_id()
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    existing_id = _find_file_id(filename)

    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        return existing_id
    else:
        metadata = {"name": filename, "parents": [folder]}
        file = (
            service.files()
            .create(body=metadata, media_body=media, fields="id")
            .execute()
        )
        return file["id"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def boot_sync() -> None:
    """
    Download all standard workspace files from Drive to /tmp/workspace/.
    Call once at application startup. Missing files are created with empty content.
    """
    for filename in BOOT_FILES:
        local_path = WORKSPACE / filename
        file_id = _find_file_id(filename)
        if file_id:
            data = _download_file(file_id)
            local_path.write_bytes(data)
        else:
            # File doesn't exist in Drive yet; create it empty
            local_path.write_text("", encoding="utf-8")
            _upload_or_update(filename, "")


def read_file(filename: str) -> str:
    """
    Read a workspace file from the local cache.
    Falls back to Drive if the file isn't cached locally.
    """
    local_path = WORKSPACE / filename
    if local_path.exists():
        return local_path.read_text(encoding="utf-8")

    # Cache miss — pull from Drive
    file_id = _find_file_id(filename)
    if file_id:
        data = _download_file(file_id)
        local_path.write_bytes(data)
        return local_path.read_text(encoding="utf-8")

    return ""


def write_file(filename: str, content: str) -> None:
    """
    Write content to the local cache and immediately persist to Google Drive.
    This is the only way the agent should write workspace files.
    """
    local_path = WORKSPACE / filename
    local_path.write_text(content, encoding="utf-8")
    _upload_or_update(filename, content)


def append_file(filename: str, content: str) -> None:
    """Append content to a workspace file and sync to Drive."""
    existing = read_file(filename)
    write_file(filename, existing + content)


def list_session_files() -> list[str]:
    """List all *.jsonl session files in the Drive workspace folder."""
    service = _drive_service()
    folder = _folder_id()
    resp = (
        service.files()
        .list(
            q=f"'{folder}' in parents and name contains '.jsonl' and trashed=false",
            fields="files(id, name)",
            spaces="drive",
        )
        .execute()
    )
    return [f["name"] for f in resp.get("files", [])]
