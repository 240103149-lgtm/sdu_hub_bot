"""One private-chat conversation for portal and Moodle sign-in.

Switching /grade <-> /deadline ends the previous login. Other bot commands
also end it, then run normally. Passwords never reach the AI text handler.
"""
from __future__ import annotations

from functools import wraps
import time

from telegram.ext import CommandHandler, ConversationHandler, CallbackQueryHandler, MessageHandler, filters

import deadline_bot as deadline
import grade_bot as grade
import telegram_bot as bot

FLOW_TTL = 600


async def clear_login(update, context):
    await grade._drop_pending(update.effective_user.id)
    context.user_data.pop("grade_sid", None)
    deadline._close_flow(update.effective_user.id, context)
    context.user_data.pop("login_flow", None)
    context.user_data.pop("login_started", None)


def entry(feature, callback):
    @wraps(callback)
    async def run(update, context):
        await clear_login(update, context)
        context.user_data["login_flow"] = feature
        context.user_data["login_started"] = time.monotonic()
        state = await callback(update, context)
        if state == ConversationHandler.END:
            await clear_login(update, context)
        return state
    return run


def step(callback):
    @wraps(callback)
    async def run(update, context):
        if time.monotonic() - context.user_data.get("login_started", 0) >= FLOW_TTL:
            feature = context.user_data.get("login_flow", "grade")
            await clear_login(update, context)
            if update.callback_query:
                await update.callback_query.answer()
            module = deadline if feature == "deadline" else grade
            await update.effective_message.reply_text(module.t("session_lost", bot.user_lang(update, context)))
            return ConversationHandler.END
        state = await callback(update, context)
        if state == ConversationHandler.END:
            await clear_login(update, context)
        return state
    return run


async def cancel(update, context):
    feature = context.user_data.get("login_flow", "grade")
    lang = bot.user_lang(update, context)
    await clear_login(update, context)
    module = deadline if feature == "deadline" else grade
    await update.effective_message.reply_text(module.t("cancelled", lang))
    return ConversationHandler.END


def leave(callback):
    @wraps(callback)
    async def run(update, context):
        await clear_login(update, context)
        await callback(update, context)
        return ConversationHandler.END
    return run


async def private_only(update, context):
    texts = {
        "kk": "Аккаунтты байланыстыру үшін боттың жеке чатына жазыңыз.",
        "ru": "Чтобы связать аккаунт, напишите боту в личный чат.",
        "en": "Please link your account in a private chat with the bot.",
    }
    await update.message.reply_text(texts[bot.user_lang(update, context)])


def register_accounts(app):
    from events_bot import cmd_events
    from rooms_bot import cmd_rooms

    private = filters.ChatType.PRIVATE
    only_text = filters.TEXT & ~filters.COMMAND & private
    commands = {
        "start": bot.cmd_start, "help": bot.cmd_help, "lang": bot.cmd_lang,
        "reset": bot.cmd_reset, "guide": bot.cmd_guide,
        "rooms": cmd_rooms, "room": cmd_rooms, "events": cmd_events,
        "unlink": grade.cmd_unlink, "unlink_moodle": deadline.cmd_unlink_moodle,
    }
    app.add_handler(ConversationHandler(
        entry_points=[
            CommandHandler("grade", entry("grade", grade.cmd_grade), filters=private),
            CommandHandler(["deadline", "deadlines"], entry("deadline", deadline.cmd_deadline), filters=private),
        ],
        states={
            grade.CHOOSE: [CallbackQueryHandler(step(grade.on_choice), pattern=r"^grade:(register|cancel)$")],
            grade.ASK_ID: [MessageHandler(only_text, step(grade.on_student_id))],
            grade.ASK_PASSWORD: [MessageHandler(only_text, step(grade.on_password))],
            grade.ASK_CODE: [MessageHandler(only_text, step(grade.on_code))],
            deadline.CHOOSE: [CallbackQueryHandler(step(deadline.on_choice), pattern=r"^deadline:(register|cancel)$")],
            deadline.ASK_USER: [MessageHandler(only_text, step(deadline.on_username))],
            deadline.ASK_PASS: [MessageHandler(only_text, step(deadline.on_password))],
        },
        fallbacks=[CommandHandler("cancel", cancel)] + [
            CommandHandler(name, leave(callback)) for name, callback in commands.items()
        ] + [MessageHandler(filters.COMMAND, cancel)],
        allow_reentry=True,
    ))
    grade.register_grade(app)
    deadline.register_deadline(app)
    app.add_handler(CommandHandler(["grade", "deadline", "deadlines"], private_only, filters=~private))
    app.add_handler(CommandHandler("cancel", cancel))
    for name, menu, help_line in [
        ("unlink", {"kk": "Портал байланысын өшіру", "ru": "Отвязать портал", "en": "Unlink portal"},
         {"kk": "/unlink — портал байланысын өшіру", "ru": "/unlink — отвязать портал", "en": "/unlink — unlink portal"}),
        ("unlink_moodle", {"kk": "Moodle байланысын өшіру", "ru": "Отвязать Moodle", "en": "Unlink Moodle"},
         {"kk": "/unlink_moodle — Moodle байланысын өшіру", "ru": "/unlink_moodle — отвязать Moodle", "en": "/unlink_moodle — unlink Moodle"}),
        ("cancel", {"kk": "Кіруді тоқтату", "ru": "Отменить вход", "en": "Cancel sign-in"},
         {"kk": "/cancel — кіруді тоқтату", "ru": "/cancel — отменить вход", "en": "/cancel — cancel sign-in"}),
    ]:
        bot.add_command(name, menu, help_line)
