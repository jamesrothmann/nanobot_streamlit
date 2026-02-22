"""
telegram_bot.py — Async Telegram polling bot running in a background thread.

Architecture:
  - Spawned once per container lifecycle via @st.cache_resource in app.py.
  - Runs its own asyncio event loop inside a daemon thread.
  - Shares no state with Streamlit's main thread (all state is in Drive/Session).

Security:
  - Only Telegram identities listed in secrets["telegram"] allow-lists are served.
  - All other senders receive a polite rejection message.
"""

import asyncio
import logging

import streamlit as st
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import Agent
from session import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not _is_allowed(update):
        uid = update.effective_user.id
        uname = (update.effective_user.username or "(no username)")
        await update.message.reply_text(
            f"Unauthorized. Your Telegram id is `{uid}` and username is @{uname}."
        )
        return
    await update.message.reply_text(
        "Hello! I'm your Nanobot assistant. Send me a message and I'll get to work."
    )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all incoming text messages."""
    if not _is_allowed(update):
        uid = update.effective_user.id
        uname = (update.effective_user.username or "(no username)")
        await update.message.reply_text(
            f"Unauthorized. Your Telegram id is `{uid}` and username is @{uname}."
        )
        return

    user_id = update.effective_user.id
    user_text = update.message.text or ""
    if not user_text.strip():
        return

    # Each Telegram user gets their own persistent session
    session_id = f"tg_{user_id}"
    session = Session(session_id)
    agent = Agent(session)

    # Show typing indicator while processing
    await update.message.chat.send_action("typing")

    try:
        response = await agent.run(user_text)
    except Exception as exc:
        logger.exception("Agent error in Telegram handler")
        response = f"Sorry, I encountered an error: {exc}"

    # Telegram has a 4096-character message limit
    for chunk in _split_message(response, max_len=4000):
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _as_list(value) -> list[str]:
    """
    Normalize secrets values into a list of strings.

    Accepts list/tuple/set, comma-separated strings, or single scalars.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return [text]


def _is_allowed(update: Update) -> bool:
    """
    Return True if sender is in configured allow-lists.

    Supports:
      telegram.allowed_users: ["username_without_at", ...]
      telegram.allowed_user_ids: [123456789, ...]
      telegram.allowed_user_id: 123456789
    """
    tg = dict(st.secrets.get("telegram", {}))

    allowed_users = {
        str(u).lstrip("@").strip()
        for u in _as_list(tg.get("allowed_users", []))
        if str(u).strip()
    }
    allowed_ids_values = _as_list(tg.get("allowed_user_ids", []))
    allowed_ids_values.extend(_as_list(tg.get("allowed_user_id", "")))
    allowed_ids = {str(i).strip() for i in allowed_ids_values if str(i).strip()}

    username = (update.effective_user.username or "").lstrip("@").strip()
    user_id = str(update.effective_user.id or "").strip()

    if not allowed_users and not allowed_ids:
        return False
    return (username in allowed_users) or (user_id in allowed_ids)


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


# ---------------------------------------------------------------------------
# Bot entry point (called from a background thread)
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """
    Build and start the Telegram bot with long-polling.
    This function blocks indefinitely and should be run in a daemon thread.
    """
    tg = dict(st.secrets.get("telegram", {}))
    enabled = bool(tg.get("enabled", True))
    token = str(tg.get("token", "")).strip()
    if not enabled:
        logger.info("Telegram bot disabled by config.")
        return
    if not token:
        logger.info("Telegram token not configured; bot not started.")
        return

    # Create a dedicated event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _main() -> None:
        app = (
            Application.builder()
            .token(token)
            .build()
        )
        app.add_handler(CommandHandler("start", _start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

        logger.info("Telegram bot starting...")
        # initialize + start + run polling — all within this coroutine
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        # Keep the loop alive
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(_main())
    except Exception:
        logger.exception("Telegram bot crashed")
    finally:
        loop.close()
