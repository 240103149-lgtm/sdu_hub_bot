"""/events - US4: the published campus event schedule.

    /events                       every published event, by date and time
    /events 01.11-30.11           only events that start in that range
    ℹ️ buttons                    the full published details of one event

Read from knowledge/campus_events.md - no AI request is needed. Each event is
a "## N. Title" section with "- Key: value" lines (Date / Event date, Time,
Venue, Format, Program). Optional keys the organizers can add:

    - Status: Cancelled | Postponed | Draft     (Draft is never shown)
    - New date: November 20, 2026               (for a postponed event)

Status (upcoming / today / passed) is computed from the event date in campus
time. Published facts are shown verbatim, in the source language; a missing
time or venue is labelled "To be confirmed" instead of being guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import core
import i18n
import rooms
from rooms import CAMPUS_TZ
from telegram_bot import add_command, reply, retry_keyboard, user_lang

EVENTS_FILE = core.KNOWLEDGE_DIR / "campus_events.md"
TEXTS = i18n.by_lang("events")  # locales/<lang>.json, section "events"
BUTTONS_PER_ROW = 5

WEEKDAY_PREFIX = re.compile(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+")
CLOCK = re.compile(r"\b(\d{1,2}:\d{2})\b")


class EventsError(Exception):
    """The events file exists but could not be read: a temporary failure."""


@dataclass(frozen=True)
class Event:
    number: int            # its position in the file; the details button uses it
    title: str
    body: str              # the published details, verbatim
    date: date | None
    time: str | None       # "18:30"
    venue: str | None
    status: str            # scheduled | cancelled | postponed
    new_date: date | None  # a postponed event's new date, when published

    @property
    def start(self) -> date | None:
        """The date the event actually starts (a postponed one: its new date)."""
        return self.new_date if self.status == "postponed" else self.date


# ---------------------------------------------------------------- parsing ----


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in re.finditer(r"^- ([A-Za-z ]+?):\s*(.*)$", body, re.MULTILINE):
        fields.setdefault(match.group(1).strip().lower(), match.group(2).strip())
    return fields


def _published_date(value: str | None) -> date | None:
    """'October 9, 2026 (Friday)' or 'Friday, October 16, 2026' -> a date."""
    if not value:
        return None
    raw = WEEKDAY_PREFIX.sub("", re.sub(r"\s*\([^)]*\)", "", value)).strip()
    try:
        return datetime.strptime(raw, "%B %d, %Y").date()
    except ValueError:
        return None  # the published text is still shown in the details


def parse_events(source: str) -> list[Event]:
    """Published events in date and time order; drafts are left out."""
    events: list[Event] = []
    sections = re.split(r"^##\s+", source, flags=re.MULTILINE)[1:]
    for number, section in enumerate(sections, start=1):
        heading, _, body = section.strip().partition("\n")
        fields = _fields(body)
        status_text = fields.get("status", "").lower()
        if "draft" in status_text or "unpublished" in status_text:
            continue
        status = (
            "cancelled" if "cancel" in status_text
            else "postponed" if "postpone" in status_text
            else "scheduled"
        )
        time_match = CLOCK.search(fields.get("time", ""))
        program_times = re.findall(r"^\s+- (\d{1,2}:\d{2})\b", body, re.MULTILINE)
        time = time_match.group(1) if time_match else (min(program_times, key=rooms.to_minutes) if program_times else None)
        venue = fields.get("venue")
        if not venue and fields.get("format", "").lower().startswith("online"):
            venue = fields["format"]
        if not venue and program_times:
            venue = "program"  # the program lists a place for each part
        events.append(Event(
            number=number,
            title=re.sub(r"^\d+\.\s*", "", heading).strip(),
            body=body.strip(),
            date=_published_date(fields.get("date") or fields.get("event date")),
            time=time,
            venue=venue,
            status=status,
            new_date=_published_date(fields.get("new date")),
        ))
    events.sort(key=lambda e: (e.start or date.max, rooms.to_minutes(e.time) if e.time else 24 * 60, e.number))
    return events


def parse_range(text: str, today: date | None = None) -> tuple[date, date] | None:
    """'01.11-30.11', '2026-11-01 2026-11-30' or one date. None: no range given.
    Raises ValueError for an invalid range (QA: end before start)."""
    today = today or datetime.now(CAMPUS_TZ).date()
    tokens: list[str] = []
    for token in text.split():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            tokens.append(token)
        else:
            tokens += [part for part in re.split(r"[-–—]", token) if part]
    if not tokens:
        return None
    if len(tokens) > 2:
        raise ValueError("too many dates")
    try:
        start = rooms.parse_date(tokens[0], today)
        end = rooms.parse_date(tokens[-1], today)
    except rooms.SearchError as exc:
        raise ValueError(str(exc)) from None
    if end < start:
        raise ValueError("the end date is before the start date")
    return start, end


def load_events() -> list[Event] | None:
    """None: not published. Raises EventsError when the file can't be read."""
    if not EVENTS_FILE.exists():
        return None
    try:
        source = EVENTS_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EventsError(f"{EVENTS_FILE.name} could not be read: {exc}") from exc
    return parse_events(source)


# -------------------------------------------------------------- rendering ----


def _status(event: Event, lang: str, today: date) -> str:
    labels = TEXTS[core.normalize_lang(lang)]
    if event.status == "cancelled":
        return labels["cancelled"]
    if event.status == "postponed":
        if event.new_date:
            return labels["postponed"].format(date=f"{event.new_date:%d.%m.%Y}")
        return labels["postponed_tbc"]
    if event.date is None:
        return ""
    return labels["past" if event.date < today else "today" if event.date == today else "upcoming"]


