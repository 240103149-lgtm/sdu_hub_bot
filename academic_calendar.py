"""US13 Academic calendar: the portal's official System Calendar.

my.sdu.edu.kz -> Information -> System Calendar (index.php?mod=calendar) is
where the university opens and closes things for each term: course
registration, add/drop, late registration, early and late withdrawal, the
pre-final and final grades, the FX application and resit exams - each with a
start and an end (date and time) and OPENED / CLOSED. Below that table come
the student's own "Individual Permissions" (e.g. a personal registration
window).

The page itself only holds the list of terms, <select id="ysem"> (20261 =
2026 - 2027 Fall, 20252 = 2025 - 2026 Spring, 20253 = Summer; the current one
first). Its script then loads both tables with

    POST index.php   ajx=1&mod=calendar&action=GetCalendar&yt=20261
    POST index.php   ajx=1&mod=calendar&action=GetPermissions&yt=20261

which answer {"CODE": "1", "DATA": "<table>...</table>"}. Signed out, the
answer is a marker that starts with "#!" instead.

The AI assistant shows this as a card when the student is signed in to the
portal. Otherwise it answers from knowledge/sdu_academic_calendar_*.md (the
calendar published on sdu.edu.kz), so a question about dates never needs a
password.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

import httpx

import core
import i18n
from grade import HEADERS, PORTAL_BASE, TIMEOUT, PortalError, _find_login_form, _soup

CALENDAR_URL = PORTAL_BASE + "/index.php?mod=calendar"
AJAX_URL = PORTAL_BASE + "/index.php"

# For the AI: what the portal's rows mean, next to the data.
MEANING = (
    "These are the portal's system windows: when something can be done in the portal "
    "(registration, add/drop, withdrawal, the FX application) or when teachers enter attendance "
    "and grades. \"until\" is the last moment a window is open: a date alone means to the end "
    "of that day, a date with a time means until that time. WHEN EXAMS TAKE PLACE comes from the "
    "academic calendar in the knowledge base: the portal's \"Final Exam\" window is longer than "
    "the exam period. Holidays and the next semesters are there too."
)

C: dict[str, dict[str, str]] = i18n.by_key("calendar")  # locales/<lang>.json, section "calendar"


def t(key: str, lang: str) -> str:
    return C[key].get(core.normalize_lang(lang), C[key]["en"])


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime | None  # campus time, as the portal shows it
    end: datetime | None
    status: str             # the portal's word: OPENED, CLOSED, ACTIVE, INACTIVE


@dataclass
class Calendar:
    term: str  # "2026 - 2027 Fall"
    events: list[Event] = field(default_factory=list)
    permissions: list[Event] = field(default_factory=list)  # the student's own


# ------------------------------------------------------------- parsing ----


def _when(text: str) -> datetime | None:
    """"21.09.2026 00:00" - the page puts a <br> between the date and the time."""
    try:
        return datetime.strptime(" ".join(text.split()), "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def parse_events(html: str) -> list[Event]:
    """The rows of an Event Calendar or Individual Permissions table:
    #, title, [permitted to,] start, end, status."""
    events = []
    for row in _soup(html).find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        if len(cells) < 5 or not cells[0].isdigit():
            continue  # the caption and the header row
        events.append(Event(cells[1], _when(cells[-3]), _when(cells[-2]), cells[-1].upper()))
    return events


def parse_terms(html: str) -> list[tuple[str, str, bool]]:
    """(id, title, selected) of every term the page offers."""
    select = _soup(html).find("select", id="ysem")
    if select is None:
        return []
    return [(option["value"].strip(), option.get_text(" ", strip=True), option.has_attr("selected"))
            for option in select.find_all("option") if option.get("value", "").strip()]


def _checked(response: httpx.Response) -> str:
    if response.status_code in (401, 403):
        raise PortalError("session_expired")
    if response.status_code >= 500:
        raise PortalError("unavailable")
    text = response.text.strip()
    if text.startswith("#!") or _find_login_form(_soup(text)) is not None:  # signed out
        raise PortalError("session_expired")
    return text


