"""
skills.py — Markdown skill loader.

Skills are plain Markdown files stored in the Google Drive workspace folder
under a virtual "skills/" prefix (named "skills/<name>.md").

On boot, drive_sync downloads them alongside MEMORY.md etc.
This module reads them from the local /tmp/workspace/skills/ cache and
concatenates them into a single block for injection into the system prompt.

Each skill file should follow the convention:
  # Skill: <Name>
  <description and usage instructions>
"""

from pathlib import Path

import streamlit as st

# Skills are stored under /tmp/workspace/skills/
SKILLS_DIR = Path("/tmp/workspace/skills")
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin_skills"


def _ensure_skills_dir() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _seed_builtin_skills() -> None:
    """
    Copy bundled skill Markdown files into the local cache when missing.

    Built-ins are intentionally one-way seeded so user-managed skill files in
    Drive can override/edit them without being clobbered on every boot.
    """
    if not BUILTIN_SKILLS_DIR.exists():
        return

    for src in sorted(BUILTIN_SKILLS_DIR.glob("*.md")):
        dst = SKILLS_DIR / src.name
        if not dst.exists():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def load_all_skills() -> str:
    """
    Load all skill Markdown files from the local skills cache and
    return them concatenated as a single string.

    Returns an empty string if no skills are present.
    """
    _ensure_skills_dir()
    _seed_builtin_skills()
    skill_files = sorted(SKILLS_DIR.glob("*.md"))
    if not skill_files:
        return ""

    system_cfg = dict(st.secrets.get("system", {}))
    include_chat_oauth_skill = bool(system_cfg.get("enable_chat_oauth_skill", False))

    parts: list[str] = []
    for path in skill_files:
        if path.name == "google-oauth-onboarding.md" and not include_chat_oauth_skill:
            continue
        content = path.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)

    return "\n\n---\n\n".join(parts)


def sync_skills_from_drive() -> int:
    """
    Download all files from the Drive workspace folder whose names start with
    'skills/' and cache them locally.

    Returns the number of skill files synced.

    Note: This is called during boot_sync in drive_sync.py if the folder
    contains skill files.  It can also be called on-demand.
    """
    import drive_sync

    _ensure_skills_dir()
    service = drive_sync._drive_service()
    folder_id = drive_sync._folder_id()

    resp = (
        service.files()
        .list(
            q=(
                f"'{folder_id}' in parents "
                f"and name contains 'skills_' "
                f"and trashed=false"
            ),
            fields="files(id, name)",
            spaces="drive",
        )
        .execute()
    )

    count = 0
    for f in resp.get("files", []):
        name: str = f["name"]
        # Expect naming convention: skills_<name>.md
        local_name = name.replace("skills_", "", 1)
        data = drive_sync._download_file(f["id"])
        (SKILLS_DIR / local_name).write_bytes(data)
        count += 1

    _seed_builtin_skills()
    return count


def write_skill(name: str, content: str) -> str:
    """
    Save a new skill file both locally and to Google Drive.

    :param name: Skill filename (without .md extension).
    :param content: Full Markdown content of the skill.
    """
    import drive_sync

    _ensure_skills_dir()
    filename = f"skills_{name}.md"
    local_path = SKILLS_DIR / f"{name}.md"
    local_path.write_text(content, encoding="utf-8")
    drive_sync._upload_or_update(filename, content)
    return f"Skill '{name}' saved."
