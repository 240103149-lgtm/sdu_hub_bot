"""/login - US7: sign in to SDU Hub with the university ID and password.

    /login  (private chat)
      |- already signed in ...... "signed in as 240103149", how to sign out
      '- [🔐 Sign in] -> student number -> password (deleted at once)
                      -> e-mail code if the portal asks -> signed in:
                         the personal main menu opens

The steps themselves are the shared portal sign-in (grade_bot, run by the
conversation in accounts_bot); the password is checked by the university's
own portal and never stored. login_guard locks an account after 5 wrong
passwords in a row and times every sign-in against the 3-second target.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import core
import grade_bot
import i18n
from telegram_bot import home_keyboard, user_lang

T = i18n.by_key("login")  # locales/<lang>.json, section "login"


def t(key: str, lang: str) -> str:
    return T[key].get(core.normalize_lang(lang), T[key]["en"])


def _student_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    return str(((context.user_data or {}).get("portal") or {}).get("student_id") or "")


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point of the shared sign-in conversation (accounts_bot)."""
    lang = user_lang(update, context)
    student_id = _student_id(context)
    if student_id:
        await update.effective_message.reply_text(
            t("already", lang).format(id=student_id), reply_markup=home_keyboard(lang)
        )
        return ConversationHandler.END
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("btn_signin", lang), callback_data="login:signin"),
        InlineKeyboardButton(t("btn_cancel", lang), callback_data="login:cancel"),
    ]])
    await update.effective_message.reply_text(t("intro", lang), reply_markup=keyboard)
    return grade_bot.CHOOSE


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    query = update.callback_query
    await query.answer()
    if query.data == "login:cancel":
        await query.edit_message_text(t("cancelled", lang))
        return ConversationHandler.END
    await query.edit_message_text(grade_bot.t("ask_id", lang))
    return grade_bot.ASK_ID


async def after_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """QA: valid ID and password -> the student lands on the personal main menu."""
    lang = user_lang(update, context)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=t("signed_in", lang).format(id=_student_id(context)),
        reply_markup=home_keyboard(lang),
    )
