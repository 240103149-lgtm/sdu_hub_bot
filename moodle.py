"""Client for SDU Moodle (https://moodle.sdu.edu.kz) used by /deadline.

Like grade.py it knows nothing about Telegram. Two ways in, tried in order:

1. Moodle's official mobile web service: POST /login/token.php gives a token
   and the deadlines come back as JSON. Nothing is parsed from HTML, and the
   token (not the password) is what the bot keeps.
2. If the university has that service switched off: sign in through the normal
   login form (like a browser) and ask Moodle's own AJAX endpoint for the same
   data the website's "Timeline" block uses.

A wrong password is detected in step 1 and ends the attempt right away, so one
mistyped password never turns into two failed logins on the account.

    session = await login(username, password)     # keep `session`, NOT the password
    deadlines = await fetch_deadlines(session)
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

# Same form-reading helpers the portal client uses (hidden tokens, field names).
from grade import _find_login_form, _payload, _soup, _text_inputs

MOODLE_BASE = os.getenv("MOODLE_BASE_URL", "https://moodle.sdu.edu.kz").strip().rstrip("/")
TIMEOUT = float(os.getenv("MOODLE_TIMEOUT_SECONDS", "15"))
LOOKBACK_DAYS = 14  # recently missed deadlines are still shown, marked overdue
LOOKAHEAD_DAYS = 90
MAX_EVENTS = 50  # Moodle's own upper limit for this call

FUNCTION = "core_calendar_get_action_events_by_timesort"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; UniversityHubBot/1.0)", "Accept-Language": "en"}
DEAD_SESSION_CODES = {
    "invalidtoken",
    "accessexception",
    "servicerequireslogin",
    "invalidsesskey",
    "requireloginerror",
    "sessiontimedout",
}


class MoodleError(Exception):
    """A failure the bot can explain to the student.

    `key` is one of: bad_credentials, unavailable, unexpected_page, session_expired.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass
class Deadline:
    course: str  # "CSS 206" (or the course name when it has no code)
    title: str  # "Lab 3"
    due: int  # unix timestamp
    url: str = ""
    kind: str = ""  # Moodle event type: "due", "close", ...


# ------------------------------------------------------------------- login --


def _client(transport=None, cookies=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT, headers=HEADERS, transport=transport, cookies=cookies
    )


