"""Website account sessions and the authenticated My week API.

Sessions use encrypted, HttpOnly cookies so Vercel cold starts do not erase
them. Passwords and OTPs are never retained or returned to the browser.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

import grade
import moodle
import portal_schedule
import rooms

router = APIRouter(prefix="/api/me")
SESSION_TTL = 8 * 3600
PENDING_TTL = 600
PREFIX = "sdu_web_"


def _cipher() -> Fernet:
    secret = (os.getenv("WEB_SESSION_SECRET") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not secret:
        raise HTTPException(503, detail="accounts_not_configured")
    key = hashlib.sha256(b"SDU-Hub-website-sessions-v1\0" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def read_session(request: Request, account: str) -> dict:
    token = request.cookies.get(PREFIX + account, "")
    if not token or len(token) > 3800:
        return {}
    try:
        raw = _cipher().decrypt(token.encode(), ttl=PENDING_TTL if account == "pending" else SESSION_TTL)
        data = json.loads(raw)
        return data["session"] if data.get("account") == account and isinstance(data.get("session"), dict) else {}
    except (InvalidToken, ValueError, KeyError, TypeError):
        return {}


def write_session(response: Response, request: Request, account: str, session: dict):
    token = _cipher().encrypt(json.dumps({"account": account, "session": session}, separators=(",", ":")).encode()).decode()
    if len(token) > 3800:
        raise HTTPException(503, detail="session_too_large")
    response.set_cookie(PREFIX + account, token, max_age=PENDING_TTL if account == "pending" else SESSION_TTL,
                        httponly=True, secure=bool(os.getenv("VERCEL")) or request.url.scheme == "https",
                        samesite="strict", path="/api/me")


def clear_session(response: Response, account: str):
    response.delete_cookie(PREFIX + account, path="/api/me")


def same_origin(request: Request):
    if request.headers.get("x-sdu-hub") != "1":
        raise HTTPException(403, detail="invalid_origin")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, detail="invalid_origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, detail="invalid_origin")


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=1024)


class Code(BaseModel):
    code: SecretStr = Field(min_length=4, max_length=16)


class Disconnect(BaseModel):
    account: Literal["mysdu", "moodle"]


def _pending(login) -> dict:
    form = login._code_form
    if form is None:
        raise HTTPException(502, detail="unexpected_page")
    # Reconstruct only the form controls needed by the existing login client.
    # Password fields or hidden password values are never serialized.
    inputs = []
    for item in form.find_all("input"):
        name = str(item.get("name") or "")
        if not name or "pass" in name.casefold() or "pwd" in name.casefold() or item.get("type") == "password":
            continue
        attributes = {key: str(item.get(key) or "") for key in ("name", "type", "id", "value", "placeholder", "autocomplete")}
        if attributes["type"] in ("text", "number", "tel", "email", ""):
            attributes["value"] = ""
        inputs.append("<input " + " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in attributes.items()) + ">")
    serialized = '<form method="' + html.escape(form.get("method") or "get", quote=True) + '" action="' + html.escape(form.get("action") or "", quote=True) + '">' + "".join(inputs) + "</form>"
    return {"cookies": login.cookies(), "form": serialized, "url": login._code_url}


@router.post("/mysdu/login")
async def portal_login(body: Credentials, request: Request, response: Response):
    same_origin(request)
    _cipher()  # Fail before sending credentials if sessions cannot be retained.
    if not body.username.strip().isdigit():
        raise HTTPException(422, detail="invalid_student_id")
    login = grade.PortalLogin()
    try:
        needs_code = await login.submit_credentials(body.username.strip(), body.password.get_secret_value())
        if needs_code:
            write_session(response, request, "pending", _pending(login))
            return {"status": "code_required"}
        cookies = login.cookies()
        if not cookies:
            raise grade.PortalError("unexpected_page")
        write_session(response, request, "mysdu", {"cookies": cookies})
        clear_session(response, "pending")
        return {"status": "connected"}
    except grade.PortalError as exc:
        raise HTTPException(401 if exc.key == "bad_credentials" else 502, detail=exc.key) from None
    finally:
        await login.close()


@router.post("/mysdu/code")
async def portal_code(body: Code, request: Request, response: Response):
    same_origin(request)
    pending = read_session(request, "pending")
    if not pending:
        raise HTTPException(401, detail="session_expired")
    login = grade.PortalLogin()
    try:
        for name, value in pending["cookies"].items():
            login.client.cookies.set(name, value, domain=urlsplit(grade.PORTAL_BASE).hostname, path="/")
        login._code_form = grade._soup(pending["form"]).find("form")
        login._code_url = pending["url"]
        await login.submit_code(body.code.get_secret_value().strip())
        write_session(response, request, "mysdu", {"cookies": login.cookies()})
        clear_session(response, "pending")
        return {"status": "connected"}
    except grade.PortalError as exc:
        raise HTTPException(401 if exc.key in ("bad_code", "session_lost") else 502, detail=exc.key) from None
    finally:
        await login.close()


@router.post("/moodle/login")
async def moodle_login(body: Credentials, request: Request, response: Response):
    same_origin(request)
    _cipher()
    try:
        session = await moodle.login(body.username.strip(), body.password.get_secret_value())
        write_session(response, request, "moodle", session)
        return {"status": "connected"}
    except moodle.MoodleError as exc:
        raise HTTPException(401 if exc.key == "bad_credentials" else 502, detail=exc.key) from None


@router.post("/disconnect")
async def disconnect(body: Disconnect, request: Request, response: Response):
    same_origin(request)
    clear_session(response, body.account)
    if body.account == "mysdu":
        import sdu_oauth
        await sdu_oauth.disconnect(request, response)
        clear_session(response, "pending")
    return {"status": "disconnected"}


@router.get("/week")
async def my_week(request: Request, response: Response, week: date | None = None):
    today = datetime.now(rooms.CAMPUS_TZ).date()
    first = week or today
    if abs((first - today).days) > 732:
        raise HTTPException(422, detail="invalid_week")
    first -= timedelta(days=first.weekday())
    last = first + timedelta(days=7)
    start = datetime.combine(first, datetime.min.time(), tzinfo=rooms.CAMPUS_TZ)
    finish = datetime.combine(last, datetime.min.time(), tzinfo=rooms.CAMPUS_TZ)
    portal, linked_moodle = read_session(request, "mysdu"), read_session(request, "moodle")

    async def schedule():
        if not portal:
            return {"status": "disconnected", "items": []}
        try:
            items = await portal_schedule.fetch_schedule(portal.get("cookies") or {})
            dated = [{**item, "date": (first + timedelta(days=rooms.DAYS.index(item["day"]))).isoformat()} for item in items]
            return {"status": "ready", "items": dated, "synced_at": datetime.now(rooms.CAMPUS_TZ).isoformat()}
        except grade.PortalError as exc:
            if exc.key == "session_expired":
                clear_session(response, "mysdu")
            return {"status": "expired" if exc.key == "session_expired" else "unavailable", "items": []}

    async def deadlines():
        if not linked_moodle:
            return {"status": "disconnected", "items": []}
        try:
            items = await moodle.fetch_deadlines(linked_moodle, window=(int(start.timestamp()), int(finish.timestamp()) - 1))
            return {"status": "ready", "synced_at": datetime.now(rooms.CAMPUS_TZ).isoformat(), "items": [
                {"course": item.course, "title": item.title,
                 "due": datetime.fromtimestamp(item.due, rooms.CAMPUS_TZ).isoformat(), "source": "moodle"}
                for item in items if start.timestamp() <= item.due < finish.timestamp()
                and item.kind.lower() in ("", "due", "close")]}
        except moodle.MoodleError as exc:
            if exc.key == "session_expired":
                clear_session(response, "moodle")
            return {"status": "expired" if exc.key == "session_expired" else "unavailable", "items": []}

    mysdu, tasks = await asyncio.gather(schedule(), deadlines())
    return {"week_start": first.isoformat(), "week_end": (last - timedelta(days=1)).isoformat(),
            "today": today.isoformat(), "timezone": "Asia/Almaty", "mysdu": mysdu, "moodle": tasks}


async def portal_session(request):
    import sdu_oauth
    linked = await sdu_oauth.session(request)
    return linked or read_session(request, 'mysdu')


async def portal_classes(session):
    if session.get('mode') == 'oauth':
        import sdu_oauth
        return await sdu_oauth.schedule(session)
    return await portal_schedule.fetch_schedule(session.get('cookies') or {})


async def portal_grades(session):
    if session.get('mode') == 'oauth':
        import sdu_oauth
        return await sdu_oauth.transcript(session)
    return await grade.fetch_transcript(session.get('cookies') or {})


@router.get('/month')
async def my_month(request: Request, response: Response, month: date | None = None):
    """A full calendar month; sources fail independently and completion is explicit."""
    import calendar
    today = datetime.now(rooms.CAMPUS_TZ).date()
    first = (month or today).replace(day=1)
    if not 2000 <= first.year <= 2100 or abs((first - today).days) > 732:
        raise HTTPException(422, detail='invalid_month')
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    linked_moodle = read_session(request, 'moodle')

    async def classes():
        try:
            portal = await portal_session(request)
            if not portal:
                return {'status': 'disconnected', 'items': []}
            rows = await portal_classes(portal)
            dated = []
            for offset in range(last.day):
                day = first + timedelta(days=offset)
                dated += [{**row, 'date': day.isoformat()} for row in rows if row['day'] == rooms.DAYS[day.weekday()]]
            return {'status': 'ready', 'items': dated, 'synced_at': datetime.now(rooms.CAMPUS_TZ).isoformat()}
        except (grade.PortalError, HTTPException) as exc:
            reason = exc.key if isinstance(exc, grade.PortalError) else str(exc.detail)
            if reason == 'session_expired':
                clear_session(response, 'mysdu')
            status = {'session_expired': 'expired', 'two_factor_required': 'code_required', 'permission_denied': 'permission_required', 'oauth_storage_not_configured': 'not_configured'}.get(reason, 'unavailable')
            return {'status': status, 'error': reason, 'items': []}

    async def tasks():
        if not linked_moodle:
            return {'status': 'disconnected', 'items': []}
        try:
            rows = await moodle.fetch_month(linked_moodle, first.year, first.month)
            items = []
            for row in rows:
                due = datetime.fromtimestamp(row.due, rooms.CAMPUS_TZ)
                if first <= due.date() <= last:
                    items.append({'id': row.event_id, 'course': row.course, 'title': row.title,
                                  'due': due.isoformat(), 'source': 'moodle', 'kind': row.kind,
                                  'completed': row.completed, 'url': row.url})
            return {'status': 'ready', 'items': items, 'synced_at': datetime.now(rooms.CAMPUS_TZ).isoformat()}
        except moodle.MoodleError as exc:
            if exc.key == 'session_expired':
                clear_session(response, 'moodle')
            return {'status': 'expired' if exc.key == 'session_expired' else 'unavailable',
                    'error': exc.key, 'items': []}

    mysdu, tasks = await asyncio.gather(classes(), tasks())
    return {'month_start': first.isoformat(), 'month_end': last.isoformat(), 'today': today.isoformat(),
            'timezone': 'Asia/Almaty', 'mysdu': mysdu, 'moodle': tasks}
