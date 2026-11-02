"""One private-chat conversation for portal and Moodle sign-in.

Switching /grade <-> /deadline ends the previous login. Other bot commands
also end it, then run normally. Passwords never reach the AI text handler.

After a successful sign-in the student's request continues by itself:
/grade shows the grades, /deadline the deadlines, /documents the checklist,
/login the personal menu - and a question the AI assistant could not answer
without an account ("What are my grades?") is answered right away
(assistant_bot.resume). The assistant's [🔐 Sign in] button (auth:portal /
auth:moodle) enters this same conversation, so the password is only ever
typed into these steps, whose messages are deleted.
"""
from __future__ import annotations

from functools import wraps
import time

from telegram.ext import CommandHandler, ConversationHandler, CallbackQueryHandler, MessageHandler, filters

import deadline_bot as deadline
import documents_bot as docs
import grade_bot as grade
import i18n
import login_bot as login
import login_guard
import telegram_bot as bot

FLOW_TTL = 600
A = i18n.by_key("accounts")  # locales/<lang>.json, section "accounts"


def texts_for(feature):
    """The module whose t() words the sign-in messages of this feature."""
    return {"deadline": deadline, "assistant_moodle": deadline, "documents": docs, "login": login}.get(feature, grade)


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
        feature = context.user_data.get("login_flow", "grade")
        if time.monotonic() - context.user_data.get("login_started", 0) >= FLOW_TTL:
            await clear_login(update, context)
            if update.callback_query:
                await update.callback_query.answer()
            elif update.message and login_guard.looks_like_password(update.message.text):
                # Probably the password, typed after the sign-in expired: delete it too.
                await grade._forget(update.message)
                login_guard.note_failed_signin(context.user_data)
            module = texts_for(feature)
            await update.effective_message.reply_text(module.t("session_lost", bot.user_lang(update, context)))
            return ConversationHandler.END
        state = await callback(update, context)
        if state == ConversationHandler.END:
            await clear_login(update, context)
            # Signed in: continue with what the student asked for.
            if feature == "documents" and context.user_data.get("portal"):
                await docs.show_documents(update, context)
            elif feature == "login" and context.user_data.get("portal"):
                await login.after_login(update, context)
            elif feature == "grade" and context.user_data.get("portal"):
                await grade.show_after_sign_in(update, context)
            if feature in ("login", "assistant_portal", "assistant_moodle"):
                from assistant_bot import resume  # a question waiting for this sign-in
                await resume(update, context)
        return state
    return run


async def cancel(update, context):
    feature = context.user_data.get("login_flow", "grade")
    lang = bot.user_lang(update, context)
    await clear_login(update, context)
    module = texts_for(feature)
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
    await update.effective_message.reply_text(A["private_only"][bot.user_lang(update, context)])


async def assistant_portal(update, context):
    """[🔐 Sign in] under the AI assistant's offer: straight to the student number."""
    if context.user_data.get("portal"):  # signed in meanwhile: just answer the question
        from assistant_bot import resume
        await resume(update, context)
        return ConversationHandler.END
    await update.callback_query.edit_message_text(grade.t("ask_id", bot.user_lang(update, context)))
    return grade.ASK_ID


async def assistant_moodle(update, context):
    """The same for Moodle: straight to the Moodle username."""
    if context.user_data.get("moodle"):
        from assistant_bot import resume
        await resume(update, context)
        return ConversationHandler.END
    deadline._open_flow(update.effective_user.id)
    await update.callback_query.edit_message_text(deadline.t("ask_user", bot.user_lang(update, context)))
    return deadline.ASK_USER


def menu_entry(feature, callback):
    """Enter the shared sign-in conversation from a main-menu button."""
    begin = entry(feature, callback)

    async def run(update, context):
        await update.callback_query.answer()
        if update.effective_chat.type != "private":
            await private_only(update, context)
            return ConversationHandler.END
        return await begin(update, context)
    return run


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
        "review": docs.cmd_review,
    }
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(menu_entry("grade", grade.cmd_grade), pattern=r"^menu:grade$"),
            CallbackQueryHandler(menu_entry("deadline", deadline.cmd_deadline), pattern=r"^menu:deadline$"),
            CallbackQueryHandler(menu_entry("documents", docs.cmd_documents), pattern=r"^menu:documents$"),
            CallbackQueryHandler(menu_entry("assistant_portal", assistant_portal), pattern=r"^auth:portal$"),
            CallbackQueryHandler(menu_entry("assistant_moodle", assistant_moodle), pattern=r"^auth:moodle$"),
            CommandHandler("grade", entry("grade", grade.cmd_grade), filters=private),
            CommandHandler(["deadline", "deadlines"], entry("deadline", deadline.cmd_deadline), filters=private),
            CommandHandler("documents", entry("documents", docs.cmd_documents), filters=private),
            CommandHandler("login", entry("login", login.cmd_login), filters=private),
        ],
        states={
            grade.CHOOSE: [
                CallbackQueryHandler(step(grade.on_choice), pattern=r"^grade:(register|cancel)$"),
                CallbackQueryHandler(step(docs.on_signin_choice), pattern=r"^docs:(signin|nosignin)$"),
                CallbackQueryHandler(step(login.on_choice), pattern=r"^login:(signin|cancel)$"),
            ],
            grade.ASK_ID: [MessageHandler(only_text, step(grade.on_student_id))],
            grade.ASK_PASSWORD: [
                MessageHandler(only_text, step(grade.on_password)),
                CallbackQueryHandler(step(grade.on_change_id), pattern=r"^grade:newid$"),
            ],
            grade.ASK_CODE: [MessageHandler(only_text, step(grade.on_code))],
            deadline.CHOOSE: [CallbackQueryHandler(step(deadline.on_choice), pattern=r"^deadline:(register|cancel)$")],
            deadline.ASK_USER: [MessageHandler(only_text, step(deadline.on_username))],
            deadline.ASK_PASS: [
                MessageHandler(only_text, step(deadline.on_password)),
                CallbackQueryHandler(step(deadline.on_change_user), pattern=r"^deadline:newuser$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(leave(bot.on_menu), pattern=bot.MENU_PATTERN),
            CallbackQueryHandler(leave(bot.on_lang_chosen), pattern=r"^lang:"),
        ] + [
            CommandHandler(name, leave(callback)) for name, callback in commands.items()
        ] + [MessageHandler(filters.COMMAND, cancel)],
        allow_reentry=True,
    ))
    grade.register_grade(app)
    deadline.register_deadline(app)
    app.add_handler(CommandHandler(["grade", "deadline", "deadlines", "documents", "login"], private_only, filters=~private))
    app.add_handler(CommandHandler("cancel", cancel))
    for name in ("unlink", "unlink_moodle", "cancel"):
        bot.add_command(name, A[f"{name}_menu"], A[f"{name}_help"])
    bot.add_command("login", login.T["menu"], login.T["help_line"])
