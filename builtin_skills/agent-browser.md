---
name: agent-browser
description: Browser automation workflow for agent-browser commands. Use when web tasks require navigation, clicking, form filling, waiting, screenshots, extraction, or repeated interaction in a real browser. When running on Steel cloud sessions, execute commands through steel_agent_browser with the session CDP URL.
---

# Browser Automation with agent-browser

Use this command pattern for reliable browser work:

1. Navigate: `open <url>`
2. Snapshot: `snapshot -i` to get refs (`@e1`, `@e2`, ...)
3. Interact: `click`, `fill`, `type`, `select`, `press`, `scroll`
4. Re-snapshot after navigation or major DOM change

## Steel Cloud Flow

When using Steel browser:

1. `steel_create_session` to get `cdp_url`
2. Run commands via `steel_agent_browser(cdp_url, command)`
3. `steel_close_session` for cleanup

## Core Command Sequence

Start with:

- `open https://example.com`
- `wait --load networkidle`
- `snapshot -i`

Then iterate:

- Use refs from snapshot output (`@e1`, `@e2`, ...)
- `click @eX` or `fill @eY "value"`
- `wait @eZ` or `wait --load networkidle`
- `snapshot -i` again when page changes

## High-value Commands

- `open <url>`
- `snapshot -i`
- `click @e1`
- `fill @e2 "text"`
- `type @e2 "text"`
- `select @e3 "option"`
- `check @e4`
- `press Enter`
- `wait @e1`
- `wait --load networkidle`
- `get text @e1`
- `get url`
- `get title`
- `screenshot --full`

## Reliability Rules

- Always snapshot before using refs.
- Never reuse stale refs after navigation.
- Prefer short step groups: navigate, wait, snapshot, act.
- If blocked or challenged by anti-bot defenses, use Steel with proxy and captcha solver enabled.

