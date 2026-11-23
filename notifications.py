"""Class (15 min) and Moodle deadline (12 h) reminders.

Two ways to run the check:

* A persistent host (python bot.py, or uvicorn with TELEGRAM_MODE=webhook
  outside Vercel): install() adds a job that runs tick() every minute.
* Vercel: no process stays alive, so /api/cron/reminders runs durable_tick()
  each time it is called (.github/workflows/reminders.yml, every 5 minutes).
  It reads the students' records from Redis (bot_store); without Redis the
  endpoint does nothing and /notify says delivery is paused.

With Redis every reminder is claimed there before it is sent, so two checks
running at once (a tick and an "enable" click) never send it twice.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import os
import random
import time
import weakref
from datetime import datetime
from types import SimpleNamespace

from telegram.error import Forbidden, TelegramError

import bot_store
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
TICK_BUDGET = 40  # seconds one scheduled check may spend; the rest waits for the next


def enabled(data, key):
    """Account linking alone does not subscribe a student to notifications."""
    return bool(data.get(key, False))


def _turned_off() -> bool:
    return os.getenv("REMINDERS_ENABLED", "on").strip().lower() == "off"


async def delivery_paused() -> bool:
    """True when nothing will send a reminder later, whatever the buttons say.

    A persistent host runs the minute job (install() fails loudly without it).
    On Vercel delivery needs Redis and a recent scheduled check.
    """
    if _turned_off():
        return True
    if not os.getenv("VERCEL"):
        return False
    store = bot_store.current()
    if store is None:
        return True
    try:
        last = await store.last_heartbeat()
    except bot_store.StoreError:
        return True
    return last is None or time.time() - last > bot_store.HEARTBEAT_FRESH


def _session_tag(session) -> str:
    """A short fingerprint of one linked session (never the session itself)."""
    raw = repr(sorted((session.get("cookies") or {}).items())) + str(session.get("token") or "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def check_now(app, user_id):
    """Catch up within the current window as soon as a student enables delivery."""
    if _turned_off():
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
                    course=item["title"], time=item["start"], room=item["room"] or labels["tbc"]), item["key"]))
        except grade.PortalError as exc:
            if exc.key in ("session_expired", "two_factor_required") and data.get("portal") == portal:
                data.pop("portal", None)
                data.pop("personal_schedule_cache", None)
                text = labels["portal_two_factor_required"] if exc.key == "two_factor_required" else labels["portal_expired"]
                messages.append(("portal:expired", stamp, text, "portal:expired:" + _session_tag(portal)))
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
                    key = deadline_key(item)
                    messages.append((key, item.due, labels["deadline_message"].format(
                        course=item.course, title=item.title, due=f"{due:%d.%m.%Y %H:%M}",
                        hours=hours, minutes=minutes), key))
        except moodle.MoodleError as exc:
            if exc.key == "session_expired" and data.get("moodle") == session:
                data.pop("moodle", None)
                # Once per expired session; the next successful link can resume.
                messages.append(("moodle:expired", stamp, labels["session_expired"],
                                 "moodle:expired:" + _session_tag(session)))
            else:
                log.warning("Deadline reminders skipped: Moodle unavailable")
    store = bot_store.current()
    for key, event_time, text, claim in messages:
        if key.startswith("class:") and (not enabled(data, "class_notifications") or data.get("portal") != portal):
            continue
        if key.startswith("deadline:") and not enabled(data, "deadline_notifications"):
            continue
        if key in sent:
            continue
        claim = f"{user_id}:{claim}"
        claimed = False
        if store is not None:
            try:
                if not await store.claim(claim):
                    sent[key] = event_time  # another check (another instance) sent it
                    continue
                claimed = True
            except bot_store.StoreError:
                # Better a rare duplicate than a missed class.
                log.warning("Reminder claim unavailable; sending with local deduplication only")
        try:
            await app.bot.send_message(chat_id=user_id, text=text, disable_web_page_preview=True)
        except Forbidden:
            data["reminders_blocked"] = True
            await _release(store, claim, claimed)
            break
        except TelegramError:
            log.warning("Reminder delivery failed; it will be retried")
            await _release(store, claim, claimed)
            continue
        sent[key] = event_time
    app.mark_data_for_update_persistence(user_ids=[user_id])


async def _release(store, claim, claimed):
    if claimed:
        try:
            await store.release(claim)
        except bot_store.StoreError:
            log.warning("Reminder claim could not be released; this reminder may be skipped")


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


def _merge(latest: dict, before: dict, after: dict) -> bool:
    """Put what one scheduled check changed into the newest copy of a record.

    The check worked on a copy that may be a few seconds old: the student can
    have relinked, unlinked or chatted meanwhile. Only the check's own
    results move over, and only while they still apply to the same session.
    """
    changed = False
    for account in ("portal", "moodle"):
        if account in before and account not in after and latest.get(account) == before[account]:
            latest.pop(account, None)  # this session expired during the check
            if account == "portal":
                latest.pop("personal_schedule_cache", None)
            changed = True
    cache = after.get("personal_schedule_cache")
    if (cache and cache != before.get("personal_schedule_cache") and after.get("portal")
            and latest.get("portal") == after.get("portal")):
        latest["personal_schedule_cache"] = cache
        changed = True
    old_sent, new_sent = before.get("reminders_sent") or {}, after.get("reminders_sent") or {}
    added = {key: value for key, value in new_sent.items() if old_sent.get(key) != value}
    pruned = [key for key in old_sent if key not in new_sent]  # older than a day
    if added or pruned:
        sent = latest.setdefault("reminders_sent", {})
        sent.update(added)
        for key in pruned:
            sent.pop(key, None)
        changed = True
    if after.get("reminders_blocked") and not latest.get("reminders_blocked"):
        latest["reminders_blocked"] = True
        changed = True
    return changed


async def durable_tick(bot, store, now: datetime) -> dict:
    """One scheduled check on a serverless host, from the records in Redis.

    It works on copies, never on the live Application's user_data, so a
    student's message that arrives meanwhile is not held up or overwritten.
    """
    started = time.monotonic()
    ids = await store.reminder_users()
    records = await store.load_users(ids)
    active = {uid: record for uid, record in records.items() if record and bot_store.subscribed(record)}
    stale = [uid for uid in ids if uid not in active]
    if stale:
        await store.unsubscribe(stale)
    before = {uid: copy.deepcopy(record) for uid, record in active.items()}
    shim = SimpleNamespace(user_data=active, bot=bot, mark_data_for_update_persistence=lambda **_: None)
    order = list(active)
    random.shuffle(order)  # if the time budget runs out, the next check starts elsewhere
    checked = failed = 0
    for offset in range(0, len(order), 20):
        if time.monotonic() - started > TICK_BUDGET:
            break
        batch = order[offset:offset + 20]
        results = await asyncio.gather(*(check_user(shim, uid, now) for uid in batch), return_exceptions=True)
        checked += len(batch)
        failed += sum(isinstance(result, Exception) for result in results)
    if failed:
        log.warning("%s reminder checks failed; other students were processed", failed)
    saved = 0
    for uid in order[:checked]:
        if bot_store.dump(active[uid]) == bot_store.dump(before[uid]):
            continue
        try:
            latest = await store.load_user(uid)
            if latest is not None and _merge(latest, before[uid], active[uid]):
                await store.save_user(uid, latest, was_subscribed=True)
                saved += 1
        except bot_store.StoreError:
            log.warning("A reminder record could not be updated; it is retried next time")
    await store.heartbeat()
    return {"students": len(active), "checked": checked, "failed": failed, "updated": saved}


def install(app) -> None:
    if os.getenv("VERCEL") or _turned_off():
        return
    if app.job_queue is None:
        raise RuntimeError("Install requirements.txt including python-telegram-bot[job-queue] for reminders")
    if not app.job_queue.get_jobs_by_name("student-reminders"):
        app.job_queue.run_repeating(tick, interval=60, first=5, name="student-reminders")