async def login(username: str, password: str, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Sign in and return a session dict that fetch_deadlines() understands."""
    async with _client(transport) as client:
        token = await _try_token(client, username, password)
        if token:
            return {"mode": "token", "token": token, "username": username}
        return await _web_login(client, username, password, transport)


async def _try_token(client: httpx.AsyncClient, username: str, password: str) -> str | None:
    try:
        response = await client.post(
            f"{MOODLE_BASE}/login/token.php",
            data={"username": username, "password": password, "service": "moodle_mobile_app"},
        )
    except httpx.HTTPError as exc:
        raise MoodleError("unavailable") from exc
    if response.status_code >= 500:
        raise MoodleError("unavailable")
    try:
        data = response.json()
    except ValueError:
        return None  # not JSON: the service is probably off, use the web form
    if not isinstance(data, dict):
        return None
    if data.get("token"):
        return str(data["token"])
    if data.get("errorcode") == "invalidlogin":
        raise MoodleError("bad_credentials")
    return None


def _sesskey(html: str) -> str:
    match = re.search(r'"sesskey":"([A-Za-z0-9]+)"', html)
    return match.group(1) if match else ""


async def _web_login(
    client: httpx.AsyncClient, username: str, password: str, transport: httpx.AsyncBaseTransport | None
) -> dict:
    try:
        page = await client.get(f"{MOODLE_BASE}/login/index.php")
    except httpx.HTTPError as exc:
        raise MoodleError("unavailable") from exc
    if page.status_code >= 500:
        raise MoodleError("unavailable")

    form = _find_login_form(_soup(page.text))
    if form is None:
        print(f"[moodle] no login form found at {page.url} (HTTP {page.status_code})")
        raise MoodleError("unexpected_page")

    data = _payload(form)  # includes Moodle's hidden "logintoken"
    data[_text_inputs(form)[0]["name"]] = username
    data[form.find("input", attrs={"type": "password"})["name"]] = password
    try:
        response = await client.post(urljoin(str(page.url), form.get("action") or ""), data=data)
        if _find_login_form(_soup(response.text)) is not None:
            raise MoodleError("bad_credentials")  # bounced back to the login page
        home = await client.get(f"{MOODLE_BASE}/my/")
    except httpx.HTTPError as exc:
        raise MoodleError("unavailable") from exc

    sesskey = _sesskey(home.text)
    if not sesskey:
        print(f"[moodle] no sesskey on {home.url} after login")
        raise MoodleError("unexpected_page")

    session = {
        "mode": "web",
        "cookies": {cookie.name: cookie.value for cookie in client.cookies.jar},
        "sesskey": sesskey,
        "username": username,
    }
    try:  # proves the login really worked (a guest also has a sesskey)
        await fetch_deadlines(session, transport=transport)
    except MoodleError as exc:
        if exc.key == "session_expired":
            raise MoodleError("bad_credentials") from exc
        raise
    return session


# --------------------------------------------------------------- deadlines --


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError as exc:
        raise MoodleError("session_expired") from exc  # an HTML login page instead of JSON


async def _events_via_token(client: httpx.AsyncClient, token: str, args: dict) -> list[dict]:
    response = await client.post(
        f"{MOODLE_BASE}/webservice/rest/server.php",
        data={"wstoken": token, "wsfunction": FUNCTION, "moodlewsrestformat": "json", **args},
    )
    if response.status_code >= 500:
        raise MoodleError("unavailable")
    data = _json(response)
    if isinstance(data, dict) and (data.get("exception") or data.get("errorcode")):
        code = str(data.get("errorcode", ""))
        if code in DEAD_SESSION_CODES:
            raise MoodleError("session_expired")
        print(f"[moodle] web service error: {code} {data.get('message', '')}")
        raise MoodleError("unexpected_page")
    return data.get("events", []) if isinstance(data, dict) else []


async def _events_via_web(client: httpx.AsyncClient, sesskey: str, args: dict) -> list[dict]:
    response = await client.post(
        f"{MOODLE_BASE}/lib/ajax/service.php",
        params={"sesskey": sesskey, "info": FUNCTION},
        json=[{"index": 0, "methodname": FUNCTION, "args": args}],
    )
    if response.status_code >= 500:
        raise MoodleError("unavailable")
    data = _json(response)
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise MoodleError("unexpected_page")
    item = data[0]
    if item.get("error"):
        code = str((item.get("exception") or {}).get("errorcode", ""))
        if code in DEAD_SESSION_CODES:
            raise MoodleError("session_expired")
        print(f"[moodle] AJAX error: {code}")
        raise MoodleError("unexpected_page")
    return (item.get("data") or {}).get("events", [])


def _course_label(full_name: str) -> str:
    """"CSS 206 Database Management Systems 1 (Dina Kengesbay)" -> "CSS 206"."""
    match = re.match(r"^([A-Z]{2,5})\s*(\d{3}[A-Za-z]?)\b", full_name)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return re.sub(r"\s*\([^)]*\)\s*$", "", full_name)[:40]


def _title(event: dict) -> str:
    title = str(event.get("activityname") or event.get("name") or "").strip()
    return re.sub(r"\s+(is due|closes|opens)$", "", title, flags=re.IGNORECASE)


def parse_events(events: list[dict]) -> list[Deadline]:
    result: list[Deadline] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        due = int(event.get("timesort") or event.get("timestart") or 0)
        if not due:
            continue
        course = event.get("course") or {}
        full = str(course.get("fullname") or course.get("shortname") or "").strip()
        action = event.get("action") or {}
        result.append(
            Deadline(
                course=_course_label(full),
                title=_title(event),
                due=due,
                url=str(event.get("url") or action.get("url") or ""),
                kind=str(event.get("eventtype") or ""),
            )
        )
    return sorted(result, key=lambda d: d.due)


async def fetch_deadlines(session: dict, transport: httpx.AsyncBaseTransport | None = None) -> list[Deadline]:
    """Everything the student still has to hand in, oldest first."""
    now = int(time.time())
    args = {
        "timesortfrom": now - LOOKBACK_DAYS * 86400,
        "timesortto": now + LOOKAHEAD_DAYS * 86400,
        "limitnum": MAX_EVENTS,
    }
    mode = session.get("mode")
    cookies = session.get("cookies") if mode == "web" else None
    async with _client(transport, cookies) as client:
        try:
            if mode == "token" and session.get("token"):
                events = await _events_via_token(client, session["token"], args)
            elif mode == "web" and session.get("sesskey"):
                events = await _events_via_web(client, session["sesskey"], args)
            else:
                raise MoodleError("session_expired")
        except httpx.HTTPError as exc:
            raise MoodleError("unavailable") from exc
    return parse_events(events)
