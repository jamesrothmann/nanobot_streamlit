"""
memory.py — Agent memory logic backed by Google Drive via drive_sync.py.

Two layers:
  MEMORY.md   — Long-term distilled facts the agent writes deliberately.
  HISTORY.md  — Append-only event log (lightweight breadcrumb trail).

Both files live in /tmp/workspace/ locally and are mirrored to Google Drive
so they survive SCC container restarts.
"""

from datetime import datetime, timezone
from typing import Optional

import drive_sync

MEMORY_FILE = "MEMORY.md"
HISTORY_FILE = "HISTORY.md"
AGENTS_FILE = "AGENTS.md"
USER_FILE = "USER.md"


# ---------------------------------------------------------------------------
# Read helpers (used by agent.py to build system prompt context)
# ---------------------------------------------------------------------------

def read_memory() -> str:
    """Return the current contents of MEMORY.md."""
    return drive_sync.read_file(MEMORY_FILE)


def read_history() -> str:
    """Return the current contents of HISTORY.md."""
    return drive_sync.read_file(HISTORY_FILE)


def read_agents() -> str:
    """Return AGENTS.md — the agent's personality / instruction file."""
    return drive_sync.read_file(AGENTS_FILE)


def read_user() -> str:
    """Return USER.md — the user profile file."""
    return drive_sync.read_file(USER_FILE)


# ---------------------------------------------------------------------------
# Write helpers (called by the agent when it decides to update memory)
# ---------------------------------------------------------------------------

def update_memory(new_content: str) -> str:
    """
    Overwrite MEMORY.md with new_content and sync to Google Drive.
    Returns a confirmation string for the LLM tool result.
    """
    drive_sync.write_file(MEMORY_FILE, new_content)
    return "Memory updated successfully."


def append_history(event: str) -> str:
    """
    Append a timestamped event line to HISTORY.md and sync to Google Drive.
    Returns a confirmation string for the LLM tool result.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"[{ts}] {event}\n"
    drive_sync.append_file(HISTORY_FILE, line)
    return f"History updated: {line.strip()}"


# ---------------------------------------------------------------------------
# Context assembly (called by agent.py to build the full system prompt)
# ---------------------------------------------------------------------------

def build_memory_context() -> str:
    """
    Assemble all memory-related context into a single string
    suitable for injection into the LLM system prompt.
    """
    sections: list[str] = []

    agents_md = read_agents()
    if agents_md.strip():
        sections.append(f"# Agent Instructions\n{agents_md}")

    user_md = read_user()
    if user_md.strip():
        sections.append(f"# User Profile\n{user_md}")

    memory_md = read_memory()
    if memory_md.strip():
        sections.append(f"# Long-Term Memory\n{memory_md}")

    history_md = read_history()
    if history_md.strip():
        # Only show the last 50 lines of history to keep prompts manageable
        lines = history_md.strip().splitlines()
        tail = "\n".join(lines[-50:])
        sections.append(f"# Recent History (last 50 events)\n{tail}")

    return "\n\n---\n\n".join(sections)
