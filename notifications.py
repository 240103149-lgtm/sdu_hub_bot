"""Minute-resolution reminders on a persistent polling or webhook host."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import weakref
from datetime import datetime

from telegram.error import Forbidden, TelegramError

import core
import i18n
import moodle
import rooms
import student_schedule
import grade
import portal_schedule

log = logging.getLogger(__name__)
_limit = asyncio.Semaphore(5)
_user_locks = weakref.WeakKeyDictionary()


def enabled(data, key):
    """Account linking alone does not subscribe a student to notifications."""
    return bool(data.get(key, False))


async def check_now(app, user_id):
    """Catch up within the current window as soon as a student enables delivery."""
    if os.getenv("REMINDERS_ENABLED", "on").lower() == "off":
        return
    await check_user(app, user_id, datetime.now(rooms.CAMPUS_TZ))
    await app.update_persistence()


async def personal_classes(data):
    portal = data.get("portal")
    if not portal:
        return []
    signature = hashlib.sha256(repr((portal.get("student_id"), sorted((portal.get("cookies") or {}).items()))).encode()).hexdigest()
    cache = data.get("personal_schedule_cache", {})
    if cache.get("session") == signature and 0 <= time.time() - cache.get("fetched", 0) < 900:
        return cache["items"]
    async with _limit:
        items = await portal_schedule.fetch_schedule(portal.get("cookies") or {})
    if data.get("portal") != portal:
        return []
    data["personal_schedule_cache"] = {"session": signature, "fetched": time.time(), "items": items}
    return items


def upcoming_personal(items, now):
    if not student_schedule.active(now.date()):
        return []
    result = []
    for item in items:
        if item["day"] != rooms.DAYS[now.weekday()]:
            continue
        hour, minute = map(int, item["start"].split(":"))
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if 0 < (moment - now).total_seconds() <= 900:
            identity = f"{item['course']}|{item.get('section', '')}|{moment.isoformat()}"
            result.append({**item, "key": "class:portal:" + hashlib.sha256(identity.encode()).hexdigest(),
                           "at": int(moment.timestamp())})
    return result


def deadline_key(item: moodle.Deadline) -> str:
    raw = f"{item.course}|{item.title}|{item.due}|{item.url}"
    return "deadline:" + hashlib.sha256(raw.encode()).hexdigest()


async def check_user(app, user_id: int, now: datetime) -> None:
    # A settings click and the minute job can overlap; serialize one student's
    # fetch/send/persist decisions so they cannot send the same event twice.
    locks = _user_locks.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(user_id, asyncio.Lock()):
        await _check_user(app, user_id, now)


async def _check_user(app, user_id: int, now: datetime) -> None:
    data = app.user_data[user_id]
    if data.get("reminders_blocked"):
        return
    lang = core.normalize_lang(data.get("lang", "en"))
    labels = i18n.section(lang, "notifications")
    stamp = int(now.timestamp())
    sent = data.setdefault("reminders_sent", {})
    for key in list(sent):
        if sent[key] < stamp - 86400:
            del sent[key]
    messages = []
    portal = data.get("portal")
    if enabled(data, "class_notifications") and portal:
        try:
            items = upcoming_personal(await personal_classes(data), now)
            for item in (items if enabled(data, "class_notifications") else []):
                messages.append((item["key"], item["at"], labels["class_message"].format(
                    course=item["title"], time=item["start"], room=item["room"] or labels["tbc"])))
        except grade.PortalError as exc:
            if exc.key in ("session_expired", "two_factor_required") and data.get("portal") == portal:
                data.pop("portal", None)
                data.pop("personal_schedule_cache", None)
                text = labels["portal_two_factor_required"] if exc.key == "two_factor_required" else labels["portal_expired"]
                messages.append(("portal:expired", stamp, text))
            else:
                log.warning("Class reminders skipped: MySDU unavailable")
        except (rooms.ScheduleError, OSError, ValueError):
            log.warning("Class reminders skipped: timetable unavailable")
    session = data.get("moodle")
    if enabled(data, "deadline_notifications") and session:
        try:
            async with _limit:
                items = await moodle.fetch_deadlines(session, upcoming_only=True)
            if data.get("moodle") != session or not enabled(data, "deadline_notifications"):
                items = []
            for item in items:
                remaining = item.due - stamp
                if 0 < remaining <= 12 * 3600 and item.kind.lower() in ("", "due", "close"):
                    due = datetime.fromtimestamp(item.due, rooms.CAMPUS_TZ)
                    hours, minutes = divmod(max(1, remaining // 60), 60)
                    messages.append((deadline_key(item), item.due, labels["deadline_message"].format(
                        course=item.course, title=item.title, due=f"{due:%d.%m.%Y %H:%M}",
                        hours=hours, minutes=minutes)))
        except moodle.MoodleError as exc:
            if exc.key == "session_expired" and data.get("moodle") == session:
                data.pop("moodle", None)
                # Once per expired session; the next successful link can resume.
                messages.append(("moodle:expired", stamp, labels["session_expired"]))
            else:
                log.warning("Deadline reminders skipped: Moodle unavailable")
    for key, event_time, text in messages:
        if key.startswith("class:") and (not enabled(data, "class_notifications") or data.get("portal") != portal):
            continue
        if key.startswith("deadline:") and not enabled(data, "deadline_notifications"):
            continue
        if key in sent:
            continue
        try:
            await app.bot.send_message(chat_id=user_id, text=text, disable_web_page_preview=True)
        except Forbidden:
            data["reminders_blocked"] = True
            break
        except TelegramError:
            log.warning("Reminder delivery failed; it will be retried")
            continue
        sent[key] = event_time
    app.mark_data_for_update_persistence(user_ids=[user_id])


async def tick(context) -> None:
    app = context.application
    now = datetime.now(rooms.CAMPUS_TZ)
    users = [uid for uid, data in app.user_data.items()
             if enabled(data, "class_notifications") or enabled(data, "deadline_notifications")]
    # Batch users so a large installation doesn't create an unbounded task list.
    for offset in range(0, len(users), 20):
        results = await asyncio.gather(*(check_user(app, uid, now) for uid in users[offset:offset + 20]),
                                       return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            log.warning("One reminder check failed; other students were processed")
    if users:
        await app.update_persistence()


def install(app) -> None:
    if os.getenv("VERCEL") or os.getenv("REMINDERS_ENABLED", "on").lower() == "off":
        return
    if app.job_queue is None:
        raise RuntimeError("Install requirements.txt including python-telegram-bot[job-queue] for reminders")
    if not app.job_queue.get_jobs_by_name("student-reminders"):
        app.job_queue.run_repeating(tick, interval=60, first=5, name="student-reminders")
