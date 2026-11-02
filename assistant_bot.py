"""The unified AI assistant inside the Telegram bot: every text message.

    student: "What deadlines do I have?"
      -> assistant.run(): the model calls get_my_deadlines
      -> TelegramToolbox runs it with the student's own Moodle session and
         shows the same card /deadline shows; the model adds a short answer

The tools reuse the existing feature code - nothing is rebuilt here:

    get_my_grades ........... grade_bot (portal transcript, semester buttons)
    get_my_deadlines ........ moodle + deadline_bot
    get_academic_calendar ... academic_calendar (the portal's System Calendar),
                              else the academic calendar in the knowledge base
    find_free_rooms ......... rooms + rooms_bot (a period or an interval, buttons)
    get_campus_events ....... events_bot (ℹ️ details buttons)
    get_registration_guide .. core.load_guide + the portal button
    get_required_documents .. documents (official checklist) / documents_bot
    sign_in ................. the shared sign-in conversation (accounts_bot)

Personal data and signing in:

    - grades and deadlines need a private chat and a linked account;
    - not linked: "sign in?" [🔐 Sign in] [✖️ Not now]. The question is kept
      for PENDING_TTL; the button opens the bot's existing sign-in steps
      (accounts_bot), so the password goes through that flow only - never
      through the AI - and its message is deleted;
    - once signed in, resume() answers the kept question by itself, so the
      student does not have to ask again.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

import academic_calendar
import assistant
import core
import deadline_bot
import documents
import documents_bot
import events_bot
import grade
import grade_bot
import i18n
import login_guard
import moodle
import rooms
import rooms_bot
from grade import PortalError
from moodle import MoodleError
from telegram_bot import (
    PORTAL_URL,
    keep_typing,
    portal_keyboard,
    render_chunks,
    retry_keyboard,
    strip_tags,
    user_lang,
)

PENDING_TTL = 900  # seconds a question waits for its sign-in
REMEMBERED = 1500  # characters of one answer (with its tool summaries) kept in the history
MAX_LISTED = 120   # rooms / deadlines listed for the model

A: dict[str, dict[str, str]] = i18n.by_key("assistant")  # locales/<lang>.json, section "assistant"


def t(key: str, lang: str) -> str:
    return A[key].get(core.normalize_lang(lang), A[key]["en"])


# ---------------------------------------------------------------- cards ----


@dataclass
class Card:
    """One message a tool shows: the same text and buttons as its command."""

    text: str
    markup: InlineKeyboardMarkup | None = None
    html: bool = False  # already Telegram HTML; otherwise plain text with **bold** and links


async def send_card(bot, chat_id: int, card: Card) -> None:
    chunks = [card.text] if card.html else render_chunks(card.text)
    for index, chunk in enumerate(chunks):
        markup = card.markup if index == len(chunks) - 1 else None
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True, reply_markup=markup)
        except TelegramError as exc:  # a formatting slip must never swallow the answer
            print(f"[assistant] HTML send failed ({exc}); resending as plain text")
            await bot.send_message(chat_id=chat_id, text=strip_tags(chunk),
                                   disable_web_page_preview=True, reply_markup=markup)


# --------------------------------------------------------------- toolbox ----


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()) if value else None
    except ValueError:
        return None


def _mark(course) -> str:
    """The grade as grade_bot shows it; a grade not published yet is never a zero."""
    mark = course.letter or course.score.replace("*", "").strip()
    return mark if mark and mark.upper() != "IP" else "not published"


class TelegramToolbox:
    """Runs the assistant's tools for one student, collecting their cards."""

    def __init__(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.update, self.context = update, context
        self.cards: list[Card] = []
        chat = update.effective_chat
        self.private = chat is not None and chat.type == "private"

    @property
    def user_id(self) -> int:
        return self.update.effective_user.id

    def notes(self) -> str:
        if self.private and self.context.user_data.get("portal"):
            return ("\n\nTHIS STUDENT IS SIGNED IN TO THE PORTAL. For any question about university dates "
                    "(registration, add/drop, withdrawal, exams, FX, resits), call get_academic_calendar "
                    "before you answer: it shows their official System Calendar and their own windows.")
        return ""

    async def run(self, name: str, args: dict[str, Any], lang: str) -> assistant.ToolResult:
        tool = getattr(self, f"tool_{name}", None) if name in {t.name for t in assistant.TOOLS} else None
        if tool is None:
            return assistant.ToolResult({"error": f"there is no tool called {name!r}"})
        cards = len(self.cards)
        try:
            result = await tool(args or {}, core.normalize_lang(lang))
            result.shown = len(self.cards) > cards
            return result
        except Exception as exc:  # noqa: BLE001 - one broken service must not break the answer
            print(f"[assistant] tool {name} failed: {exc!r}")
            return assistant.ToolResult({"error": "this service failed; tell the student to try again later"})

    def _private_only(self, lang: str) -> assistant.ToolResult:
        self.cards.append(Card(t("private_data", lang)))
        return assistant.ToolResult(
            {"error": "personal data is shown only in a private chat with the bot; it was not loaded here"})

    # ------------------------------------------------ the student's data ----

    async def tool_get_my_grades(self, args: dict, lang: str) -> assistant.ToolResult:
        if not self.private:
            return self._private_only(lang)
        if not self.context.user_data.get("portal"):
            return assistant.ToolResult({}, auth="portal")
        try:
            semesters = await grade_bot._load_semesters(self.context, self.user_id, force=True)
        except PortalError as exc:
            if exc.key == "session_expired":  # sign in again, then this question is answered
                self.context.user_data.pop("portal", None)
                grade_bot._CACHE.pop(self.user_id, None)
                return assistant.ToolResult({}, auth="portal")
            text, retry = grade_bot._records_error(exc, lang)
            self.cards.append(Card(text, retry))
            return assistant.ToolResult({"error": "the portal's academic records are unavailable right now"})
        if not semesters:
            self.cards.append(Card(grade_bot.t("grades_empty", lang)))
            return assistant.ToolResult({"semesters": []}, summary="[grades: the portal has no academic records]")

        query = str(args.get("course") or "").strip()
        matches = grade.match_courses(semesters, query) if query else []
        if matches:  # one course: only its grade(s), with a button to all the grades
            text, keyboard = await grade_bot.course_page(matches, lang)
        else:
            text, keyboard = await grade_bot._page(semesters, len(semesters) - 1, lang)
        self.cards.append(Card(text, keyboard, html=True))
        data: dict[str, Any] = {"semesters": [
            {"semester": s.title, "spa": s.spa, "gpa": s.gpa, "courses": [
                {"code": c.code, "name": c.name, "ects": c.ects, "grade": _mark(c)} for c in s.courses]}
            for s in semesters
        ]}
        if any(_mark(c) == "not published" for s in semesters for c in s.courses):
            data["not_published_means"] = (
                "the course is in progress: the transcript holds only final grades, and the current "
                "attestation (midterm) points are not available to the bot - they are on the portal")
        if query:
            data["asked_course"] = query
            data["matching_courses"] = [f"{s.title}: {c.code} {c.name} {_mark(c)}" for s, c in matches] or (
                "none: the card shows the latest semester instead; say the course was not found and "
                "name the closest courses from the list")
        if matches:
            shown = "; ".join(f"{c.code} {c.name} {_mark(c)} ({s.title})" for s, c in matches)
            return assistant.ToolResult(data, summary=f"[shown: the grade for {shown}]")
        latest = semesters[-1]
        courses = ", ".join(f"{c.code} {_mark(c)}" for c in latest.courses)
        summary = (f"[shown: grades, {len(semesters)} semesters; latest {latest.title}: {courses}; "
                   f"SPA {latest.spa or '-'}, GPA {latest.gpa or '-'}]")
        return assistant.ToolResult(data, summary=summary)

    async def tool_get_academic_calendar(self, args: dict, lang: str) -> assistant.ToolResult:
        # A question about dates never asks for a password: without the portal,
        # the model answers from the academic calendar in the knowledge base.
        portal = self.context.user_data.get("portal") if self.private else None
        if portal:
            try:
                calendar = await academic_calendar.fetch_calendar(portal.get("cookies") or {})
            except PortalError as exc:
                print(f"[assistant] the portal's System Calendar is unavailable: {exc.key}")
                if exc.key == "session_expired":
                    self.context.user_data.pop("portal", None)
                    grade_bot._CACHE.pop(self.user_id, None)
            else:
                now = datetime.now(deadline_bot._tz()).replace(tzinfo=None)
                self.cards.append(Card(academic_calendar.render(calendar, lang, now), html=True))
                data = {"today": f"{now:%Y-%m-%d %H:%M}", "source": "my.sdu.edu.kz -> Information -> System Calendar",
                        **academic_calendar.as_data(calendar, now), "meaning": academic_calendar.MEANING}
                windows = "; ".join(f"{e['title']} {e['from']} - {e['until']} ({e['now']})" for e in data["events"])
                return assistant.ToolResult(data, summary=f"[shown: System Calendar, {calendar.term}: {windows}]")
        return assistant.ToolResult({"source": "knowledge base", "note": (
            "The portal's System Calendar is not available here (not signed in, or the portal did not "
            "answer): answer from the academic calendar in the knowledge base with the exact dates. "
            "Do not ask the student to sign in for this.")})

    async def tool_get_my_deadlines(self, args: dict, lang: str) -> assistant.ToolResult:
        if not self.private:
            return self._private_only(lang)
        session = self.context.user_data.get("moodle")
        if not session:
            return assistant.ToolResult({}, auth="moodle")
        try:
            items = await moodle.fetch_deadlines(session)
        except MoodleError as exc:
            if exc.key == "session_expired":
                self.context.user_data.pop("moodle", None)
                return assistant.ToolResult({}, auth="moodle")
            self.cards.append(Card(deadline_bot._error_text(exc, lang)))
            return assistant.ToolResult({"error": "Moodle is unavailable right now"})

        self.cards += [Card(page, html=True) for page in deadline_bot.render_pages(items, lang)]
        zone, now = deadline_bot._tz(), int(time.time())
        when = lambda item: datetime.fromtimestamp(item.due, zone).strftime("%Y-%m-%d %H:%M")  # noqa: E731
        data = {
            "now": datetime.fromtimestamp(now, zone).strftime("%Y-%m-%d %H:%M"),
            "deadlines": [{"course": d.course, "title": d.title, "due": when(d), "overdue": d.due < now}
                          for d in items[:MAX_LISTED]],
        }
        upcoming = [d for d in items if d.due >= now]
        nearest = f"; nearest: {upcoming[0].course} {upcoming[0].title} due {when(upcoming[0])}" if upcoming else ""
        return assistant.ToolResult(data, summary=f"[shown: Moodle deadlines, {len(items)}{nearest}]")

    # ------------------------------------------------- public services ----

    async def tool_find_free_rooms(self, args: dict, lang: str) -> assistant.ToolResult:
        today = datetime.now(rooms.CAMPUS_TZ).date()
        day = _date(args.get("date")) or today
        start = str(args.get("start_time") or "").strip()
        end = str(args.get("end_time") or "").strip()
        search: rooms.Search | None = None
        try:
            if start and end:  # the whole interval, like /rooms 20.10 14:00-16:00
                search = rooms.parse_search(f"{day.isoformat()} {start} {end}")
                rooms.check_search(search)
                body, markup = rooms_bot.search_message(search, lang)
                free = rooms.free_rooms_between(search.day, search.start, search.end)
                when = f"{search.date.isoformat()} {rooms.hhmm(search.start)}-{rooms.hhmm(search.end)}"
            else:  # one class period, like /rooms or /rooms 14:30
                if day < today:
                    raise rooms.SearchError("past")
                weekday = rooms.DAYS[day.weekday()]
                if day == today and not start:
                    view = rooms.current_view()
                else:
                    if weekday not in rooms.load_schedule().days:
                        search = rooms.Search(day, 0, 1)
                        raise rooms.SearchError("no_classes")
                    view = rooms.pick_view(weekday, start or None)
                body, markup = rooms_bot.rooms_message(view, lang)
                free = rooms.free_rooms(view)
                when = f"{view.day} {rooms.hhmm(view.slot.start)}-{rooms.hhmm(view.slot.end)}"
                if view.status in ("day_over", "day_off"):
                    when += " (the next class day: no more classes today)"
        except rooms.SearchError as exc:
            self.cards.append(Card(rooms_bot.search_error_text(exc.key, search, lang)))
            return assistant.ToolResult({"error": f"cannot search rooms: {exc.key}"})
        except rooms.ScheduleError as exc:
            print(f"[rooms] schedule: {exc}")
            self.cards.append(Card(core.msg("schedule_missing", lang)))
            return assistant.ToolResult({"error": "the class schedule is unavailable"})
        except ValueError:  # not a time, or after the last class period
            slots = rooms.load_schedule().slots
            self.cards.append(Card(rooms_bot.t("bad_time", lang).format(
                first=rooms.hhmm(slots[0].start), last=rooms.hhmm(slots[-1].end))))
            return assistant.ToolResult({"error": "no class periods at that time"})

        self.cards.append(Card(body, markup, html=True))
        names = [item.room.name for item in free]
        data = {"when": when, "free_count": len(free), "free_rooms": names[:MAX_LISTED],
                "note": "free according to the class schedule only; bookings and locked rooms are not known"}
        return assistant.ToolResult(data, summary=f"[shown: {len(free)} free rooms for {when}: {', '.join(names[:15])}]")

    async def tool_get_campus_events(self, args: dict, lang: str) -> assistant.ToolResult:
        labels = events_bot.TEXTS[lang]
        try:
            source = events_bot.EVENTS_FILE.read_text(encoding="utf-8") if events_bot.EVENTS_FILE.exists() else None
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[events] {events_bot.EVENTS_FILE.name} could not be read: {exc}")
            self.cards.append(Card(labels["error"], retry_keyboard("events:retry:", lang)))
            return assistant.ToolResult({"error": "the events list could not be read"})
        if source is None:
            self.cards.append(Card(labels["missing"]))
            return assistant.ToolResult({"events": [], "note": "no events are published"})

        start, end = _date(args.get("from_date")), _date(args.get("to_date"))
        start, end = start or end, end or start  # one date: that day only
        if start and end and end < start:
            start, end = end, start
        self.cards.append(Card(events_bot.render_events(source, lang, start=start, end=end),
                               events_bot.details_keyboard(source, start, end)))
        today = datetime.now(rooms.CAMPUS_TZ).date()
        events = [e for e in events_bot.parse_events(source)
                  if not (start and end) or (e.start and start <= e.start <= end)]

        def status(event) -> str:
            if event.status != "scheduled":
                return event.status
            if event.date is None:
                return "date to be confirmed"
            return "passed" if event.date < today else "today" if event.date == today else "upcoming"

        data = {"today": today.isoformat(), "events": [
            {"title": e.title, "date": e.start.isoformat() if e.start else None, "time": e.time,
             "venue": "see the program" if e.venue == "program" else e.venue, "status": status(e)}
            for e in events
        ]}
        return assistant.ToolResult(data, summary=f"[shown: campus events, {len(events)}]")

    async def tool_get_registration_guide(self, args: dict, lang: str) -> assistant.ToolResult:
        try:
            guide = core.load_guide(lang)
        except core.GuideError as exc:
            print(f"[bot] registration guide: {exc}")
            self.cards.append(Card(core.msg("guide_error", lang), retry_keyboard("guide:retry", lang)))
            return assistant.ToolResult({"error": "the guide could not be loaded right now"})
        if not guide or not guide.strip():
            self.cards.append(Card(core.msg("guide_missing", lang)))
            return assistant.ToolResult({"error": "no guide is published for this semester"})
        self.cards.append(Card(guide.strip(), portal_keyboard(lang)))
        return assistant.ToolResult({"shown": "the full registration guide", "portal": PORTAL_URL},
                                    summary="[shown: the course registration guide]")

    async def tool_get_required_documents(self, args: dict, lang: str) -> assistant.ToolResult:
        try:
            catalog = documents.load_catalog()
        except documents.CatalogError as exc:
            print(f"[documents] {exc}")
            self.cards.append(Card(documents_bot.t("catalog_missing", lang)))
            return assistant.ToolResult({"error": "the list of documents is unavailable"})

        student_id = documents_bot._student_id(self.context) if self.private else None
        submission = documents.store().get(student_id) if student_id else None
        if submission is not None and submission.track in catalog.tracks:
            # The student's own checklist, with its upload buttons.
            text, markup = documents_bot._board(student_id, lang)
            self.cards.append(Card(text, markup))
            missing = documents.missing_required(catalog, submission.track, submission.documents)
            data = {"track": submission.track, "submission_status": submission.status, "documents": [
                {"name": doc.label(lang), "required": doc.required,
                 "status": submission.documents[doc.id].status if doc.id in submission.documents else "missing"}
                for doc in catalog.for_track(submission.track)
            ]}
            names = ", ".join(doc.label("en") for doc in missing) or "none"
            return assistant.ToolResult(data, summary=f"[shown: the student's document checklist; missing: {names}]")

        track = args.get("track")
        tracks = [track] if track in catalog.tracks else list(catalog.tracks)
        markup = None
        if self.private:  # the button opens the submission (it signs the student in first if needed)
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(t("btn_submit_docs", lang), callback_data="menu:documents")]])
        self.cards.append(Card(_documents_text(catalog, tracks, lang), markup))
        data = {"source": catalog.source, "checked": catalog.checked, "max_file_mb": catalog.max_bytes // (1024 * 1024),
                "tracks": {tr: {
                    "name": catalog.track_name(tr, "en"),
                    "required": [doc.label(lang) for doc in catalog.required(tr)],
                    "optional": [f"{doc.label(lang)} ({doc.condition(lang)})"
                                 for doc in catalog.for_track(tr) if not doc.required],
                } for tr in tracks}}
        return assistant.ToolResult(data, summary="[shown: the official list of enrollment documents]")

    async def tool_sign_in(self, args: dict, lang: str) -> assistant.ToolResult:
        account = "moodle" if args.get("account") == "moodle" else "portal"
        if self.context.user_data.get(account):
            return assistant.ToolResult({"already_signed_in": True, "account": account})
        if not self.private:
            return self._private_only(lang)
        return assistant.ToolResult({}, auth=account)