def _summary(event: Event, lang: str) -> str:
    """🗓 09.10.2026 · 🕒 18:30 · 📍 SDULife Scene"""
    labels = TEXTS[core.normalize_lang(lang)]
    when = f"{event.date:%d.%m.%Y}" if event.date else labels["tbc"]
    venue = labels["see_program"] if event.venue == "program" else (event.venue or labels["tbc"])
    return f"🗓 {when} · 🕒 {event.time or labels['tbc']} · 📍 {venue}"


def render_events(source: str, lang: str, today: date | None = None,
                  start: date | None = None, end: date | None = None) -> str:
    """The schedule: one short entry per event, in date and time order."""
    lang = core.normalize_lang(lang)
    labels = TEXTS[lang]
    today = today or datetime.now(CAMPUS_TZ).date()
    events = parse_events(source)
    if not events:
        return labels["missing"]
    if start and end:
        events = [e for e in events if e.start and start <= e.start <= end]
    lines = [f"# {labels['title']}"]
    if start and end:
        lines.append(labels["range"].format(start=f"{start:%d.%m.%Y}", end=f"{end:%d.%m.%Y}"))
    if not events:
        return "\n".join(lines + ["", labels["none"], "", labels["range_hint"].format(example=_example(today))])
    for position, event in enumerate(events, start=1):  # numbered as shown; the button knows the event
        status = _status(event, lang, today)
        lines += ["", f"{position}. **{event.title}**" + (f" · {status}" if status else ""), _summary(event, lang)]
    lines += ["", labels["details_hint"], labels["source"], labels["range_hint"].format(example=_example(today))]
    return "\n".join(lines)


def render_details(event: Event, lang: str, today: date | None = None) -> str:
    lang = core.normalize_lang(lang)
    today = today or datetime.now(CAMPUS_TZ).date()
    status = _status(event, lang, today)
    lines = [f"## {event.title}" + (f" · {status}" if status else ""), _summary(event, lang), "", event.body]
    return "\n".join(lines + ["", TEXTS[lang]["no_registration"]])


def _example(today: date) -> str:
    first = today.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    last = following - timedelta(days=1)
    return f"/events {first:%d.%m}-{last:%d.%m}"


def details_keyboard(source: str, start: date | None, end: date | None) -> InlineKeyboardMarkup | None:
    events = parse_events(source)
    if start and end:
        events = [e for e in events if e.start and start <= e.start <= end]
    buttons = [
        InlineKeyboardButton(f"ℹ️ {position}", callback_data=f"ev:{e.number}")
        for position, e in enumerate(events, start=1)
    ]
    rows = [buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(rows) if rows else None


# --------------------------------------------------------------- handlers ----


def _range_code(start: date | None, end: date | None) -> str:
    return f"{start:%Y%m%d}-{end:%Y%m%d}" if start and end else ""


async def _show(message, lang: str, start: date | None, end: date | None) -> None:
    labels = TEXTS[lang]
    try:
        exists = EVENTS_FILE.exists()
        source = EVENTS_FILE.read_text(encoding="utf-8") if exists else None
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[events] {EVENTS_FILE.name} could not be read: {exc}")
        # A loading failure is not "no events"; Retry keeps the chosen dates.
        retry = f"events:retry:{_range_code(start, end)}"
        await message.reply_text(labels["error"], reply_markup=retry_keyboard(retry, lang))
        return
    if source is None:
        await reply(message, labels["missing"])
        return
    await reply(message, render_events(source, lang, start=start, end=end),
                reply_markup=details_keyboard(source, start, end))


async def cmd_events(update, context):
    lang = user_lang(update, context)
    try:
        selected = parse_range(" ".join(context.args or []))
    except ValueError:  # no search is submitted until the range is corrected
        today = datetime.now(CAMPUS_TZ).date()
        await update.effective_message.reply_text(TEXTS[lang]["bad_range"].format(example=_example(today)))
        return
    start, end = selected if selected else (None, None)
    await _show(update.effective_message, lang, start, end)


async def on_events_retry(update, context):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 2)[2] if query.data.count(":") >= 2 else ""
    start = end = None
    if code:
        try:
            first, last = code.split("-")
            start, end = rooms.parse_date(first, date.today()), rooms.parse_date(last, date.today())
        except (ValueError, rooms.SearchError):
            start = end = None
    await _show(update.effective_message, user_lang(update, context), start, end)


async def on_event_details(update, context):
    query = update.callback_query
    await query.answer()
    lang = user_lang(update, context)
    try:
        events = load_events() or []
    except EventsError as exc:
        print(f"[events] {exc}")
        await update.effective_message.reply_text(TEXTS[lang]["error"], reply_markup=retry_keyboard("events:retry", lang))
        return
    number = int(query.data.split(":", 1)[1])
    event = next((e for e in events if e.number == number), None)
    if event is None:  # the list changed since the button was sent
        await update.effective_message.reply_text(TEXTS[lang]["gone"])
        return
    await reply(update.effective_message, render_details(event, lang))


def register_events(app):
    add_command("events",
        {lang: texts["menu"] for lang, texts in TEXTS.items()},
        {lang: texts["help_line"] for lang, texts in TEXTS.items()})
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CallbackQueryHandler(on_event_details, pattern=r"^ev:\d+$"))
    app.add_handler(CallbackQueryHandler(on_events_retry, pattern=r"^events:retry"))
