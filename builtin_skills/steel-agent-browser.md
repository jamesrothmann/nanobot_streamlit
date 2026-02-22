---
name: steel-agent-browser
description: Cloud browser automation via Steel.dev sessions and agent-browser CLI. Use when a task requires resilient stateful browsing, anti-bot bypass, CAPTCHA solving, or multi-step interaction that Jina cannot complete.
---

# Steel + agent-browser Workflow

Use Steel as fallback or when explicitly requested.
<<<<<<< HEAD
Also load and follow the `agent-browser` skill for command-level interaction patterns.
=======
>>>>>>> 51fb39dfc197a1edee6e1ae0b6987a81b46b861c

1. `steel_create_session`
2. `steel_agent_browser` for commands against the returned CDP URL
3. `steel_close_session` to stop billing

## Best Practices

- Prefer a short command sequence: `open`, `wait --load networkidle`, `snapshot -i`.
- Re-run `snapshot -i` after each navigation before targeting refs.
- Keep session lifetime short; close immediately after collecting output.
- Include `use_proxy=true` and `solve_captcha=true` unless the user requests otherwise.

## Fast Path

For URL-first tasks where Jina may fail, use `browse_jina_then_steel`. It tries Jina Reader first and automatically falls back to a temporary Steel session if needed.
<<<<<<< HEAD
=======

>>>>>>> 51fb39dfc197a1edee6e1ae0b6987a81b46b861c