def _documents_text(catalog: documents.Catalog, tracks: list[str], lang: str) -> str:
    """The official checklist for one or both tracks, as /documents words it."""
    lines: list[str] = []
    for track in tracks:
        name = f"{documents_bot.TRACK_ICONS.get(track, '')} {catalog.track_name(track, lang)}".strip()
        lines += [t("docs_title", lang).format(track=name), "", f"**{documents_bot.t('required', lang)}**"]
        lines += [f"• {doc.label(lang)}" for doc in catalog.required(track)]
        optional = [doc for doc in catalog.for_track(track) if not doc.required]
        if optional:
            lines += ["", f"**{documents_bot.t('optional', lang)}**"]
            lines += [f"• {doc.label(lang)} — {doc.condition(lang)}" for doc in optional]
        lines.append("")
    formats = list(dict.fromkeys(fmt for track in tracks for doc in catalog.for_track(track) for fmt in doc.formats))
    try:
        checked = datetime.strptime(catalog.checked, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        checked = catalog.checked
    lines += [
        documents_bot.t("rules", lang).format(formats=", ".join(f.upper() for f in formats),
                                              mb=documents_bot._mb(catalog.max_bytes)),
        documents_bot.t("source", lang).format(url=catalog.source, date=checked),
        "",
        documents_bot.t("prototype", lang),
    ]
    return "\n".join(lines)


# ----------------------------------------------------------- the handler ----


def _remembered(answer: assistant.Answer) -> str:
    """The answer as kept in the history: the text plus what the cards showed,
    so a follow-up such as "and the nearest one?" can be answered."""
    text = "\n".join(part for part in [answer.text, *answer.summaries] if part).strip()
    return text[:REMEMBERED]


async def answer_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str | None = None) -> None:
    """Answer a text message - or a kept question after the sign-in."""
    lang = user_lang(update, context)
    chat_id = update.effective_chat.id
    text = (question if question is not None else (update.effective_message.text or "")).strip()
    if not text:
        return
    if question is None and login_guard.maybe_secret(context.user_data, text):
        # Right after a sign-in ended, a lone word with digits or symbols is most
        # likely the password typed again: it must not reach the model or the history.
        try:
            await update.effective_message.delete()
        except TelegramError as exc:
            print(f"[assistant] could not delete a possible password: {exc}")
        await context.bot.send_message(chat_id=chat_id, text=t("secret_guard", lang))
        return

    history: list[dict[str, str]] = context.user_data.setdefault("history", [])
    toolbox = TelegramToolbox(update, context)
    typing = asyncio.create_task(keep_typing(context.bot, chat_id))
    try:
        answer = await assistant.run(text, history, lang, toolbox)
    except core.AssistantError as exc:
        await context.bot.send_message(chat_id=chat_id, text=exc.localized(lang))
        return
    except Exception as exc:  # noqa: BLE001 - the student still gets an answer
        print(f"[assistant] unexpected error: {exc!r}")
        await context.bot.send_message(chat_id=chat_id, text=core.msg("unexpected", lang))
        return
    finally:
        typing.cancel()

    if answer.text:
        await send_card(context.bot, chat_id, Card(answer.text))
    for card in toolbox.cards:
        await send_card(context.bot, chat_id, card)
    if answer.auth:
        await _ask_to_sign_in(context, chat_id, answer, text)
        return
    history.extend([{"role": "user", "text": text}, {"role": "assistant", "text": _remembered(answer)}])
    del history[: -core.HISTORY_TURNS]  # keep only the most recent turns


