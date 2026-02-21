"""
tools.py - Core agent tools: web search/fetch, Jina/Steel web automation, and shell execution.

Each public function in this module is auto-discovered by agent.py and
exposed to the LLM as a callable tool. Keep signatures clean and
docstrings precise - they become the tool descriptions the LLM sees.
"""

import asyncio
import os
import re
import shlex
import shutil
import subprocess
from typing import Any

import httpx
import streamlit as st

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Commands that are never allowed
_BLOCKED = re.compile(
    r"\b(rm\s+-rf|mkfs|dd\s+if=|fork\s*bomb|:\(\)\s*\{|shutdown|reboot|poweroff)\b",
    re.IGNORECASE,
)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [output truncated]"


def _get_system_secret(secret_key: str, env_key: str = "") -> str:
    """Return secret from st.secrets['system'] or fallback environment variable."""
    value = ""
    try:
        system_section = dict(st.secrets.get("system", {}))
        value = str(system_section.get(secret_key, "")).strip()
    except Exception:
        value = ""
    if value:
        return value
    return os.getenv(env_key, "").strip() if env_key else ""


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match((value or "").strip()))


# ---------------------------------------------------------------------------
# Web Search (Brave Search API)
# ---------------------------------------------------------------------------

async def web_search(query: str, num_results: int = 5) -> str:
    """
    Search the web using the Brave Search API and return a summary of results.

    :param query: The search query string.
    :param num_results: Maximum number of results to return (default 5).
    """
    api_key = _get_system_secret("brave_api_key", "BRAVE_API_KEY")
    if not api_key:
        return "Error: Brave API key is not configured."

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": min(max(num_results, 1), 20)}

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

    # Try readability-style extraction first, fall back to raw tag stripping.
    try:
        from readability import Document

        doc = Document(html)
        text = doc.summary()
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()

    return text[:max_chars] if len(text) > max_chars else text


# ---------------------------------------------------------------------------
# Jina APIs (Search + Reader)
# ---------------------------------------------------------------------------

def _format_jina_search_results(results: list[dict[str, Any]], max_chars: int = 1400) -> str:
    if not results:
        return "No results found."

    blocks: list[str] = []
    for idx, item in enumerate(results, 1):
        title = str(item.get("title", "")).strip() or "(untitled)"
        url = str(item.get("url", "")).strip() or "(no url)"
        description = str(item.get("description", "")).strip()
        content = _truncate(str(item.get("content", "")).strip(), max_chars)
        block = [
            f"{idx}. {title}",
            f"URL: {url}",
        ]
        if description:
            block.append(f"Description: {description}")
        if content:
            block.append(f"Content:\n{content}")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def _format_jina_reader_data(
    url: str,
    data: dict[str, Any],
    return_format: str,
    max_content_chars: int = 12_000,
) -> str:
    content = _truncate(str(data.get("content", "")).strip(), max_content_chars)
    links = data.get("links", {})

    lines = [
        f"URL: {url}",
        f"Return format: {return_format}",
    ]
    if content:
        lines.append(f"Content:\n{content}")
    else:
        lines.append("Content: (empty)")

    if isinstance(links, dict) and links:
        link_lines = []
        for idx, (k, v) in enumerate(links.items(), 1):
            if idx > 25:
                link_lines.append("... (links truncated)")
                break
            link_lines.append(f"- {k}: {v}")
        lines.append("Links summary:\n" + "\n".join(link_lines))

    image_urls = data.get("images")
    if isinstance(image_urls, list) and image_urls:
        lines.append("Images: " + ", ".join(str(x) for x in image_urls[:10]))

    return "\n\n".join(lines)


async def _jina_search_request(
    query: str,
    num_results: int,
    site: str,
    no_cache: bool,
    respond_with: str,
    gl: str,
    hl: str,
    location: str,
) -> tuple[list[dict[str, Any]], str | None]:
    api_key = _get_system_secret("jina_api_key", "JINA_API_KEY")
    if not api_key:
        return [], "Error: JINA_API_KEY is not configured."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if site:
        headers["X-Site"] = site
    if no_cache:
        headers["X-No-Cache"] = "true"
    if respond_with:
        headers["X-Respond-With"] = respond_with

    payload: dict[str, Any] = {
        "q": query,
        "gl": gl or "US",
        "hl": hl or "en",
        "num": min(max(num_results, 1), 20),
    }
    if location:
        payload["location"] = location

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.post("https://s.jina.ai/", headers=headers, json=payload)
        if resp.status_code >= 400:
            return [], f"Jina Search API error ({resp.status_code}): {_truncate(resp.text, 600)}"
        body = resp.json()
        results = body.get("data", [])
        if not isinstance(results, list):
            return [], "Error: Unexpected Jina Search response format."
        return results, None
    except Exception as exc:
        return [], f"Error calling Jina Search API: {exc}"


