"""Course-section selection; the campus export is never a personal timetable."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from functools import lru_cache

import rooms


def group_id(row: dict) -> str:
    key = "|".join(str(row.get(k, "")) for k in ("course", "type", "section"))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def _load(stamp: int) -> list[dict]:
    rows = json.loads(rooms.SCHEDULE_FILE.read_text(encoding="utf-8"))
    groups = {}
    for row in rows:
        try:
            valid = row.get("day") in rooms.DAYS and rooms.to_minutes(row["start"]) < rooms.to_minutes(row["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if not valid or not row.get("course") or not row.get("section"):
            continue
        key = group_id(row)
        group = groups.setdefault(key, {
            "id": key, "course": row["course"], "title": row.get("course_title", row["course"]),
            "type": row.get("type", ""), "section": row["section"], "sessions": [],
        })
        session = {k: row.get(k, "") for k in ("day", "start", "end", "room", "teacher")}
        if session not in group["sessions"]:
            group["sessions"].append(session)
    for group in groups.values():
        group["sessions"].sort(key=lambda s: (rooms.DAYS.index(s["day"]), s["start"]))
    return sorted(groups.values(), key=lambda g: (g["course"], g["type"], g["section"]))


def groups(query: str = "") -> list[dict]:
    try:
        items = _load(rooms.SCHEDULE_FILE.stat().st_mtime_ns)
    except (OSError, ValueError, TypeError) as exc:
        raise rooms.ScheduleError("The campus timetable could not be read") from exc
    query = re.sub(r"\s+", "", query).casefold()
    return [g for g in items if query in re.sub(r"\s+", "", g["title"]).casefold()]


def term() -> dict:
    meta = json.loads(rooms.SCHEDULE_META.read_text(encoding="utf-8"))
    return {"updated": meta.get("updated"), "valid_from": meta.get("valid_from"), "valid_until": meta.get("valid_until")}


def active(day: date) -> bool:
    info = term()
    return bool(info["valid_from"] and info["valid_until"] and
                date.fromisoformat(info["valid_from"]) <= day <= date.fromisoformat(info["valid_until"]))


def upcoming(selected: list[str], now: datetime) -> list[dict]:
    if not active(now.date()):
        return []
    result = []
    for group in groups():
        if group["id"] not in selected:
            continue
        for session in group["sessions"]:
            if session["day"] != rooms.DAYS[now.weekday()]:
                continue
            hour, minute = map(int, session["start"].split(":"))
            moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if 0 < (moment - now).total_seconds() <= 15 * 60:
                result.append({**session, "course": group["course"], "title": group["title"],
                               "key": f"class:{group['id']}:{moment.isoformat()}", "at": int(moment.timestamp())})
    return result
