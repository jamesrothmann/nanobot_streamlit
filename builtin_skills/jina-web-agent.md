---
name: jina-web-agent
description: Stateless, LLM-optimized web interaction with Jina Reader and Search APIs. Use when the user needs fast extraction, search, link discovery, content summarization, screenshot capture, or simple scripted page interaction without stateful browser automation. Always try Jina first, then fall back to steel-agent-browser only when Jina fails, is blocked, or cannot complete the interaction.
---

# Jina-First Web Workflow

Follow this order for every web task:

1. Use `browse_jina_then_steel` for automatic fallback (recommended default).
2. If you need control, run `jina_search` then `jina_read` manually.
3. Use Steel only after Jina failure or for heavy stateful automation.

## Jina Search

Use `jina_search` to discover URLs quickly.

- Prefer `num_results` 3-8.
- Set `site` for documentation/domain-restricted queries.
- Use `no_cache=true` when freshness matters.

## Jina Reader

Use `jina_read` for extraction and lightweight interaction.

- Always keep `return_format` aligned with the task (`markdown`, `html`, `text`, `pageshot`).
- Use `wait_for_selector` for dynamic pages.
- Use `target_selector` to focus extraction.
- Use `remove_selector` to exclude banners/chrome.
- Use `inject_page_script` for one-shot interactions (accept cookie, fill + submit).
- Use `set_cookie` for authenticated reads.

## Escalation To Steel

Escalate to Steel when any of these occur:

- Reader content is empty or clearly incomplete after tuned retry.
- The page requires multi-step interaction beyond one script injection.
- Anti-bot protections block Jina results.
- The user explicitly requests cloud browser automation.

