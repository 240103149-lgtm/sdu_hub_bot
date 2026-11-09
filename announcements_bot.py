"""Telegram entry points for SDU academic news."""
from __future__ import annotations

import asyncio
import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import CallbackQueryHandler, CommandHandler

import announcements
from telegram_bot import add_command, keep_typing, reply, user_lang

log = logging.getLogger(__name__)


async def show_news(update, context, lang):
    message = update.effective_message
    typing = asyncio.create_task(keep_typing(context.bot, message.chat_id))
    try:
        response = await announcements.answer(lang)
    finally:
        typing.cancel()
    keyboard = None
    if message.chat.type == ChatType.PRIVATE and announcements.subscriptions_enabled():
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(announcements.label("subscribe", lang), callback_data="news:add"),
            InlineKeyboardButton(announcements.label("unsubscribe", lang), callback_data="news:remove"),
        ]])
    await reply(message, response, reply_markup=keyboard)


async def cmd_announcements(update, context):
    await show_news(update, context, user_lang(update, context))


async def on_subscription(update, context):
    query = update.callback_query
    lang = user_lang(update, context)
    chat = update.effective_chat
    if chat.type != ChatType.PRIVATE or update.effective_user.id != chat.id:
        await query.answer(announcements.label("private", lang))
        return
    if announcements.subscriptions_enabled():
        enabled = query.data == "news:add"
        try:
            await announcements.subscribe(chat.id, lang, enabled)
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            log.warning("SDU news subscription update failed: %s", type(exc).__name__)
        else:
            key = "subscribed" if enabled else "unsubscribed"
            await query.answer(announcements.label(key, lang))
            return
    await query.answer(announcements.label("unavailable", lang))


def register_announcements(app):
    add_command(
        "uni_announcments",
        {lang: texts["menu"] for lang, texts in announcements.TEXTS.items()},
        {lang: texts["help_line"] for lang, texts in announcements.TEXTS.items()},
    )
    app.add_handler(CommandHandler(["uni_announcments", "uni_announcements"], cmd_announcements))
    app.add_handler(CallbackQueryHandler(on_subscription, pattern=r"^news:(add|remove)$"))