async def _jina_reader_request(
    url: str,
    return_format: str,
    wait_for_selector: str,
    target_selector: str,
    remove_selector: str,
    timeout_seconds: int,
    with_links_summary: bool,
    with_generated_alt: bool,
    set_cookie: str,
    inject_page_script: str,
    use_eu_endpoint: bool,
    viewport_width: int,
    viewport_height: int,
) -> tuple[dict[str, Any], str | None]:
    api_key = _get_system_secret("jina_api_key", "JINA_API_KEY")
    if not api_key:
        return {}, "Error: JINA_API_KEY is not configured."

    endpoint = "https://eu.r.jina.ai/" if use_eu_endpoint else "https://r.jina.ai/"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Return-Format": return_format or "markdown",
        "X-With-Links-Summary": "true" if with_links_summary else "false",
        "X-With-Generated-Alt": "true" if with_generated_alt else "false",
    }
    if wait_for_selector:
        headers["X-Wait-For-Selector"] = wait_for_selector
    if target_selector:
        headers["X-Target-Selector"] = target_selector
    if remove_selector:
        headers["X-Remove-Selector"] = remove_selector
    if timeout_seconds > 0:
        headers["X-Timeout"] = str(timeout_seconds)
    if set_cookie:
        headers["X-Set-Cookie"] = set_cookie

    payload: dict[str, Any] = {
        "url": url,
        "viewport": {
            "width": max(320, viewport_width),
            "height": max(320, viewport_height),
        },
    }
    if inject_page_script:
        payload["injectPageScript"] = inject_page_script

    try:
        async with httpx.AsyncClient(timeout=max(timeout_seconds, 10) + 10, follow_redirects=True) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
        if resp.status_code >= 400:
            return {}, f"Jina Reader API error ({resp.status_code}): {_truncate(resp.text, 600)}"
        body = resp.json()
        data = body.get("data", {})
        if not isinstance(data, dict):
            return {}, "Error: Unexpected Jina Reader response format."
        return data, None
    except Exception as exc:
        return {}, f"Error calling Jina Reader API: {exc}"


async def jina_search(
    query: str,
    num_results: int = 5,
    site: str = "",
    no_cache: bool = False,
    respond_with: str = "",
    gl: str = "US",
    hl: str = "en",
    location: str = "",
) -> str:
    """
    Search the web with Jina Search API and return summarized top results.

    :param query: Search query.
    :param num_results: Number of results to return (1-20).
    :param site: Optional domain restriction (for X-Site header).
    :param no_cache: Set true to bypass Jina cache.
    :param respond_with: Optional X-Respond-With header value.
    :param gl: Country code, e.g. US.
    :param hl: Language code, e.g. en.
    :param location: Optional location hint for search.
    """
    results, err = await _jina_search_request(
        query=query,
        num_results=num_results,
        site=site,
        no_cache=no_cache,
        respond_with=respond_with,
        gl=gl,
        hl=hl,
        location=location,
    )
    if err:
        return err
    return _format_jina_search_results(results[: min(max(num_results, 1), 20)])


async def jina_read(
    url: str,
    return_format: str = "markdown",
    wait_for_selector: str = "",
    target_selector: str = "",
    remove_selector: str = "",
    timeout_seconds: int = 30,
    with_links_summary: bool = True,
    with_generated_alt: bool = True,
    set_cookie: str = "",
    inject_page_script: str = "",
    use_eu_endpoint: bool = False,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
) -> str:
    """
    Read/extract a web page via Jina Reader API with optional JS and headers.

    :param url: Target URL to read.
    :param return_format: markdown, html, text, screenshot, or pageshot.
    :param wait_for_selector: Optional CSS selector to wait for.
    :param target_selector: Optional CSS selector scope.
    :param remove_selector: Optional CSS selector to exclude.
    :param timeout_seconds: Max wait/load timeout in seconds.
    :param with_links_summary: Include links dictionary in response.
    :param with_generated_alt: Include generated image alt text where available.
    :param set_cookie: Optional cookie header value for authenticated sessions.
    :param inject_page_script: Optional JS to run before capture.
    :param use_eu_endpoint: Use eu.r.jina.ai endpoint.
    :param viewport_width: Viewport width for page rendering.
    :param viewport_height: Viewport height for page rendering.
    """
    if not _looks_like_url(url):
        return f"Error: invalid URL: {url!r}"

    data, err = await _jina_reader_request(
        url=url,
        return_format=return_format,
        wait_for_selector=wait_for_selector,
        target_selector=target_selector,
        remove_selector=remove_selector,
        timeout_seconds=timeout_seconds,
        with_links_summary=with_links_summary,
        with_generated_alt=with_generated_alt,
        set_cookie=set_cookie,
        inject_page_script=inject_page_script,
        use_eu_endpoint=use_eu_endpoint,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
    )
    if err:
        return err
    return _format_jina_reader_data(url=url, data=data, return_format=return_format)


