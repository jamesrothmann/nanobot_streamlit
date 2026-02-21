"""
llm.py — LiteLLM wrapper and prompt construction.

Responsible for:
  - Injecting API keys from st.secrets into the environment
  - Building the system prompt from memory context + skills
  - Making async LLM calls via litellm.acompletion
  - Extracting tool call / text responses from the LLM reply
"""

import asyncio
import inspect
import json
import os
from typing import Any, Callable

import litellm
import streamlit as st

import memory
import skills as skills_module


def _configure_env() -> None:
    """Push API keys from secrets into environment variables for LiteLLM."""
    llm_secrets = dict(st.secrets.get("llm", {}))
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        if key in llm_secrets:
            os.environ[key] = llm_secrets[key]


_configure_env()


# ---------------------------------------------------------------------------
# Tool schema generation
# ---------------------------------------------------------------------------

def _python_type_to_json_schema(annotation) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema type descriptor."""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        args = getattr(annotation, "__args__", (str,))
        return {"type": "array", "items": _python_type_to_json_schema(args[0])}
    mapping = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }
    return mapping.get(annotation, {"type": "string"})


def build_tool_schemas(tool_fns: list[Callable]) -> list[dict[str, Any]]:
    """
    Dynamically inspect Python functions and produce OpenAI-compatible
    tool JSON schemas from their signatures and docstrings.
    """
    schemas = []
    for fn in tool_fns:
        sig = inspect.signature(fn)
        doc = inspect.getdoc(fn) or fn.__name__
        # First line of docstring = tool description
        description = doc.splitlines()[0].strip()

        properties: dict[str, Any] = {}
        required: list[str] = []

        for name, param in sig.parameters.items():
            prop = _python_type_to_json_schema(param.annotation)
            # Pull per-param description from docstring if present (":param name: …")
            for line in doc.splitlines():
                tag = f":param {name}:"
                if tag in line:
                    prop["description"] = line.split(tag, 1)[1].strip()
                    break
            properties[name] = prop
            if param.default is inspect.Parameter.empty:
                required.append(name)

        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return schemas


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """
    Assemble the full system prompt:
      1. Memory context (AGENTS.md, USER.md, MEMORY.md, HISTORY.md)
      2. Loaded skills (from Google Drive workspace)
    """
    parts = [memory.build_memory_context()]

    skills_text = skills_module.load_all_skills()
    if skills_text.strip():
        parts.append(f"# Available Skills\n{skills_text}")

    return "\n\n---\n\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

async def chat_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> Any:
    """
    Call the LLM via LiteLLM and return the raw response object.

    :param messages: Full conversation messages list (system + history + new user msg).
    :param tools: Optional list of tool schemas.
    """
    model = st.secrets["llm"].get("model", "anthropic/claude-3-5-sonnet-20241022")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.7,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    response = await litellm.acompletion(**kwargs)
    return response


def extract_response(response: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Parse a LiteLLM response and return (text_content, tool_calls).

    Returns:
        text_content  — The assistant's text reply, or None if it made tool calls.
        tool_calls    — List of tool call dicts (may be empty).
    """
    choice = response.choices[0]
    message = choice.message

    tool_calls = []
    if hasattr(message, "tool_calls") and message.tool_calls:
        for tc in message.tool_calls:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments or "{}"),
                }
            )

    text = message.content or None
    return text, tool_calls
