"""Private notification settings and personal course-section selection."""
from __future__ import annotations

import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

import i18n
import rooms
import grade
import notifications
import student_schedule
from telegram_bot import reply, user_lang


def labels(lang):
    return i18n.section(lang, "notifications")


def settings(data, lang):
    t = labels(lang)
    state = lambda key: t["on"] if notifications.enabled(data, key) else t["off"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{t['deadlines']} · {state('deadline_notifications')}", callback_data="notify:deadline")],
        [InlineKeyboardButton(f"{t['classes']} · {state('class_notifications')}", callback_data="notify:class")],
        [InlineKeyboardButton(t["schedule"], callback_data="schedule:home")],
    ])
    text = t["settings"] + "\n\n" + t["timing"]
    if os.getenv("VERCEL") or os.getenv("REMINDERS_ENABLED", "on").lower() == "off":
        text += "\n\n" + t["service_paused"]
    if not data.get("moodle"):
        text += "\n\n" + t["link_moodle"]
    text += "\n\n" + (t["portal_schedule"] if data.get("portal") else t["choose_sections"])
    return text, keyboard


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
    text, keyboard = settings(context.user_data, lang)
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


def schedule_card(data, lang, query="", page=0):
    t = labels(lang)
    selected = data.get("schedule_groups", [])
    groups = student_schedule.groups(query) if query else [g for g in student_schedule.groups() if g["id"] in selected]
    lines = [t["schedule"], t["choose_sections"], "", t["search_hint"]]
    info = student_schedule.term()
    lines += ["", t["validity"].format(updated=info["updated"], end=info["valid_until"])]
    if not groups:
        lines += ["", t["no_sections"]]
    keyboard = []
    page = max(0, min(page, max(0, (len(groups) - 1) // 8)))
    for group in groups[page * 8:(page + 1) * 8]:
        chosen = group["id"] in selected
        title = f"{group['course']} · {group['type']} · {group['section']}"
        lines += ["", ("✓ " if chosen else "") + title]
        lines += [f"{s['day']} {s['start']}–{s['end']} · {s['room']}" for s in group["sessions"]]
        keyboard.append([InlineKeyboardButton((t["remove"] if chosen else t["add"]) + " " + title,
                                              callback_data=f"schedule:toggle:{group['id']}")])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(t["previous"], callback_data=f"schedule:page:{page - 1}"))
    if (page + 1) * 8 < len(groups):
        nav.append(InlineKeyboardButton(t["next"], callback_data=f"schedule:page:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(t["settings"], callback_data="notify:home")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def personal_schedule_card(data, lang, query="", page=0):
    if not data.get("portal"):
        return schedule_card(data, lang, query, page)
    t = labels(lang)
    try:
        items = await notifications.personal_classes(data)
    except grade.PortalError:
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
    elif parts[1] == "toggle":
        key = parts[-1]
        try:
            if key not in {g["id"] for g in student_schedule.groups()}:
                return
        except rooms.ScheduleError:
            await show_schedule(update, context)
            return
        selected = list(context.user_data.get("schedule_groups", []))
        if key in selected:
            selected.remove(key)
        elif len(selected) < 40:
            selected.append(key)
            context.user_data["class_notifications"] = True
            context.user_data.pop("reminders_blocked", None)
        context.user_data["schedule_groups"] = selected
    await show_schedule(update, context, query, page)


def register(app):
    app.add_handler(CommandHandler(["notify", "notifications"], cmd_notifications, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("schedule", cmd_schedule, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_settings, pattern=r"^notify:(home|deadline|class)$"))
    app.add_handler(CallbackQueryHandler(on_schedule, pattern=r"^schedule:(home|toggle:[a-f0-9]{16}|page:\d+)$"))
