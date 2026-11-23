"""Private reminders sourced only from the student's linked accounts."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

import i18n
import rooms
import grade
import notifications
from telegram_bot import reply, user_lang


def labels(lang):
    return i18n.section(lang, "notifications")


def settings(data, lang, paused=False):
    """The reminder card. `paused`: nothing will deliver later (see
    notifications.delivery_paused), so the card says so instead of implying
    that a switched-on reminder will arrive."""
    t = labels(lang)
    state = lambda key: t["on"] if notifications.enabled(data, key) else t["off"]
    keyboard = [
        [InlineKeyboardButton(f"{t['deadlines']} · {state('deadline_notifications')}", callback_data="notify:deadline")],
        [InlineKeyboardButton(f"{t['classes']} · {state('class_notifications')}", callback_data="notify:class")],
    ]
    if not data.get("portal"):
        keyboard.append([InlineKeyboardButton(t["connect_portal"], callback_data="notify:link_portal")])
    if not data.get("moodle"):
        keyboard.append([InlineKeyboardButton(t["connect_moodle"], callback_data="notify:link_moodle")])
    text = t["settings"] + "\n\n" + t["timing_short"]
    if paused:
        text += "\n\n⚠️ " + t["service_paused"]
    return text, InlineKeyboardMarkup(keyboard)


async def render_card(update, text, keyboard):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    else:
        await reply(update.effective_message, text, reply_markup=keyboard)


async def cmd_notifications(update, context):
    lang = user_lang(update, context)
    if update.effective_chat.type != "private":
        await reply(update.effective_message, labels(lang)["private"])
        return
    text, keyboard = settings(context.user_data, lang, paused=await notifications.delivery_paused())
    await render_card(update, text, keyboard)


async def on_settings(update, context):
    await update.callback_query.answer()
    if update.effective_chat.type != "private":
        return
    action = update.callback_query.data.split(":")[1]
    if action in ("deadline", "class"):
        key = "deadline_notifications" if action == "deadline" else "class_notifications"
        context.user_data[key] = not notifications.enabled(context.user_data, key)
        context.user_data.pop("reminders_blocked", None)
    await cmd_notifications(update, context)
    if action in ("deadline", "class") and notifications.enabled(context.user_data, key):
        await notifications.check_now(context.application, update.effective_user.id)


async def personal_schedule_card(data, lang, query="", page=0):
    t = labels(lang)
    if not data.get("portal"):
        return t["connect_for_schedule"], InlineKeyboardMarkup([
            [InlineKeyboardButton(t["connect_portal"], callback_data="notify:link_portal")],
            [InlineKeyboardButton(t["settings"], callback_data="notify:home")],
        ])
    try:
        items = await notifications.personal_classes(data)
    except grade.PortalError as exc:
        if exc.key in ("session_expired", "two_factor_required"):
            data.pop("portal", None)
            data.pop("personal_schedule_cache", None)
            text, keyboard = await personal_schedule_card(data, lang, query, page)
            if exc.key == "two_factor_required":
                text = t["portal_two_factor_required"] + "\n\n" + text
            return text, keyboard
        return t["unavailable"], InlineKeyboardMarkup([
            [InlineKeyboardButton(t["schedule"], callback_data="schedule:home")],
            [InlineKeyboardButton(t["settings"], callback_data="notify:home")],
        ])
    page = max(0, min(page, max(0, (len(items) - 1) // 8)))
    lines = [t["schedule"], t["portal_schedule"]]
    for item in items[page * 8:(page + 1) * 8]:
        lines += ["", f"{item['day']} {item['start']}–{item.get('end') or '?'} · "
                  f"{item.get('room') or t['tbc']}\n{item['course']} · {item['title']}"]
    if not items:
        lines += ["", t["empty_portal_schedule"]]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(t["previous"], callback_data=f"schedule:page:{page - 1}"))
    if (page + 1) * 8 < len(items):
        nav.append(InlineKeyboardButton(t["next"], callback_data=f"schedule:page:{page + 1}"))
    keyboard = ([nav] if nav else []) + [[InlineKeyboardButton(t["settings"], callback_data="notify:home")]]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def show_schedule(update, context, query="", page=0):
    lang = user_lang(update, context)
    if update.effective_chat.type != "private":
        await reply(update.effective_message, labels(lang)["private"])
        return
    context.user_data["schedule_search"] = query[:100]
    try:
        text, keyboard = await personal_schedule_card(context.user_data, lang, query, page)
    except rooms.ScheduleError:
        await reply(update.effective_message, labels(lang)["unavailable"])
        return
    await render_card(update, text, keyboard)


async def cmd_schedule(update, context):
    await show_schedule(update, context, " ".join(context.args or [])[:100])


async def on_schedule(update, context):
    await update.callback_query.answer()
    if update.effective_chat.type != "private":
        return
    parts = update.callback_query.data.split(":")
    query = context.user_data.get("schedule_search", "")
    page = 0
    if parts[1] == "home":
        query = ""
    elif parts[1] == "page" and parts[-1].isdigit():
        page = int(parts[-1])
    # Old section-selection buttons now open the personal timetable only.
    await show_schedule(update, context, query, page)


def register(app):
    app.add_handler(CommandHandler(["notify", "notifications"], cmd_notifications, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("schedule", cmd_schedule, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_settings, pattern=r"^notify:(home|deadline|class)$"))
    app.add_handler(CallbackQueryHandler(on_schedule, pattern=r"^schedule:(home|toggle:[a-f0-9]{16}|page:\d+)$"))