# ---------------------------------------------------------------------------
# Steel.dev + agent-browser
# ---------------------------------------------------------------------------

async def _steel_create_session_request(
    use_proxy: bool,
    solve_captcha: bool,
) -> tuple[dict[str, Any], str | None]:
    api_key = _get_system_secret("steel_api_key", "STEEL_API_KEY")
    if not api_key:
        return {}, "Error: STEEL_API_KEY is not configured."

    payload = {
        "useProxy": bool(use_proxy),
        "solveCaptcha": bool(solve_captcha),
    }
    headers = {
        "steel-api-key": api_key,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.post(
                "https://api.steel.dev/v1/sessions",
                headers=headers,
                json=payload,
            )
        if resp.status_code >= 400:
            return {}, f"Steel session create error ({resp.status_code}): {_truncate(resp.text, 600)}"
        data = resp.json()
        if not isinstance(data, dict):
            return {}, "Error: Unexpected Steel create-session response format."
        if not data.get("id") or not data.get("websocketUrl"):
            return {}, "Error: Steel session response missing id or websocketUrl."
        return data, None
    except Exception as exc:
        return {}, f"Error creating Steel session: {exc}"


async def _steel_close_session_request(session_id: str) -> tuple[bool, str | None]:
    api_key = _get_system_secret("steel_api_key", "STEEL_API_KEY")
    if not api_key:
        return False, "Error: STEEL_API_KEY is not configured."
    headers = {"steel-api-key": api_key}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.delete(
                f"https://api.steel.dev/v1/sessions/{session_id}",
                headers=headers,
            )
        if resp.status_code >= 400:
            return False, f"Steel session close error ({resp.status_code}): {_truncate(resp.text, 600)}"
        return True, None
    except Exception as exc:
        return False, f"Error closing Steel session: {exc}"


def _run_agent_browser(cdp_url: str, command: str, timeout: int) -> str:
    if not shutil.which("agent-browser"):
        return "Error: 'agent-browser' CLI is not installed or not in PATH."
    if _BLOCKED.search(command):
        return f"Error: command blocked for safety: {command!r}"

    try:
        cmd_parts = shlex.split(command)
    except ValueError as exc:
        return f"Error parsing agent-browser command: {exc}"

    try:
        result = subprocess.run(
            ["agent-browser", "--cdp", cdp_url, *cmd_parts],
            capture_output=True,
            text=True,
            timeout=max(timeout, 1),
        )
        output = (result.stdout or "") + (result.stderr or "")
        if not output.strip():
            output = "(no output)"
        output = _truncate(output, 12_000)
        if result.returncode != 0:
            return f"Exit code {result.returncode}\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: agent-browser command timed out after {timeout}s."
    except Exception as exc:
        return f"Error running agent-browser command: {exc}"


async def steel_create_session(use_proxy: bool = True, solve_captcha: bool = True) -> str:
    """
    Create a Steel.dev browser session and return session id/CDP/viewer URLs.

    :param use_proxy: Enable Steel proxy routing.
    :param solve_captcha: Enable Steel CAPTCHA solving.
    """
    data, err = await _steel_create_session_request(
        use_proxy=use_proxy,
        solve_captcha=solve_captcha,
    )
    if err:
        return err
    return (
        "Steel session created.\n"
        f"session_id: {data.get('id', '')}\n"
        f"cdp_url: {data.get('websocketUrl', '')}\n"
        f"viewer_url: {data.get('sessionViewerUrl', '')}"
    )


async def steel_agent_browser(cdp_url: str, command: str, timeout: int = 90) -> str:
    """
    Run one agent-browser command against a Steel CDP endpoint.

    :param cdp_url: CDP websocket URL from steel_create_session.
    :param command: agent-browser subcommand, e.g. 'open https://example.com'.
    :param timeout: Per-command timeout in seconds.
    """
    if not cdp_url.strip():
        return "Error: cdp_url is required."
    if not command.strip():
        return "Error: command is required."

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _run_agent_browser(cdp_url=cdp_url, command=command, timeout=timeout),
    )


