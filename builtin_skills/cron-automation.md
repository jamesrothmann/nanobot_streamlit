---
name: cron-automation
description: Recurring autonomous task scheduling. Use when the user wants repetitive assistant workflows (daily summaries, periodic checks, recurring reminders) to run automatically.
---

# Cron Automation

Manage recurring tasks with:

1. `cron_create(name, prompt, interval_minutes, session_id)`
2. `cron_list()`
3. `cron_delete(task_id)`
4. `cron_run_due(limit)` for manual trigger/testing

## Notes

- Keep prompts self-contained because tasks may run later.
- Use dedicated session IDs for recurring workflows when context separation matters.
- Review `next_run_utc` to verify schedule behavior.

