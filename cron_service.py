"""
cron_service.py - Lightweight recurring task scheduler backed by Drive storage.

Tasks are persisted in CRON_TASKS.json in the workspace and can be executed
from app boot hooks or via cron_* tools.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import drive_sync

CRON_FILE = "CRON_TASKS.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _load_tasks() -> list[dict[str, Any]]:
    raw = drive_sync.read_file(CRON_FILE).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [dict(x) for x in parsed if isinstance(x, dict)]
        return []
    except Exception:
        return []


def _save_tasks(tasks: list[dict[str, Any]]) -> None:
    drive_sync.write_file(CRON_FILE, json.dumps(tasks, ensure_ascii=False, indent=2))


def create_task(name: str, prompt: str, interval_minutes: int, session_id: str) -> dict[str, Any]:
    task = {
        "id": str(uuid.uuid4())[:8],
        "name": name or "Unnamed cron task",
        "prompt": prompt,
        "interval_minutes": max(1, int(interval_minutes)),
        "session_id": session_id,
        "created_at_utc": _dt_to_iso(_now_utc()),
        "next_run_utc": _dt_to_iso(_now_utc() + timedelta(minutes=max(1, int(interval_minutes)))),
        "last_run_utc": "",
        "enabled": True,
    }
    tasks = _load_tasks()
    tasks.append(task)
    _save_tasks(tasks)
    return task


def list_tasks() -> list[dict[str, Any]]:
    return _load_tasks()


def delete_task(task_id: str) -> bool:
    tasks = _load_tasks()
    remaining = [t for t in tasks if str(t.get("id", "")) != task_id]
    if len(remaining) == len(tasks):
        return False
    _save_tasks(remaining)
    return True


async def run_due_tasks(limit: int = 3) -> list[dict[str, Any]]:
    """
    Execute up to `limit` tasks whose next_run_utc <= now.
    """
    from agent import Agent
    from session import Session

    now = _now_utc()
    tasks = _load_tasks()
    due = []
    for t in tasks:
        if not bool(t.get("enabled", True)):
            continue
        next_run = str(t.get("next_run_utc", "")).strip()
        if not next_run:
            continue
        try:
            if _iso_to_dt(next_run) <= now:
                due.append(t)
        except Exception:
            continue

    due = due[: max(1, int(limit))]
    results: list[dict[str, Any]] = []

    for task in due:
        status = "ok"
        result_text = ""
        try:
            session = Session(str(task.get("session_id", "cron_default")))
            agent = Agent(session)
            result_text = await agent.run(str(task.get("prompt", "")))
        except Exception as exc:
            status = "error"
            result_text = f"{exc}"

        interval = max(1, int(task.get("interval_minutes", 60)))
        task["last_run_utc"] = _dt_to_iso(_now_utc())
        task["next_run_utc"] = _dt_to_iso(_now_utc() + timedelta(minutes=interval))
        results.append(
            {
                "id": task.get("id", ""),
                "name": task.get("name", ""),
                "status": status,
                "result": result_text,
                "next_run_utc": task.get("next_run_utc", ""),
            }
        )

    if due:
        # Persist schedule updates back to full task list.
        due_ids = {str(t.get("id", "")) for t in due}
        task_map = {str(t.get("id", "")): t for t in due}
        merged = [task_map.get(str(t.get("id", "")), t) if str(t.get("id", "")) in due_ids else t for t in tasks]
        _save_tasks(merged)

    return results


def run_due_tasks_sync(limit: int = 3) -> str:
    """
    Sync helper for Streamlit boot path.
    """
    try:
        results = asyncio.run(run_due_tasks(limit=limit))
    except RuntimeError:
        # Fallback when called under an already-running event loop.
        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(run_due_tasks(limit=limit))
    if not results:
        return "No due cron tasks."
    return "\n".join(
        f"- {r.get('id','')} {r.get('status','ok')} next={r.get('next_run_utc','')}"
        for r in results
    )
