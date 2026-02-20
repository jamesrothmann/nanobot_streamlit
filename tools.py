"""
tools.py — Core agent tools: web search, web fetch, and shell execution.

Each public function in this module is auto-discovered by agent.py and
exposed to the LLM as a callable tool.  Keep signatures clean and
docstrings precise — they become the tool descriptions the LLM sees.
"""

import asyncio
import re
import subprocess
from typing import Optional

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Web Search (Brave Search API)
# ---------------------------------------------------------------------------

async def web_search(query: str, num_results: int = 5) -> str:
    """
    Search the web using the Brave Search API and return a summary of results.

    :param query: The search query string.
    :param num_results: Maximum number of results to return (default 5).
    """
    api_key = st.secrets["system"].get("brave_api_key", "")
    if not api_key:
        return "Error: Brave API key is not configured in secrets.toml."

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(num_results, 20)}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("web", {}).get("results", [])
    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results[:num_results], 1):
        title = r.get("title", "")
        href = r.get("url", "")
        desc = r.get("description", "")
        lines.append(f"{i}. **{title}**\n   {href}\n   {desc}")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Web Fetch (extract readable text from a URL)
# ---------------------------------------------------------------------------

async def web_fetch(url: str, max_chars: int = 8000) -> str:
    """
    Fetch a web page and return its main readable text content.

    :param url: The URL to fetch.
    :param max_chars: Maximum characters to return from the page content.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; Nanobot/1.0; +https://github.com/HKUDS/nanobot)"
        )
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        html = resp.text

    # Try readability-style extraction first, fall back to raw tag stripping
    try:
        from readability import Document

        doc = Document(html)
        text = doc.summary()
        # Strip residual HTML tags
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()

    return text[:max_chars] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# Shell / Exec
# ---------------------------------------------------------------------------

# Commands that are never allowed
_BLOCKED = re.compile(
    r"\b(rm\s+-rf|mkfs|dd\s+if=|fork\s*bomb|:\(\)\s*\{|shutdown|reboot|poweroff)\b",
    re.IGNORECASE,
)


def shell_exec(command: str, timeout: int = 30) -> str:
    """
    Execute a shell command and return its combined stdout + stderr output.

    Only non-destructive commands are permitted.  Dangerous patterns
    (rm -rf, mkfs, dd, etc.) are blocked.

    :param command: The shell command to run.
    :param timeout: Maximum seconds to wait before killing the process.
    """
    if _BLOCKED.search(command):
        return f"Error: command blocked for safety: {command!r}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        # Truncate very long output
        if len(output) > 10_000:
            output = output[:10_000] + "\n… [output truncated]"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    except Exception as exc:
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Memory tools (thin wrappers so the LLM can update memory as a tool call)
# ---------------------------------------------------------------------------

def update_memory(new_content: str) -> str:
    """
    Overwrite the agent's long-term MEMORY.md with new content.

    Use this to distil important facts, preferences, or context that should
    be remembered across sessions.

    :param new_content: The full new content to store in MEMORY.md.
    """
    import memory as mem_module
    return mem_module.update_memory(new_content)


def append_history(event: str) -> str:
    """
    Append a brief event summary to the HISTORY.md log.

    :param event: A one-line description of what happened or was learned.
    """
    import memory as mem_module
    return mem_module.append_history(event)
