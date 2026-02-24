"""
session.py — Conversation history manager.

Each session is stored as a JSONL file on Google Drive:
  web_<username>.jsonl   — Web UI sessions
  tg_<user_id>.jsonl     — Telegram sessions

Because SCC's filesystem is ephemeral, Drive is the source of truth.
On load the JSONL is downloaded; every append immediately re-syncs.
"""

import json
from pathlib import Path
from typing import Any

import drive_sync

# Local cache dir (ephemeral — Drive is canonical)
WORKSPACE = Path("/tmp/workspace")
WORKSPACE.mkdir(parents=True, exist_ok=True)

# How many past messages to include in each LLM call
MAX_HISTORY_MESSAGES = 40


class Session:
    """Manages a single conversation session backed by Google Drive."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.filename = f"{session_id}.jsonl"
        self._messages: list[dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _local_path(self) -> Path:
        return WORKSPACE / self.filename

    def _load(self) -> None:
        """Download the session JSONL from Drive and parse it."""
        try:
            raw = drive_sync.read_file(self.filename)
        except Exception:
            local = self._local_path()
            raw = local.read_text(encoding="utf-8") if local.exists() else ""
        messages = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        self._messages = messages

    def _save(self) -> None:
        """Serialise all messages to JSONL and push to Drive."""
        content = "\n".join(json.dumps(m, ensure_ascii=False) for m in self._messages)
        if content:
            content += "\n"
        drive_sync.write_file(self.filename, content)

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def add_message(self, role: str, content: str) -> None:
        """Append a message and immediately persist to Drive."""
        self._messages.append({"role": role, "content": content})
        self._save()

    def add_tool_call(self, tool_call: dict[str, Any]) -> None:
        """Append a raw tool-call message (as returned by the LLM)."""
        self._messages.append(tool_call)
        self._save()

    def add_tool_result(self, tool_call_id: str, name: str, result: str) -> None:
        """Append a tool result message."""
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": result,
            }
        )
        self._save()

    def get_messages(self) -> list[dict[str, Any]]:
        """
        Return the recent conversation history for inclusion in the LLM call.
        Truncates to MAX_HISTORY_MESSAGES to keep context windows manageable.
        """
        return self._messages[-MAX_HISTORY_MESSAGES:]

    def get_messages_since(self, start_index: int) -> list[dict[str, Any]]:
        """
        Return all messages appended at or after `start_index`.
        """
        idx = max(0, int(start_index))
        return self._messages[idx:]

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """
        Replace full in-memory history and persist to Drive.

        :param messages: Full message list to store.
        """
        self._messages = list(messages)
        self._save()

    def clear(self) -> None:
        """Wipe the session history locally and on Drive."""
        self._messages = []
        self._save()

    def __len__(self) -> int:
        return len(self._messages)
