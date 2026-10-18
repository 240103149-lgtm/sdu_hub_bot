"""/rooms - free classrooms right now, or at a chosen time.

    /rooms          the class period that is on now (or the next one)
    /rooms 14:30    that time today
    buttons         ◀ previous period · 🔄 Now · next period ▶, and one per day;
                    they redraw the same message instead of sending new ones

The rooms themselves come from rooms.py (data/schedule.json), which the
website's "Free rooms" tab uses too. Wired into the bot by two lines in
telegram_bot.build_application (see register_rooms below).
"""
from __future__ import annotations

import html
import itertools

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import core
import i18n
import rooms
from telegram_bot import add_command, render_chunks, user_lang

# ------------------------------------------------------------------ texts ----

R: dict[str, dict[str, str]] = i18n.by_key("rooms")  # locales/<lang>.json, section "rooms"

MENU = R["menu"]
HELP_LINE = R["help_line"]

# Schedule day code -> (short name for the day buttons, full name for the heading).
DAY_NAMES: dict[str, dict[str, tuple[str, str]]] = {
    lang: {day: tuple(names) for day, names in days.items()} for lang, days in R["day_names"].items()
}


def t(key: str, lang: str) -> str:
    return R[key].get(core.normalize_lang(lang), R[key]["en"])


# -------------------------------------------------------------- message ----


def rooms_text(view: rooms.View, free: list[rooms.FreeRoom], lang: str) -> str:
    """The /rooms message as plain text: one line of rooms per block."""
    lang = core.normalize_lang(lang)
    when = f"🕒 {DAY_NAMES[lang][view.day][1]}, {rooms.hhmm(view.slot.start)}–{rooms.hhmm(view.slot.end)}"
    if view.status in ("now", "next"):
        when += f" · {t(view.status, lang)}"
    lines = [f"🚪 **{t('title', lang).format(count=len(free))}**", when]
    if view.status in ("day_over", "day_off"):
        lines.append(t(view.status, lang))
    lines.append("")

    if not free:
        lines.append(t("none", lang))
        return "\n".join(lines)
    for block, group in itertools.groupby(free, key=lambda item: item.room.block):
        names = ", ".join(
            item.room.name + (f" ({rooms.hhmm(item.until)})" if item.until is not None else "")
            for item in group
        )
        lines.append(f"**{block}** — {names}" if block else names)
    lines += ["", t("legend", lang)]
    return "\n".join(lines)


def rooms_button(label: str, day: str, slot: rooms.Slot) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=f"rooms:{day}:{rooms.hhmm(slot.start)}")


def rooms_keyboard(view: rooms.View, lang: str) -> InlineKeyboardMarkup:
    """◀ previous period · Now · next period ▶, and a row of day buttons."""
    lang = core.normalize_lang(lang)
    schedule = rooms.load_schedule()
    index = schedule.slots.index(view.slot)
    nav = [InlineKeyboardButton(t("btn_now", lang), callback_data="rooms:now")]
    if index > 0:
        earlier = schedule.slots[index - 1]
        nav.insert(0, rooms_button(f"◀ {rooms.hhmm(earlier.start)}", view.day, earlier))
    if index + 1 < len(schedule.slots):
        later = schedule.slots[index + 1]
        nav.append(rooms_button(f"{rooms.hhmm(later.start)} ▶", view.day, later))
    days = [
        rooms_button(("• " if day == view.day else "") + DAY_NAMES[lang][day][0], day, view.slot)
        for day in schedule.days
    ]
    return InlineKeyboardMarkup([nav, days])


def rooms_message(view: rooms.View, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """The /rooms message as Telegram HTML, with its buttons.

    It has to fit in one message so that the buttons can redraw it in place;
    the real schedule needs about a quarter of the limit.
    """
    chunks = render_chunks(rooms_text(view, rooms.free_rooms(view), lang))
    body = chunks[0] if len(chunks) == 1 else chunks[0] + "\n…"
    return body, rooms_keyboard(view, lang)


def view_from_button(data: str) -> rooms.View:
    """The period a /rooms button points at: "rooms:now" or "rooms:Mo:10:30"."""
    parts = data.split(":", 2)
    if len(parts) == 3:
        try:
            return rooms.pick_view(parts[1], parts[2])
        except ValueError:  # a button left over from an older schedule
            pass
    return rooms.current_view()


# --------------------------------------------------------------- handlers ----


async def cmd_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free classrooms right now, or at the time given: /rooms 14:30."""
    lang = user_lang(update, context)
    try:
        view = rooms.pick_view(time=" ".join(context.args or []) or None)
        body, markup = rooms_message(view, lang)
    except rooms.ScheduleError as exc:
        print(f"[rooms] schedule: {exc}")
        await update.effective_message.reply_text(core.msg("schedule_missing", lang))
        return
    except ValueError:  # not a time, or after the last class period
        slots = rooms.load_schedule().slots
        hint = t("bad_time", lang).format(first=rooms.hhmm(slots[0].start), last=rooms.hhmm(slots[-1].end))
        await update.effective_message.reply_text(hint)
        return
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_rooms_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A day, time or "Now" button under a /rooms message: redraw it in place."""
    query = update.callback_query
    await query.answer()
    lang = user_lang(update, context)
    try:
        body, markup = rooms_message(view_from_button(query.data), lang)
    except rooms.ScheduleError as exc:
        print(f"[rooms] schedule: {exc}")
        body, markup = html.escape(core.msg("schedule_missing", lang)), None
    try:
        await query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as exc:
        if "not modified" in str(exc).lower():  # "Now" pressed and nothing has changed
            return
        await query.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)


# ----------------------------------------------------------- registration ----


def register_rooms(app: Application) -> None:
    """Add /rooms and its buttons to the bot."""
    add_command("rooms", MENU, HELP_LINE)
    app.add_handler(CommandHandler(["rooms", "room"], cmd_rooms))
    app.add_handler(CallbackQueryHandler(on_rooms_chosen, pattern=r"^rooms:"))
