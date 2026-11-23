"""Public SDU Life events, shared by the website and Telegram.

Only the anonymous home-page payload is read. No login, private endpoints or
JavaScript execution is needed. A short cache also coalesces concurrent reads.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

SOURCE = "https://life.sdu.edu.kz/en"
TTL = 300
STALE_TTL = 3600
_cache: dict = {}
_lock = asyncio.Lock()


class FeedError(Exception):
    pass


def _image(value) -> str | None:
    url = str(value or "")
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in {"life.sdu.edu.kz", "s3.twcstorage.ru"}:
        return url
    return None


def parse_page(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    chunks = []
    for script in soup.find_all("script"):
        raw = script.string or ""
        if not raw.startswith("self.__next_f.push("):
            continue
        try:
            item = json.loads(raw[len("self.__next_f.push("):].rstrip(";)").strip())
        except (ValueError, TypeError):
            continue
        if isinstance(item, list) and len(item) == 2 and item[0] == 1 and isinstance(item[1], str):
            chunks.append(item[1])
    payload = "".join(chunks)
    # Flight serializes long descriptions as UTF-8 text records, referenced
    # by "$<id>". Length is bytes, not Python characters (Kazakh + emoji).
    text_records = {}
    raw_payload = payload.encode("utf-8")
    for match in re.finditer(rb"([a-f0-9]+):T([a-f0-9]+),", raw_payload):
        length = int(match.group(2), 16)
        try:
            text_records["$" + match.group(1).decode()] = raw_payload[match.end():match.end() + length].decode("utf-8")
        except UnicodeDecodeError:
            continue

    def description(value):
        value = str(value or "")
        return text_records.get(value, "") if re.fullmatch(r"\$[a-f0-9]+", value) else value

    events = {}
    for match in re.finditer(r'"events"\s*:\s*\[', payload):
        try:
            rows, _ = json.JSONDecoder().raw_decode(payload, match.end() - 1)
        except ValueError:
            continue
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("id", "")).isdigit() or not row.get("title"):
                continue
            try:
                moment = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            event_id = int(row["id"])
            events[event_id] = {
                "id": event_id, "title": str(row["title"])[:500],
                "date": moment.isoformat(), "has_time": bool(row.get("hasTime")),
                "venue": str(row.get("location") or "")[:500],
                "organizer": str(row.get("organizer") or "")[:200],
                "description": description(row.get("description"))[:8000],
                "categories": [str(c)[:100] for c in (row.get("category") or []) if isinstance(c, str)],
                "image": _image(row.get("image")),
                "url": f"https://life.sdu.edu.kz/en/events/{event_id}",
            }
    if not events:
        raise FeedError("The public SDU Life event payload is unavailable")
    return sorted(events.values(), key=lambda e: (e["date"], e["id"]))


async def fetch_events() -> dict:
    if os.getenv("CAMPUS_EVENTS_LIVE", "on").lower() == "off":
        raise FeedError("Live events disabled")
    now = time.time()
    if _cache and now - _cache["fetched_at"] < TTL:
        return {**_cache, "stale": False}
    async with _lock:
        now = time.time()
        if _cache and now - _cache["fetched_at"] < TTL:
            return {**_cache, "stale": False}
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(SOURCE, headers={"User-Agent": "SDUHub/1.0 (public campus events)"})
                response.raise_for_status()
            rows = parse_page(response.text)
        except (httpx.HTTPError, FeedError) as exc:
            if _cache and now - _cache["fetched_at"] < STALE_TTL:
                return {**_cache, "stale": True}
            raise FeedError("SDU Life is temporarily unavailable") from exc
        _cache.clear()
        _cache.update(events=rows, fetched_at=now, source=SOURCE)
        return {**_cache, "stale": False}


def select(events: list[dict], today, start=None, end=None) -> list[dict]:
    from rooms import CAMPUS_TZ

    def day(event):
        return datetime.fromisoformat(event["date"]).astimezone(CAMPUS_TZ).date()

    return [e for e in events if (start <= day(e) <= end if start and end else day(e) >= today)]