async def steel_close_session(session_id: str) -> str:
    """
    Close a Steel.dev browser session to stop billing.

    :param session_id: Steel session id returned by steel_create_session.
    """
    ok, err = await _steel_close_session_request(session_id=session_id.strip())
    if ok:
        return f"Steel session closed: {session_id}"
    return err or "Error closing Steel session."


async def browse_jina_then_steel(
    target: str,
    wait_for_selector: str = "",
    inject_page_script: str = "",
    steel_use_proxy: bool = True,
    steel_solve_captcha: bool = True,
) -> str:
    """
    Try Jina first and automatically fall back to Steel + agent-browser.

    :param target: URL or plain-language search query.
    :param wait_for_selector: Optional Jina wait selector.
    :param inject_page_script: Optional Jina page script.
    :param steel_use_proxy: Proxy setting for Steel fallback session.
    :param steel_solve_captcha: CAPTCHA solver setting for Steel fallback session.
    """
    target = (target or "").strip()
    if not target:
        return "Error: target is required."

    notes: list[str] = []
    url = target if _looks_like_url(target) else ""

    if not url:
        results, search_err = await _jina_search_request(
            query=target,
            num_results=1,
            site="",
            no_cache=False,
            respond_with="no-content",
            gl="US",
            hl="en",
            location="",
        )
        if search_err:
            notes.append(search_err)
        elif results:
            url = str(results[0].get("url", "")).strip()
            if url:
                notes.append(f"Resolved query to URL via Jina Search: {url}")
            else:
                notes.append("Jina Search returned no URL for fallback.")
        else:
            notes.append("Jina Search returned no results.")

    if url:
        data, read_err = await _jina_reader_request(
            url=url,
            return_format="markdown",
            wait_for_selector=wait_for_selector,
            target_selector="",
            remove_selector="",
            timeout_seconds=30,
            with_links_summary=True,
            with_generated_alt=True,
            set_cookie="",
            inject_page_script=inject_page_script,
            use_eu_endpoint=False,
            viewport_width=1920,
            viewport_height=1080,
        )
        if not read_err:
            content = str(data.get("content", "")).strip()
            if content:
                header = "Mode: Jina Reader (primary)\n"
                if notes:
                    header += "Notes:\n" + "\n".join(f"- {n}" for n in notes) + "\n\n"
                return header + _format_jina_reader_data(
                    url=url,
                    data=data,
                    return_format="markdown",
                )
            notes.append("Jina Reader returned empty content.")
        else:
            notes.append(read_err)
    else:
        notes.append("No URL available for Jina Reader.")

    if not url:
        return "Jina failed and Steel fallback could not start because no URL was resolved.\n" + "\n".join(
            f"- {n}" for n in notes
        )

    session_data, session_err = await _steel_create_session_request(
        use_proxy=steel_use_proxy,
        solve_captcha=steel_solve_captcha,
    )
    if session_err:
        notes.append(session_err)
        return "Jina failed and Steel fallback is unavailable.\n" + "\n".join(
            f"- {n}" for n in notes
        )

    session_id = str(session_data.get("id", "")).strip()
    cdp_url = str(session_data.get("websocketUrl", "")).strip()
    viewer_url = str(session_data.get("sessionViewerUrl", "")).strip()
    if not session_id or not cdp_url:
        return "Steel fallback failed: missing session id or CDP URL."

    outputs: list[str] = []
    try:
        for cmd in (f'open "{url}"', "wait --load networkidle", "snapshot -i"):
            out = await steel_agent_browser(cdp_url=cdp_url, command=cmd, timeout=90)
            outputs.append(f"$ agent-browser --cdp <CDP> {cmd}\n{out}")
    finally:
        closed, close_err = await _steel_close_session_request(session_id=session_id)
        if close_err:
            outputs.append(f"Cleanup warning: {close_err}")
        elif closed:
            outputs.append(f"Steel session closed: {session_id}")

    note_block = "\n".join(f"- {n}" for n in notes) if notes else "- (none)"
    output_block = "\n\n".join(outputs) if outputs else "(no Steel output)"
    return (
        "Mode: Steel fallback (Jina did not produce usable content)\n"
        f"URL: {url}\n"
        f"Session viewer: {viewer_url or '(not provided)'}\n"
        f"Notes:\n{note_block}\n\n"
        f"{output_block}"
    )


# ---------------------------------------------------------------------------
# Shell / Exec
# ---------------------------------------------------------------------------

def shell_exec(command: str, timeout: int = 30) -> str:
    """
    Execute a shell command and return its combined stdout + stderr output.

    Only non-destructive commands are permitted. Dangerous patterns
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
        if len(output) > 10_000:
            output = output[:10_000] + "\n... [output truncated]"
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
