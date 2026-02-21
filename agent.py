"""
agent.py — Core LLM agent loop and tool router.

The Agent class:
  1. Builds the system prompt from memory context + skills.
  2. Assembles messages from session history + new user input.
  3. Calls the LLM (via llm.py).
  4. If the LLM returns tool calls, executes them and loops back.
  5. Returns the final text response.

Tool discovery is automatic: any public function in tools.py and gworkspace.py
is registered and its JSON schema is derived from its signature + docstring.
"""

import asyncio
import inspect
from typing import Any, Callable

import llm as llm_module
import tools as tools_module
import gworkspace as gworkspace_module
from session import Session

MAX_TOOL_ITERATIONS = 25
MAX_ITERATIONS_SUMMARY_PROMPT = """The task reached the maximum number of tool iterations.
Provide a concise summary with:
1) What was accomplished
2) What remains incomplete
3) Recommended next action
Keep it brief and actionable."""


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def _discover_tools(*modules) -> dict[str, Callable]:
    """
    Collect all public, non-dunder callables from the given modules.
    Returns a dict mapping function name → callable.
    """
    registry: dict[str, Callable] = {}
    for mod in modules:
        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("_"):
                registry[name] = obj
    return registry


_TOOL_REGISTRY: dict[str, Callable] = _discover_tools(tools_module, gworkspace_module)
_TOOL_SCHEMAS: list[dict[str, Any]] = llm_module.build_tool_schemas(
    list(_TOOL_REGISTRY.values())
)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """
    Stateless agent wrapper.  A new instance can be created per request or
    shared; session state is managed entirely by the Session object.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    async def run(self, user_message: str) -> str:
        """
        Process a user message through the full agent loop and return the
        final text response.

        :param user_message: The raw text message from the user.
        """
        tools_module._set_active_session_id(self.session.session_id)

        # Persist user message
        self.session.add_message("user", user_message)

        # Build full message list for the LLM
        system_prompt = llm_module.build_system_prompt()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *self.session.get_messages(),
        ]
        incomplete_todos_prompted = False

        for _ in range(MAX_TOOL_ITERATIONS):
            response = await llm_module.chat_completion(messages, tools=_TOOL_SCHEMAS)
            text, tool_calls = llm_module.extract_response(response)

            if not tool_calls:
                # If there are unfinished todos, give the model one hidden chance
                # to continue before finalizing.
                if (
                    not incomplete_todos_prompted
                    and tools_module._session_has_incomplete_todos()
                ):
                    incomplete_todos_prompted = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You still have incomplete todos. Continue working, "
                                "update the todo list, and call done() only when finished."
                            ),
                        }
                    )
                    continue

                # Final text response
                final_text = text or "(no response)"
                self.session.add_message("assistant", final_text)
                return final_text

            # ----------------------------------------------------------------
            # Execute tool calls
            # ----------------------------------------------------------------
            # Append assistant message with tool_calls so the LLM sees its own actions
            assistant_msg = response.choices[0].message
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_msg.content,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": _safe_json(tc["arguments"]),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            # Also persist to session
            self.session.add_tool_call(messages[-1])

            for tc in tool_calls:
                result = await _execute_tool(tc["name"], tc["arguments"])
                tool_result_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": str(result),
                }
                messages.append(tool_result_msg)
                self.session.add_tool_result(tc["id"], tc["name"], str(result))

                done_text = _extract_done_message(tc["name"], str(result))
                if done_text is not None:
                    self.session.add_message("assistant", done_text)
                    return done_text

        final_text = await _summarize_max_iterations(messages)
        self.session.add_message("assistant", final_text)
        return final_text


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool by name, handling both sync and async functions."""
    fn = _TOOL_REGISTRY.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}'."

    try:
        if asyncio.iscoroutinefunction(fn):
            return await fn(**arguments)
        else:
            # Run blocking tool in a thread executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: fn(**arguments))
    except TypeError as exc:
        return f"Error calling tool '{name}': {exc}"
    except Exception as exc:
        return f"Tool '{name}' raised an error: {exc}"


def _extract_done_message(tool_name: str, result: str) -> str | None:
    """
    Extract final completion text when the done tool is used.
    """
    prefix = tools_module.TASK_COMPLETE_PREFIX
    text = (result or "").strip()
    if tool_name == "done" and text.startswith(prefix):
        final = text[len(prefix):].strip()
        return final or "Task completed."
    return None


async def _summarize_max_iterations(messages: list[dict[str, Any]]) -> str:
    """
    Ask the model for a compact progress summary when iteration limit is hit.
    """
    summary_messages = [*messages, {"role": "user", "content": MAX_ITERATIONS_SUMMARY_PROMPT}]
    try:
        response = await llm_module.chat_completion(summary_messages, tools=None)
        text, _ = llm_module.extract_response(response)
        if text and text.strip():
            return f"[Max iterations reached]\n\n{text.strip()}"
    except Exception:
        pass
    return "I reached the maximum number of tool iterations without a final answer."


def _safe_json(obj: Any) -> str:
    """Serialize an object to JSON string, falling back to str()."""
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)