def _table(response: httpx.Response) -> str:
    """The HTML table inside an AJAX answer."""
    try:
        answer = json.loads(_checked(response))
    except ValueError as exc:
        raise PortalError("unexpected_page") from exc
    if not isinstance(answer, dict) or str(answer.get("CODE")) != "1":
        raise PortalError("unavailable")
    return str(answer.get("DATA") or "")


async def fetch_calendar(
    cookies: dict[str, str], transport: httpx.AsyncBaseTransport | None = None
) -> Calendar:
    """The current term's System Calendar and the student's own permissions."""
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=TIMEOUT, headers=HEADERS, transport=transport
    ) as client:
        try:
            terms = parse_terms(_checked(await client.get(CALENDAR_URL)))
            if not terms:
                raise PortalError("unexpected_page")
            term_id, term = next(((id_, title) for id_, title, chosen in terms if chosen), terms[0][:2])
            tables = [
                _table(await client.post(AJAX_URL, data={"ajx": "1", "mod": "calendar", "action": action, "yt": term_id}))
                for action in ("GetCalendar", "GetPermissions")
            ]
        except httpx.HTTPError as exc:
            raise PortalError("unavailable") from exc
    return Calendar(term, parse_events(tables[0]), parse_events(tables[1]))


# ------------------------------------------------------------- showing ----

MARKS = {"open": "🟢", "upcoming": "🔜", "over": "⚪", "closed": "🔴"}


def state(event: Event, now: datetime) -> str:
    """open | upcoming | over | closed: by the dates, then by the portal's word."""
    if event.end and now >= event.end:
        return "over"
    if event.start and now < event.start:
        return "upcoming"
    return "open" if event.status in ("OPENED", "ACTIVE") else "closed"


def _at(moment: datetime | None) -> str:
    return moment.strftime("%d.%m.%Y %H:%M") if moment else "?"


def _line(event: Event, lang: str, now: datetime) -> str:
    when = state(event, now)
    return f"{MARKS[when]} <b>{escape(event.title)}</b>: {_at(event.start)} – {_at(event.end)} · {escape(t(when, lang))}"


def render(calendar: Calendar, lang: str, now: datetime) -> str:
    """The card (HTML), one line per window:

        🗓 System Calendar · 2026 - 2027 Fall
        my.sdu.edu.kz → Information → System Calendar

        🟢 Late withdrawal: 19.10.2026 00:00 – 12.12.2026 00:00 · open now
    """
    lines = [f"🗓 <b>{escape(t('title', lang))} · {escape(calendar.term)}</b>", f"<i>{escape(t('where', lang))}</i>", ""]
    lines += [_line(event, lang, now) for event in calendar.events] or [escape(t("empty", lang))]
    if calendar.permissions:
        lines += ["", f"👤 <b>{escape(t('mine', lang))}</b>"] + [_line(e, lang, now) for e in calendar.permissions]
    return "\n".join(lines)


def until(event: Event) -> str | None:
    """Until when a window is open, the way people say it. The portal closes most
    windows at 00:00, which a model reads as one day too late:
    12.12 00:00 -> "2026-12-11" (the whole day), 01.02 18:00 -> "2026-02-01 18:00"."""
    if event.end is None:
        return None
    if (event.end.hour, event.end.minute) == (0, 0):
        return (event.end - timedelta(days=1)).strftime("%Y-%m-%d")
    return event.end.strftime("%Y-%m-%d %H:%M")


def as_data(calendar: Calendar, now: datetime) -> dict:
    """What the AI reads: every window with its start, until when it is open and its state now."""
    def item(event: Event) -> dict:
        start = event.start.strftime("%Y-%m-%d %H:%M") if event.start else None
        return {"title": event.title, "from": start, "until": until(event), "now": state(event, now)}

    return {"term": calendar.term, "events": [item(e) for e in calendar.events],
            "my_permissions": [item(e) for e in calendar.permissions]}
