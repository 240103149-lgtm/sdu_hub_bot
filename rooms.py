"""Free classrooms, worked out from the class schedule.

Like core.py it is shared by both entry points and knows nothing about HTTP
or Telegram:

    telegram_bot.py  -> /rooms
    main.py          -> GET /api/rooms and the "Free rooms" tab of the site

The schedule is data/schedule.json: one entry per class period, as exported
from the student portal. When a new semester starts, just replace the file -
it is re-read automatically. Unlike core.py this module needs no API keys,
so it works (and can be tested) without a .env file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------- config ----

SCHEDULE_FILE = Path(__file__).parent / "data" / "schedule.json"

# Kazakhstan has used a single time zone, UTC+5, since 1 March 2024 and has no
# daylight saving time. A fixed offset keeps "now" right even on a server whose
# time zone database still puts Almaty at UTC+6.
CAMPUS_TZ = timezone(timedelta(hours=5))

DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")  # in datetime.weekday() order

# Not rooms a student can sit down in: online sections and the gym.
SKIPPED_BUILDINGS = {"virtual building", "sports complex"}


class ScheduleError(Exception):
    """data/schedule.json is missing, unreadable or has no classes in it."""


@dataclass(frozen=True)
class Slot:
    """One class period, in minutes since midnight."""

    start: int
    end: int


@dataclass(frozen=True)
class Room:
    name: str      # as written in the schedule, e.g. "H 201"
    block: str     # its letters, e.g. "H" - rooms are grouped by them
    building: str


@dataclass(frozen=True)
class FreeRoom:
    room: Room
    until: int | None  # when its next class that day starts; None: no more classes


@dataclass(frozen=True)
class View:
    """The day and class period to list free rooms for.

    `status` explains a choice the student didn't make themselves:
        now       the class period that is on right now
        next      a break, or before the first period: the upcoming one
        day_over  today's periods are over, so this is the next class day
        day_off   there are no classes today, so this is the next class day
    """

    day: str
    slot: Slot
    status: str | None = None


@dataclass
class Schedule:
    rooms: dict[str, Room]                   # room key ("H201") -> room
    busy: dict[tuple[str, str], list[Slot]]  # (day, room key) -> its classes
    slots: list[Slot]                        # every class period, in order
    days: list[str]                          # days with classes, Monday first


# ---------------------------------------------------------- time & rooms ----


def to_minutes(value: Any) -> int:
    """'14:30', '14.30', '1430' or '14' -> minutes since midnight."""
    match = re.fullmatch(r"(\d{1,2})(?:[:.]?(\d{2}))?", str(value).strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2) or 0) > 59:
        raise ValueError(f"not a time: {value!r}")
    return int(match.group(1)) * 60 + int(match.group(2) or 0)


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def room_key(name: str) -> str:
    """'H 201', 'H201' and 'h 201' are the same room."""
    return re.sub(r"\s+", "", name).upper()


def _room_order(room: Room) -> tuple[str, int, str]:
    """Block, then number: H 01, H 02, H 101 - not H 01, H 101, H 02."""
    key = room_key(room.name)
    number = re.search(r"\d+", key)
    return room.block, int(number.group()) if number else -1, key


# -------------------------------------------------------------- loading ----

_cache: tuple[float, Schedule] | None = None


def load_schedule() -> Schedule:
    """The parsed schedule, re-read only when the file has changed on disk."""
    global _cache
    try:
        mtime = SCHEDULE_FILE.stat().st_mtime
    except OSError as exc:
        raise ScheduleError(f"{SCHEDULE_FILE} not found") from exc
    if _cache is None or _cache[0] != mtime:
        try:
            entries = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ScheduleError(f"{SCHEDULE_FILE.name} could not be read: {exc}") from exc
        _cache = (mtime, parse_schedule(entries))
    return _cache[1]


def parse_schedule(entries: Any) -> Schedule:
    """Build the lookup tables from the list of class periods."""
    if not isinstance(entries, list):
        raise ScheduleError("schedule.json must be a list of classes")

    rooms: dict[str, Room] = {}
    busy: dict[tuple[str, str], list[Slot]] = {}
    periods: dict[int, int] = {}  # start -> end of the shortest class starting then
    days: set[str] = set()
    skipped = 0

    for entry in entries:
        try:
            day = str(entry["day"]).strip()[:2].title()
            slot = Slot(to_minutes(entry["start"]), to_minutes(entry["end"]))
            if day not in DAYS or slot.end <= slot.start:
                raise ValueError(day)
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        # Online classes still count for the class periods and days.
        days.add(day)
        periods[slot.start] = min(slot.end, periods.get(slot.start, slot.end))

        building = str(entry.get("building") or "").strip()
        virtual = str(entry.get("virtual")).strip().lower() == "true"
        if virtual or building.lower() in SKIPPED_BUILDINGS:
            continue
        # "I 110, I 111" is one class held in two rooms: both are busy.
        for name in str(entry.get("room") or "").split(","):
            name = " ".join(name.split())
            if not name:
                continue
            key = room_key(name)
            if key not in rooms:
                rooms[key] = Room(name, re.match(r"[A-Z]*", key).group(), building)
            busy.setdefault((day, key), []).append(slot)

    if skipped:
        print(f"[rooms] skipped {skipped} schedule entries without a valid day or time")
    if not periods:
        raise ScheduleError("schedule.json has no classes in it")
    return Schedule(
        rooms=rooms,
        busy=busy,
        slots=[Slot(start, end) for start, end in sorted(periods.items())],
        days=[day for day in DAYS if day in days],
    )


# ---------------------------------------------------------------- views ----


def _slot_at(schedule: Schedule, clock: int) -> Slot | None:
    """The period that is on at `clock`, or else the next one to start."""
    return next((slot for slot in schedule.slots if clock < slot.end), None)


def current_view(now: datetime | None = None) -> View:
    """The class period to show when a student asks what is free right now."""
    schedule = load_schedule()
    local = (now or datetime.now(timezone.utc)).astimezone(CAMPUS_TZ)
    today = DAYS[local.weekday()]
    clock = local.hour * 60 + local.minute

    if today in schedule.days:
        slot = _slot_at(schedule, clock)
        if slot is not None:
            return View(today, slot, "now" if clock >= slot.start else "next")
        status = "day_over"
    else:
        status = "day_off"

    index = DAYS.index(today)
    following = DAYS[index + 1:] + DAYS[: index + 1]
    next_day = next(day for day in following if day in schedule.days)
    return View(next_day, schedule.slots[0], status)


def pick_view(day: str | None = None, time: str | None = None, now: datetime | None = None) -> View:
    """The period a student asked for; whatever they left out means "now".

    Raises ValueError for a day without classes, a time that isn't one, or a
    time after the last class period.
    """
    current = current_view(now)
    if not day and not time:
        return current

    schedule = load_schedule()
    status = None
    if day:
        day = day.strip()[:2].title()
        if day not in schedule.days:
            raise ValueError(f"no classes on {day!r}")
    else:
        day = current.day
        if current.status in ("day_over", "day_off"):
            status = current.status  # still worth saying it isn't today

    if not time:
        return current if day == current.day else View(day, schedule.slots[0])
    slot = _slot_at(schedule, to_minutes(time))
    if slot is None:
        raise ValueError(f"no class periods after {time}")
    return View(day, slot, status)


def free_rooms(view: View) -> list[FreeRoom]:
    """Rooms with no class during the view's period, in block and number order."""
    schedule = load_schedule()
    free: list[FreeRoom] = []
    for key, room in schedule.rooms.items():
        classes = schedule.busy.get((view.day, key), [])
        if any(c.start < view.slot.end and view.slot.start < c.end for c in classes):
            continue
        until = min((c.start for c in classes if c.start >= view.slot.end), default=None)
        free.append(FreeRoom(room, until))
    free.sort(key=lambda item: _room_order(item.room))
    return free
