"""/rooms - free classrooms right now, or for a chosen date and interval (US3).

    /rooms                      the class period that is on now (or the next one)
    /rooms 14:30                that time today
    /rooms 20.10 14:00-16:00    rooms free for that WHOLE interval on that date
    buttons                     ◀ previous period · 🔄 Now · next period ▶, and
                                one per day; they redraw the same message

Results are grouped by building, labelled "Available according to schedule"
and show the date of the schedule data. A loading failure offers Retry, which
keeps the chosen interval. The rooms come from rooms.py (data/schedule.json).
"""
from __future__ import annotations

import html
import itertools
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import core
import i18n
import rooms
from telegram_bot import add_command, render_chunks, retry_keyboard, user_lang

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


def _example() -> str:
    """'/rooms 20.10 14:00-16:00' for the next class day, as a format example."""
    day = datetime.now(rooms.CAMPUS_TZ).date() + timedelta(days=1)
    if day.weekday() == 6:  # no classes on Sunday
        day += timedelta(days=1)
    return f"/rooms {day:%d.%m} 14:00-16:00"


def _room_lines(free: list[rooms.FreeRoom]) -> list[str]:
    """🏢 building, then one line per letter block: **F** — F101, F102 (14:30)."""
    lines: list[str] = []
    for building, in_building in itertools.groupby(rooms.by_building(free), key=lambda item: item.room.building):
        lines.append(f"🏢 {building or '—'}")
        for block, group in itertools.groupby(in_building, key=lambda item: item.room.block):
            names = ", ".join(
                item.room.name + (f" ({rooms.hhmm(item.until)})" if item.until is not None else "")
                for item in group
            )
            lines.append(f"**{block}** — {names}" if block else names)
    return lines


def _results(free: list[rooms.FreeRoom], lang: str) -> list[str]:
    """The list with its label, legend, data date and the custom-time hint."""
    if not free:
        lines = [t("none", lang)]
    else:
        lines = [t("label", lang), *_room_lines(free), "", t("legend", lang)]
    updated = rooms.schedule_updated()
    if updated:
        lines.append(t("data_date", lang).format(date=f"{updated:%d.%m.%Y}"))
    lines.append(t("custom_hint", lang).format(example=_example()))
    return lines


def rooms_text(view: rooms.View, free: list[rooms.FreeRoom], lang: str) -> str:
    """The /rooms message for one class period, as plain text."""
    lang = core.normalize_lang(lang)
    when = f"🕒 {DAY_NAMES[lang][view.day][1]}, {rooms.hhmm(view.slot.start)}–{rooms.hhmm(view.slot.end)}"
    if view.status in ("now", "next"):
        when += f" · {t(view.status, lang)}"
    lines = [f"🚪 **{t('title', lang).format(count=len(free))}**", when]
    if view.status in ("day_over", "day_off"):
        lines.append(t(view.status, lang))
    return "\n".join(lines + [""] + _results(free, lang))


def search_text(search: rooms.Search, free: list[rooms.FreeRoom], lang: str) -> str:
    """The /rooms message for a student's own date and interval, as plain text."""
    lang = core.normalize_lang(lang)
    when = (
        f"🗓 {DAY_NAMES[lang][search.day][1]}, {search.date:%d.%m.%Y}, "
        f"{rooms.hhmm(search.start)}–{rooms.hhmm(search.end)}"
    )
    lines = [f"🚪 **{t('title', lang).format(count=len(free))}**", when, ""]
    return "\n".join(lines + _results(free, lang))


def search_error_text(key: str, search: rooms.Search | None, lang: str) -> str:
    """invalid · past · hours · no_classes, each with what to do instead."""
    if key == "hours":
        slots = rooms.load_schedule().slots
        return t("hours", lang).format(first=rooms.hhmm(slots[0].start), last=rooms.hhmm(slots[-1].end))
    if key == "no_classes" and search is not None:
        return t("no_classes", lang).format(date=f"{search.date:%d.%m.%Y}")
    return t(key if key in ("invalid", "past") else "invalid", lang).format(example=_example())


def search_message(search: rooms.Search, lang: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Telegram HTML and buttons for an interval search (or its error)."""
    try:
        rooms.check_search(search)
        free = rooms.free_rooms_between(search.day, search.start, search.end)
        text = search_text(search, free, lang)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(t("btn_now", lang), callback_data="rooms:now")]])
    except rooms.ScheduleError as exc:
        print(f"[rooms] schedule: {exc}")
        # Retry keeps the student's date and interval.
        return html.escape(core.msg("schedule_missing", lang)), retry_keyboard(f"roomsq:{search.code()}", lang)
    except rooms.SearchError as exc:
        text, markup = search_error_text(exc.key, search, lang), None
    chunks = render_chunks(text)
    return (chunks[0] if len(chunks) == 1 else chunks[0] + "\n…"), markup


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
    """Free classrooms now, at a time (/rooms 14:30) or for an interval
    (/rooms 20.10 14:00-16:00)."""
    lang = user_lang(update, context)
    args = " ".join(context.args or [])
    try:
        search = rooms.parse_search(args)
    except rooms.SearchError as exc:  # an invalid search is not submitted
        await update.effective_message.reply_text(search_error_text(exc.key, None, lang))
        return
    if search is not None:
        body, markup = search_message(search, lang)
        await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
        return
    try:
        view = rooms.pick_view(time=args or None)
        body, markup = rooms_message(view, lang)
    except rooms.ScheduleError as exc:
        print(f"[rooms] schedule: {exc}")
        today = rooms.DAYS[datetime.now(rooms.CAMPUS_TZ).weekday()]
        retry = f"rooms:{today}:{args}" if args else "rooms:now"
        await update.effective_message.reply_text(
            core.msg("schedule_missing", lang), reply_markup=retry_keyboard(retry, lang)
        )
        return
    except ValueError:  # not a time, or after the last class period
        slots = rooms.load_schedule().slots
        hint = t("bad_time", lang).format(first=rooms.hhmm(slots[0].start), last=rooms.hhmm(slots[-1].end))
        await update.effective_message.reply_text(hint)
        return
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _redraw(query, body: str, markup) -> None:
    try:
        await query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramError as exc:
        if "not modified" in str(exc).lower():  # "Now" pressed and nothing has changed
            return
        await query.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_rooms_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A day, time, "Now" or Retry button under a /rooms message: redraw it in place."""
    query = update.callback_query
    await query.answer()
    lang = user_lang(update, context)
    try:
        body, markup = rooms_message(view_from_button(query.data), lang)
    except rooms.ScheduleError as exc:
        print(f"[rooms] schedule: {exc}")
        body, markup = html.escape(core.msg("schedule_missing", lang)), retry_keyboard(query.data, lang)
    await _redraw(query, body, markup)


async def on_search_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry after a loading failure, with the same date and interval."""
    query = update.callback_query
    await query.answer()
    lang = user_lang(update, context)
    try:
        search = rooms.search_from_code(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        search = None
    if search is None:
        body, markup = html.escape(search_error_text("invalid", None, lang)), None
    else:
        body, markup = search_message(search, lang)
    await _redraw(query, body, markup)


# ----------------------------------------------------------- registration ----


def register_rooms(app: Application) -> None:
    """Add /rooms and its buttons to the bot."""
    add_command("rooms", MENU, HELP_LINE)
    app.add_handler(CommandHandler(["rooms", "room"], cmd_rooms))
    app.add_handler(CallbackQueryHandler(on_rooms_chosen, pattern=r"^rooms:"))
    app.add_handler(CallbackQueryHandler(on_search_retry, pattern=r"^roomsq:"))
