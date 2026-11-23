"""SDU Platform OAuth + PKCE. Provider handles passwords/2FA; tokens stay server-side."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from contextlib import contextmanager
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
import httpx

import core
import grade

ORIGIN = 'https://api-sdu.javazhan.tech'
SID_COOKIE = 'sdu_oauth_session'
CONFIG = core.BASE_DIR / 'sdu_oauth_client.json'
SCOPES = 'profile:read schedule:read'
router = APIRouter(prefix='/api/me/sdu')


def configuration():
    saved = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    client = os.getenv('SDU_OAUTH_CLIENT_ID') or saved.get('client_id')
    callback = os.getenv('SDU_OAUTH_REDIRECT_URI') or saved.get('redirect_uri')
    if not client or not callback:
        raise HTTPException(503, detail='accounts_not_configured')
    return client, callback


def cipher():
    secret = (os.getenv('WEB_SESSION_SECRET') or os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()
    if not secret:
        raise HTTPException(503, detail='accounts_not_configured')
    key = hashlib.sha256(b'SDU-OAuth-server-v1\0' + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


class Store:
    """Encrypted server records. Redis REST for Vercel; SQLite for a durable host/dev."""
    def __init__(self):
        self.url = os.getenv('SDU_OAUTH_REDIS_URL', '').rstrip('/')
        self.token = os.getenv('SDU_OAUTH_REDIS_TOKEN', '')
        if os.getenv('VERCEL') and not (self.url and self.token):
            # Never claim that a temporary Vercel filesystem is durable storage.
            raise HTTPException(503, detail='oauth_storage_not_configured')
        if self.url and (not self.url.startswith('https://') or not self.token):
            raise HTTPException(503, detail='oauth_storage_not_configured')
        self.path = Path(os.getenv('SDU_OAUTH_DB_PATH') or core.BASE_DIR / 'data/oauth.db')

    async def command(self, *args):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(self.url, json=list(args), headers={'Authorization': 'Bearer ' + self.token})
        except httpx.HTTPError:
            raise HTTPException(503, detail='oauth_storage_unavailable') from None
        if response.status_code != 200:
            raise HTTPException(503, detail='oauth_storage_unavailable')
        payload = response.json()
        if payload.get('error'):
            raise HTTPException(503, detail='oauth_storage_unavailable')
        return payload.get('result')

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL)')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def put(self, key, value, ttl):
        sealed = cipher().encrypt(json.dumps(value).encode()).decode()
        if self.url:
            await self.command('SET', 'sduhub:' + key, sealed, 'EX', max(1, int(ttl)))
        else:
            with self.database() as db:
                db.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?)', (key, sealed, time.time() + ttl))

    async def get(self, key, consume=False):
        if self.url:
            sealed = await self.command('GETDEL' if consume else 'GET', 'sduhub:' + key)
        else:
            with self.database() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT value, expires FROM records WHERE key=?', (key,)).fetchone()
                sealed = row[0] if row and row[1] > time.time() else None
                if consume or row and row[1] <= time.time():
                    db.execute('DELETE FROM records WHERE key=?', (key,))
        if not sealed:
            return None
        try:
            return json.loads(cipher().decrypt(sealed.encode()))
        except Exception:
            raise HTTPException(503, detail='oauth_storage_unavailable') from None

    async def delete(self, key):
        if self.url:
            await self.command('DEL', 'sduhub:' + key)
        else:
            with self.database() as db:
                db.execute('DELETE FROM records WHERE key=?', (key,))


def visitor(request):
    sid = request.cookies.get(SID_COOKIE, '')
    return sid if re.fullmatch(r'[A-Za-z0-9_-]{43}', sid) else ''


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


@router.post('/connect')
async def connect(request: Request):
    import web_accounts
    web_accounts.same_origin(request)
    client_id, callback = configuration()
    store = Store()
    sid = visitor(request) or secrets.token_urlsafe(32)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    await store.put('attempt:' + key(state), {'visitor': key(sid), 'verifier': verifier,
                    'expires': time.time() + 600}, 600)
    url = ORIGIN + '/oauth/authorize?' + urlencode({'response_type': 'code', 'client_id': client_id,
        'redirect_uri': callback, 'scope': SCOPES, 'state': state, 'code_challenge': challenge,
        'code_challenge_method': 'S256'})
    from fastapi.responses import JSONResponse
    response = JSONResponse({'authorization_url': url})
    response.set_cookie(SID_COOKIE, sid, httponly=True, secure=request.url.scheme == 'https',
                        samesite='lax', path='/api/me', max_age=30 * 86400)
    return response


@router.get('/callback')
async def callback(request: Request, state: str = '', code: str = '', error: str = ''):
    sid = visitor(request)
    if not sid or not re.fullmatch(r'[A-Za-z0-9_-]{43}', state):
        raise HTTPException(400, detail='invalid_oauth_state')
    store = Store()
    attempt_key = 'attempt:' + key(state)
    attempt = await store.get(attempt_key)
    if not attempt or attempt['expires'] <= time.time() or not hmac.compare_digest(attempt['visitor'], key(sid)):
        raise HTTPException(400, detail='invalid_oauth_state')
    attempt = await store.get(attempt_key, consume=True)
    if not attempt:
        raise HTTPException(400, detail='invalid_oauth_state')
    if error:
        return RedirectResponse('/?sdu=denied', status_code=303)
    if not code:
        raise HTTPException(400, detail='missing_oauth_code')
    client_id, redirect_uri = configuration()
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            result = await client.post(ORIGIN + '/oauth/token', data={
                'grant_type': 'authorization_code', 'code': code, 'client_id': client_id,
                'redirect_uri': redirect_uri, 'code_verifier': attempt['verifier']})
        if result.status_code != 200:
            raise HTTPException(502, detail='oauth_exchange_failed')
        token = result.json()
        if token.get('token_type', '').lower() != 'bearer' or not token.get('access_token'):
            raise HTTPException(502, detail='oauth_exchange_failed')
        ttl = int(token['expires_in'])
        if ttl <= 0:
            raise HTTPException(502, detail='oauth_exchange_failed')
        await store.put('token:' + key(sid), {'access_token': token['access_token'],
                        'scope': token.get('scope', ''), 'expires': time.time() + ttl}, ttl)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, detail='oauth_exchange_failed') from None
    return RedirectResponse('/?sdu=connected', status_code=303)


async def session(request):
    sid = visitor(request)
    if not sid:
        return {}
    record = await Store().get('token:' + key(sid))
    return {'mode': 'oauth', **record} if record and record['expires'] > time.time() else {}


async def read(session, path, scope):
    if scope not in session.get('scope', '').split():
        raise grade.PortalError('permission_denied')
    try:
        async with httpx.AsyncClient(timeout=95) as client:
            response = await client.get(ORIGIN + '/api/integrations/v1/' + path,
                                        headers={'Authorization': 'Bearer ' + session['access_token']})
        if response.status_code == 401:
            raise grade.PortalError('session_expired')
        if response.status_code == 403:
            raise grade.PortalError('permission_denied')
        if response.status_code == 409:
            detail = response.json().get('detail') or {}
            detail = detail if isinstance(detail, dict) else {}
            raise grade.PortalError('two_factor_required' if detail.get('code') == 'sdu_2fa_required' else 'session_expired')
        if response.status_code != 200:
            raise grade.PortalError('unavailable')
        return response.json()
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise grade.PortalError('unavailable') from None


async def schedule(session):
    data = await read(session, 'schedule', 'schedule:read')
    import portal_schedule
    import rooms
    result = []
    for row in data.get('schedule', []):
        raw_day = row.get('day_of_week')
        day = rooms.DAYS[int(raw_day) - 1] if str(raw_day).isdigit() and 1 <= int(raw_day) <= 7 else portal_schedule._day(raw_day)
        if not day or not row.get('start_time'):
            raise grade.PortalError('unexpected_page')
        result.append({'course': row.get('course_code') or row.get('course_name') or '',
            'title': row.get('course_name') or row.get('course_code') or '', 'day': day,
            'start': str(row['start_time'])[:5], 'end': str(row['end_time'])[:5] if row.get('end_time') else None,
            'room': row.get('room') or ('Online' if row.get('is_online') else ''),
            'section': row.get('section') or '', 'teacher': row.get('teacher') or '', 'source': 'mysdu'})
    return result


async def transcript(session):
    data = await read(session, 'transcript', 'transcript:read')
    groups = {}
    for row in data.get('courses', []):
        title = str(row.get('semester') or 'Transcript')
        semester = groups.setdefault(title, grade.Semester(title))
        value = lambda name: '' if row.get(name) is None else str(row[name])
        semester.courses.append(grade.Course(value('course_code'), value('course_name'), value('credits'),
            value('ects'), value('grade_percent'), value('letter_grade'), value('grade_point'), ''))
    return list(groups.values())


async def disconnect(request, response):
    sid = visitor(request)
    if not sid:
        return
    store = Store()
    record = await store.get('token:' + key(sid))
    # Local access ends even if the provider is temporarily unreachable.
    await store.delete('token:' + key(sid))
    response.delete_cookie(SID_COOKIE, path='/api/me')
    if record:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(ORIGIN + '/oauth/revoke', data={'token': record['access_token']})
        except httpx.HTTPError:
            pass
