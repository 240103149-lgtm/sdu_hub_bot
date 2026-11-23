"""Minute-resolution reminders on a persistent polling or webhook host."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime

from telegram.error import Forbidden, TelegramError

import core
import i18n
import moodle
import rooms
import student_schedule

log = logging.getLogger(__name__)
_limit = asyncio.Semaphore(5)


def deadline_key(item: moodle.Deadline) -> str:
    raw = f"{item.course}|{item.title}|{item.due}|{item.url}"
    return "deadline:" + hashlib.sha256(raw.encode()).hexdigest()


async def check_user(app, user_id: int, now: datetime) -> None:
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
    if data.get("class_notifications") and data.get("schedule_groups"):
        try:
            for item in student_schedule.upcoming(data["schedule_groups"], now):
                messages.append((item["key"], item["at"], labels["class_message"].format(
                    course=item["title"], time=item["start"], room=item["room"] or labels["tbc"])))
        except (rooms.ScheduleError, OSError, ValueError):
            log.warning("Class reminders skipped: timetable unavailable")
    session = data.get("moodle")
    if data.get("deadline_notifications") and session:
        try:
            async with _limit:
                items = await moodle.fetch_deadlines(session, upcoming_only=True)
            if data.get("moodle") != session or not data.get("deadline_notifications"):
                return
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
             if data.get("class_notifications") or data.get("deadline_notifications")]
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
