#!/usr/bin/env python3
"""
Basic smoke checks for nanobot-streamlit runtime wiring.
"""

from __future__ import annotations

import importlib
import inspect
import sys


REQUIRED_TOOLS = {
    "python_exec_unsafe",
    "todo_read",
    "todo_write",
    "done",
    "spawn_subagent",
    "cron_create",
    "cron_list",
    "cron_delete",
    "cron_run_due",
    "mcp_list_servers",
    "mcp_call",
    "list_dir",
    "read_file",
    "write_file",
    "edit_file",
}


def main() -> int:
    errors: list[str] = []

    for module_name in ("tools", "agent", "app", "telegram_bot", "cron_service"):
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"Import failed for {module_name}: {exc}")

    try:
        tools = importlib.import_module("tools")
        public_funcs = {
            name
            for name, obj in inspect.getmembers(tools, inspect.isfunction)
            if not name.startswith("_")
        }
        missing = sorted(REQUIRED_TOOLS - public_funcs)
        if missing:
            errors.append(f"Missing expected tool functions: {', '.join(missing)}")
    except Exception as exc:
        errors.append(f"Tool inspection failed: {exc}")

    if errors:
        print("Smoke check failed:")
        for err in errors:
            print(f"- {err}")
        return 1

    print("Smoke check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
