---
name: workspace-file-ops
description: Local workspace file operations for reading, writing, editing, and directory listing. Use for coding and document workflows that require direct filesystem manipulation within the configured workspace root.
---

# Workspace File Ops

Core tools:

1. `list_dir(path, recursive, max_entries)`
2. `read_file(path, max_chars)`
3. `write_file(path, content, append)`
4. `edit_file(path, old_text, new_text, replace_all)`

## Safety

- Paths are restricted to the configured workspace root.
- Use relative paths whenever possible.
- For precise edits, prefer `edit_file` over full overwrite.

