---
name: google-workspace-assistant
description: Full Google Workspace assistant workflow across Gmail, Calendar, Drive, Docs, and Sheets. Use when the user asks to read, draft, send, plan, schedule, summarize, create documents, update spreadsheets, search files, or automate office operations in Google Workspace.
---

# Google Workspace Assistant

Use these tools as the default backbone for office tasks:

1. `google_workspace_identity` to verify delegated mode and access identity.
2. Use Gmail tools for communication (`read_recent_emails`, `read_email_thread`, `draft_email`, `send_email`).
3. Use Calendar tools for scheduling (`list_calendars`, `list_upcoming_events`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`).
4. Use Drive/Docs/Sheets tools for document workflows.

## Operational Rules

- Confirm target IDs before destructive changes (event delete, sheet clear).
- Prefer drafts before sending external emails when intent is ambiguous.
- For Sheets writes, pass data as JSON rows (for example: `[[\"task\",\"owner\"],[\"Ship v1\",\"James\"]]`).
- If permissions fail, report whether delegation is configured and what resource must be shared.

