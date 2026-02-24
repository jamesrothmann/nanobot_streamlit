"""
drive_sync.py — Google Drive wrapper for persistent state management.

Strategy:
  - On boot: download AGENTS.md, USER.md, MEMORY.md, HISTORY.md to /tmp/workspace/
  - On read:  serve from local /tmp/workspace/ cache
  - On write: write to cache AND immediately overwrite the Drive file
"""

import io
import json
import time
from pathlib import Path
from typing import Optional

import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload

# Local cache directory — ephemeral, but that's fine; Drive is the source of truth
WORKSPACE = Path("/tmp/workspace")
WORKSPACE.mkdir(parents=True, exist_ok=True)
PENDING_SYNC_FILE = WORKSPACE / ".pending_drive_sync.json"

# Files that are always synced from Drive on boot
BOOT_FILES = ["AGENTS.md", "USER.md", "MEMORY.md", "HISTORY.md"]

SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


def _api_retries() -> int:
    try:
        configured = int(st.secrets.get("system", {}).get("drive_api_retries", 3))
    except Exception:
        configured = 3
    return max(0, min(configured, 10))


def _call_with_backoff(op_name: str, fn):
    try:
        attempts = int(st.secrets.get("system", {}).get("drive_retry_attempts", 3))
    except Exception:
        attempts = 3
    attempts = max(1, min(attempts, 8))
    base_sleep = 0.75
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            time.sleep(min(base_sleep * (2 ** (attempt - 1)), 6.0))
    raise RuntimeError(f"{op_name} failed after {attempts} attempts: {last_exc}")


def _load_pending_sync() -> set[str]:
    if not PENDING_SYNC_FILE.exists():
        return set()
    try:
        raw = PENDING_SYNC_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return set()
        parsed = set(str(x).strip() for x in json.loads(raw) if str(x).strip())
        return parsed
    except Exception:
        return set()


def _save_pending_sync(pending: set[str]) -> None:
    items = sorted(x for x in pending if x.strip())
    if not items:
        if PENDING_SYNC_FILE.exists():
            PENDING_SYNC_FILE.unlink(missing_ok=True)
        return
    PENDING_SYNC_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_pending_sync(filename: str) -> None:
    pending = _load_pending_sync()
    pending.add(filename)
    _save_pending_sync(pending)


def _clear_pending_sync(filename: str) -> None:
    pending = _load_pending_sync()
    if filename in pending:
        pending.remove(filename)
        _save_pending_sync(pending)


def pending_sync_files() -> list[str]:
    """
    Return locally-cached files that failed to sync to Drive.
    """
    return sorted(_load_pending_sync())


def flush_pending_sync(limit: int = 10) -> dict[str, int]:
    """
    Best-effort push of pending local files back to Drive.
    """
    pending = _load_pending_sync()
    if not pending:
        return {"attempted": 0, "synced": 0, "remaining": 0}

    attempted = 0
    synced = 0
    for name in list(sorted(pending))[: max(1, int(limit))]:
        attempted += 1
        local_path = WORKSPACE / name
        if not local_path.exists():
            pending.discard(name)
            continue
        try:
            _upload_or_update(name, local_path.read_text(encoding="utf-8"))
            pending.discard(name)
            synced += 1
        except Exception:
            continue
    _save_pending_sync(pending)
    return {"attempted": attempted, "synced": synced, "remaining": len(pending)}


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
    resp = _call_with_backoff(
        "drive.files.list",
        lambda: (
            service.files()
            .list(
                q=f"name='{filename}' and '{folder}' in parents and trashed=false",
                fields="files(id, name)",
                spaces="drive",
            )
            .execute(num_retries=_api_retries())
        ),
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
        _, done = _call_with_backoff(
            "drive.files.get_media",
            lambda: downloader.next_chunk(num_retries=_api_retries()),
        )
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
        _call_with_backoff(
            "drive.files.update",
            lambda: service.files()
            .update(fileId=existing_id, media_body=media)
            .execute(num_retries=_api_retries()),
        )
        return existing_id
    else:
        metadata = {"name": filename, "parents": [folder]}
        file = _call_with_backoff(
            "drive.files.create",
            lambda: (
                service.files()
                .create(body=metadata, media_body=media, fields="id")
                .execute(num_retries=_api_retries())
            ),
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
    flush_pending_sync(limit=25)


def read_file(filename: str) -> str:
    """
    Read a workspace file from the local cache.
    Falls back to Drive if the file isn't cached locally.
    """
    local_path = WORKSPACE / filename
    if local_path.exists():
        return local_path.read_text(encoding="utf-8")

    # Cache miss — pull from Drive
    try:
        file_id = _find_file_id(filename)
        if file_id:
            data = _download_file(file_id)
            local_path.write_bytes(data)
            return local_path.read_text(encoding="utf-8")
    except Exception:
        if local_path.exists():
            return local_path.read_text(encoding="utf-8")
        return ""

    return ""


def write_file(filename: str, content: str) -> None:
    """
    Write content to the local cache and immediately persist to Google Drive.
    This is the only way the agent should write workspace files.
    """
    local_path = WORKSPACE / filename
    local_path.write_text(content, encoding="utf-8")
    try:
        _upload_or_update(filename, content)
        _clear_pending_sync(filename)
    except Exception as exc:
        _mark_pending_sync(filename)
        print(f"[drive_sync] warning: deferred sync for {filename}: {exc}")


def append_file(filename: str, content: str) -> None:
    """Append content to a workspace file and sync to Drive."""
    existing = read_file(filename)
    write_file(filename, existing + content)


def list_session_files() -> list[str]:
    """List all *.jsonl session files in the Drive workspace folder."""
    service = _drive_service()
    folder = _folder_id()
    resp = _call_with_backoff(
        "drive.files.list",
        lambda: (
            service.files()
            .list(
                q=f"'{folder}' in parents and name contains '.jsonl' and trashed=false",
                fields="files(id, name)",
                spaces="drive",
            )
            .execute(num_retries=_api_retries())
        ),
    )
    return [f["name"] for f in resp.get("files", [])]
