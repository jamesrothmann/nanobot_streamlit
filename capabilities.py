"""
capabilities.py - Persist reusable prompt-based capabilities in Drive storage.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import drive_sync

CAPABILITIES_FILE = "CAPABILITIES.json"
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict[str, Any]]:
    raw = drive_sync.read_file(CAPABILITIES_FILE).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _save(items: list[dict[str, Any]]) -> None:
    drive_sync.write_file(CAPABILITIES_FILE, json.dumps(items, ensure_ascii=False, indent=2))


def list_capabilities() -> list[dict[str, Any]]:
    return _load()


def create_capability(
    name: str,
    template: str,
    description: str = "",
    defaults: dict[str, str] | None = None,
    source_prompt: str = "",
    source_tools: list[str] | None = None,
) -> dict[str, Any]:
    item = {
        "id": str(uuid.uuid4())[:8],
        "name": (name or "Unnamed capability").strip(),
        "template": (template or "").strip(),
        "description": (description or "").strip(),
        "defaults": {str(k): str(v) for k, v in (defaults or {}).items()},
        "source_prompt": (source_prompt or "").strip(),
        "source_tools": [str(x) for x in (source_tools or []) if str(x).strip()],
        "created_at_utc": _now_iso_utc(),
    }
    items = _load()
    items.append(item)
    _save(items)
    return item


def delete_capability(capability_id: str) -> bool:
    cid = (capability_id or "").strip()
    if not cid:
        return False
    items = _load()
    kept = [x for x in items if str(x.get("id", "")).strip() != cid]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def template_vars(template: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _VAR_RE.finditer(template or ""):
        key = match.group(1)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def render_template(template: str, values: dict[str, str] | None = None) -> tuple[str, list[str]]:
    vals = {str(k): str(v) for k, v in (values or {}).items()}
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = vals.get(key, "")
        if value.strip():
            return value
        missing.append(key)
        return match.group(0)

    rendered = _VAR_RE.sub(_replace, template or "")
    return rendered, missing
