---
name: agentic-execution
description: Structured multi-step execution loop inspired by browser-use Agent SDK. Use for complex tasks that require planning, progress tracking, iterative tool use, and explicit task completion signaling.
---

# Agentic Execution Pattern

For non-trivial tasks, run this loop:

1. Initialize plan with `todo_write`.
2. Execute work in steps using tools.
3. Keep todo statuses updated (`[ ]`, `[>]`, `[x]`) as the task progresses.
4. Call `done` with a clear completion message only when all critical work is complete.

## Todo Format

Use status prefixes in each todo item:

- `[ ]` pending
- `[>]` in progress
- `[x]` completed

Example:

- `[>] gather requirements`
- `[ ] implement changes`
- `[ ] validate and report`

## Completion Rule

Before finishing, ensure there are no critical pending todos.
If work remains, continue tool execution and update todos.

