---
name: python-executor
description: Execute ad-hoc Python snippets for quick calculations, parsing, transformations, and diagnostics. Use python_exec_unsafe only in trusted environments because execution is in-process and not sandbox-isolated.
---

# Python Executor

Use `python_exec_unsafe` for fast code execution when needed.

## Rules

- Treat execution as trusted-only.
- Prefer short deterministic snippets.
- Print explicit outputs for clarity.
- Avoid long-running code and infinite loops.

