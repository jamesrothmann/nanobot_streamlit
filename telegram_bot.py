"""
telegram_bot.py — Async Telegram polling bot running in a background thread.

Architecture:
  - Spawned once per container lifecycle via @st.cache_resource in app.py.
  - Runs its own asyncio event loop inside a daemon thread.
  - Shares no state with Streamlit's main thread (all state is in Drive/Session).

Security:
  - Only Telegram usernames listed in secrets["telegram"]["allowed_users"] are served.
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
        await update.message.reply_text("Sorry, you are not authorised to use this bot.")
        return
    await update.message.reply_text(
        "Hello! I'm your Nanobot assistant. Send me a message and I'll get to work."
    )


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all incoming text messages."""
    if not _is_allowed(update):
        await update.message.reply_text("Unauthorised.")
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

def _is_allowed(update: Update) -> bool:
    """Return True if the sender is in the allowed users list."""
    allowed: list[str] = list(st.secrets["telegram"].get("allowed_users", []))
    username = (update.effective_user.username or "").lstrip("@")
    return username in allowed


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
    token: str = st.secrets["telegram"]["token"]

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