async def _ask_to_sign_in(context, chat_id: int, answer: assistant.Answer, question: str) -> None:
    """[🔐 Sign in] [✖️ Not now] - the button starts the existing sign-in
    conversation (accounts_bot); the question is kept and answered after it."""
    lang = answer.lang
    explicit = bool(answer.tools) and answer.tools[-1] == "sign_in"
    if not explicit:
        context.user_data["pending_ask"] = {"text": question, "account": answer.auth, "at": time.time()}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(t("btn_signin", lang), callback_data=f"auth:{answer.auth}"),
        InlineKeyboardButton(t("btn_later", lang), callback_data="auth:later"),
    ]])
    key = f"signin_{answer.auth}" if explicit else f"need_{answer.auth}"
    await context.bot.send_message(chat_id=chat_id, text=t(key, lang), reply_markup=keyboard)


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """After a successful sign-in: answer the question that was waiting for it."""
    pending = (context.user_data or {}).get("pending_ask")
    if not pending:
        return False
    if time.time() - pending.get("at", 0) > PENDING_TTL:
        context.user_data.pop("pending_ask", None)
        return False
    if not context.user_data.get(pending.get("account")):  # the sign-in did not work (yet)
        return False
    context.user_data.pop("pending_ask", None)
    question = pending["text"]
    lang = core.detect_lang(question, user_lang(update, context))
    shown = question if len(question) <= 200 else question[:199] + "…"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=t("continuing", lang).format(question=shown))
    await answer_question(update, context, question=question)
    return True


async def on_later(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[✖️ Not now] under the sign-in offer."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_ask", None)
    lang = user_lang(update, context)
    try:
        await query.edit_message_text(t("later", lang))
    except TelegramError:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=t("later", lang))


def register_assistant(app: Application) -> None:
    """The sign-in buttons auth:portal / auth:moodle are entry points of the
    shared sign-in conversation (accounts_bot); this adds [✖️ Not now]."""
    app.add_handler(CallbackQueryHandler(on_later, pattern=r"^auth:later$"))
