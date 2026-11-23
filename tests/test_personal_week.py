"""Authenticated week data: real upstream clients with scripted HTTP, no accounts."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import asyncio
from datetime import datetime
import json
import time
import unittest
from urllib.parse import parse_qs
from unittest.mock import AsyncMock, patch

import httpx
from starlette.requests import Request

import grade
import main
import moodle
import portal_schedule
import rooms
import web_accounts

LESSON = {'course': 'CSS 217', 'title': 'Software Architecture', 'day': 'Mo', 'start': '12:30',
          'end': '13:20', 'room': 'G107', 'section': '01-N', 'source': 'mysdu'}
LOGIN = '<form method="post" action="/login"><input name="student"><input name="password" type="password"></form>'
CODE = '<p>Verification code from email</p><form method="post" action="/verify"><input type="hidden" name="csrf" value="csrf-test"><input name="otp" type="text" autocomplete="one-time-code"></form>'
DASHBOARD = '<h1>Student portal</h1><a href="/index.php?mod=personal-course-grid">Course Schedule</a>'
TABLE = '<table><tr><th>Day</th><th>Start</th><th>End</th><th>Course</th><th>Course title</th><th>Room</th><th>Section</th></tr><tr><td>Monday</td><td>12:30</td><td>13:20</td><td>CSS217</td><td>Software Architecture</td><td>G107</td><td>01-N</td></tr></table>'


class PortalScheduleTests(unittest.IsolatedAsyncioTestCase):
    def test_personal_table_fields(self):
        self.assertEqual(portal_schedule.parse_schedule(TABLE), [LESSON])

    def test_weekly_grid_rowspan_and_real_times(self):
        grid = '''<table><tr><th>Time</th><th>Monday</th><th>Tuesday</th><th>Wednesday</th></tr>
        <tr><td>08:30–09:20</td><td rowspan="2">CSS 217<br>01-N<br>G107</td><td></td><td></td></tr>
        <tr><td>09:30–10:20</td><td></td><td></td></tr></table>'''
        items = portal_schedule.parse_schedule(grid)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['start'], items[0]['end'], items[0]['room']), ('08:30', '10:20', 'G107'))

    def test_empty_and_unknown_pages_are_distinct(self):
        self.assertEqual(portal_schedule.parse_schedule('<p>No registered courses</p>'), [])
        with self.assertRaises(grade.PortalError) as error:
            portal_schedule.parse_schedule('<h1>Another portal page</h1>')
        self.assertEqual(error.exception.key, 'unexpected_page')

    def test_missing_end_time_is_not_invented(self):
        single = TABLE.replace('<td>13:20</td>', '<td></td>')
        self.assertIsNone(portal_schedule.parse_schedule(single)[0]['end'])

    def test_unrecognized_class_is_not_reported_as_empty(self):
        grid = '<table><tr><th>Time</th><th>Monday</th><th>Tuesday</th><th>Wednesday</th></tr><tr><td>12:30–13:20</td><td>New class markup</td><td></td><td></td></tr></table>'
        with self.assertRaises(grade.PortalError):
            portal_schedule.parse_schedule(grid)

    async def test_follows_authenticated_schedule_link_only(self):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, text=DASHBOARD if request.url.path == '/' else TABLE)
        result = await portal_schedule.fetch_schedule({'SID': 'session-test'}, httpx.MockTransport(respond))
        self.assertEqual(result, [LESSON])
        self.assertEqual(seen[1].url.query, b'mod=personal-course-grid')
        self.assertTrue(all('SID=session-test' in request.headers['cookie'] for request in seen))
        self.assertIsNone(portal_schedule.schedule_link('<a href="https://other.example/">Course Schedule</a>', grade.PORTAL_BASE))

    async def test_expired_session_never_uses_campus_export(self):
        with self.assertRaises(grade.PortalError) as error:
            await portal_schedule.fetch_schedule({'SID': 'expired'}, httpx.MockTransport(lambda request: httpx.Response(200, text=LOGIN)))
        self.assertEqual(error.exception.key, 'session_expired')


class PersonalWeekApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'WEB_SESSION_SECRET': 'offline-test-session-encryption-key'})
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test')
        self.headers = {'X-SDU-Hub': '1', 'Origin': 'https://hub.test'}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()

    async def connect_moodle(self, client=None):
        with patch.object(moodle, 'login', new=AsyncMock(return_value={'mode': 'token', 'token': 'private-session', 'username': 'student'})):
            return await (client or self.client).post('/api/me/moodle/login', json={'username': 'student', 'password': 'test-password'}, headers=self.headers)

    def set_session(self, account, data):
        token = web_accounts._cipher().encrypt(json.dumps({'account': account, 'session': data}).encode()).decode()
        self.client.cookies.set(web_accounts.PREFIX + account, token, domain='hub.test', path='/api/me')
        return token

    async def test_disconnected_week_does_not_contact_upstreams(self):
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock()) as fetch, patch.object(moodle, 'fetch_deadlines', new=AsyncMock()) as tasks:
            response = await self.client.get('/api/me/week?week=2026-11-25')
        self.assertEqual(response.json()['week_start'], '2026-11-23')
        self.assertEqual(response.json()['mysdu']['status'], 'disconnected')
        self.assertEqual(response.json()['moodle']['status'], 'disconnected')
        self.assertIn('no-store', response.headers['cache-control'])
        fetch.assert_not_awaited(); tasks.assert_not_awaited()

    async def test_encrypted_cookie_http_only_no_password_and_independent_users(self):
        response = await self.connect_moodle()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'connected'})
        cookie = response.headers['set-cookie']
        self.assertIn('HttpOnly', cookie); self.assertIn('Secure', cookie); self.assertIn('SameSite=strict', cookie)
        self.assertNotIn('test-password', cookie + response.text)
        self.assertNotIn('private-session', cookie + response.text)
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=[])) as fetch:
            linked = await self.client.get('/api/me/week')
            self.assertEqual(linked.json()['moodle']['status'], 'ready')
            self.assertEqual(fetch.call_args.args[0]['token'], 'private-session')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test') as another:
                self.assertEqual((await another.get('/api/me/week')).json()['moodle']['status'], 'disconnected')

    async def test_portal_two_factor_survives_a_new_request(self):
        real_login = grade.PortalLogin
        def respond(request):
            if request.method == 'GET':
                return httpx.Response(200, text=LOGIN)
            data = parse_qs(request.content.decode())
            if request.url.path == '/login':
                self.assertEqual(data['password'], ['test-password'])
                return httpx.Response(200, text=CODE, headers={'set-cookie': 'SID=pending; Path=/'})
            self.assertEqual(data['otp'], ['123456'])
            self.assertIn('SID=pending', request.headers['cookie'])
            return httpx.Response(200, text=DASHBOARD, headers={'set-cookie': 'SID=verified; Path=/'})
        with patch.object(grade, 'PortalLogin', side_effect=lambda: real_login(httpx.MockTransport(respond))):
            response = await self.client.post('/api/me/mysdu/login', json={'username': '240103149', 'password': 'test-password'}, headers=self.headers)
            self.assertEqual(response.json()['status'], 'code_required')
            pending = self.client.cookies.get(web_accounts.PREFIX + 'pending')
            decoded = web_accounts._cipher().decrypt(pending.encode()).decode()
            self.assertNotIn('test-password', decoded)
            self.assertNotIn('password', decoded)
            response = await self.client.post('/api/me/mysdu/code', json={'code': '123456'}, headers=self.headers)
        self.assertEqual(response.json()['status'], 'connected')
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[LESSON])) as fetch:
            data = (await self.client.get('/api/me/week?week=2026-11-23')).json()
        self.assertEqual(fetch.call_args.args[0], {'SID': 'verified'})
        self.assertEqual(data['mysdu']['items'][0]['date'], '2026-11-23')
        self.assertEqual(data['mysdu']['items'][0]['room'], 'G107')
        self.assertIsNone(self.client.cookies.get(web_accounts.PREFIX + 'pending'))

    async def test_partial_source_failure_preserves_other_data_and_week_boundary(self):
        self.set_session('mysdu', {'cookies': {'SID': 'test'}})
        self.set_session('moodle', {'mode': 'token', 'token': 'test'})
        start = int(datetime(2026, 11, 23, tzinfo=rooms.CAMPUS_TZ).timestamp())
        items = [moodle.Deadline('CSS 217', 'Inside', start + 100), moodle.Deadline('CSS 217', 'Too early', start - 1),
                 moodle.Deadline('CSS 217', 'Next week', start + 604800), moodle.Deadline('CSS 217', 'Opens', start + 200, kind='open')]
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(side_effect=grade.PortalError('unavailable'))), patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=items)) as fetch:
            data = (await self.client.get('/api/me/week?week=2026-11-23')).json()
        self.assertEqual(data['mysdu']['status'], 'unavailable')
        self.assertEqual([item['title'] for item in data['moodle']['items']], ['Inside'])
        self.assertEqual(fetch.call_args.kwargs['window'], (start, start + 604799))
        self.assertIn('+05:00', data['moodle']['items'][0]['due'])

    async def test_logout_and_expired_upstream_clear_connections(self):
        await self.connect_moodle()
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock(side_effect=moodle.MoodleError('session_expired'))):
            data = (await self.client.get('/api/me/week')).json()
        self.assertEqual(data['moodle']['status'], 'expired')
        self.assertIsNone(self.client.cookies.get(web_accounts.PREFIX + 'moodle'))
        await self.connect_moodle()
        response = await self.client.post('/api/me/disconnect', json={'account': 'moodle'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.get('/api/me/week')).json()['moodle']['status'], 'disconnected')

    async def test_cross_site_login_and_tampered_cookie_rejected(self):
        with patch.object(moodle, 'login', new=AsyncMock()) as login:
            response = await self.client.post('/api/me/moodle/login', json={'username': 'student', 'password': 'test-password'}, headers={'X-SDU-Hub': '1', 'Origin': 'https://other.test'})
        self.assertEqual(response.status_code, 403); login.assert_not_awaited()
        self.client.cookies.set(web_accounts.PREFIX + 'moodle', 'not-a-valid-token', domain='hub.test', path='/api/me')
        self.assertEqual((await self.client.get('/api/me/week')).json()['moodle']['status'], 'disconnected')

    async def test_cookie_ttl_and_account_binding(self):
        raw = json.dumps({'account': 'moodle', 'session': {'token': 'private'}}).encode()
        token = web_accounts._cipher().encrypt_at_time(raw, int(time.time()) - web_accounts.SESSION_TTL - 1).decode()
        self.client.cookies.set(web_accounts.PREFIX + 'moodle', token, domain='hub.test', path='/api/me')
        self.assertEqual((await self.client.get('/api/me/week')).json()['moodle']['status'], 'disconnected')
        self.set_session('mysdu', {'cookies': {}})
        self.client.cookies.set(web_accounts.PREFIX + 'mysdu', web_accounts._cipher().encrypt(raw).decode(), domain='hub.test', path='/api/me')
        self.assertEqual((await self.client.get('/api/me/week')).json()['mysdu']['status'], 'disconnected')

    async def test_validation_response_never_echoes_password(self):
        secret = 'test-password' * 90
        response = await self.client.post('/api/me/moodle/login', json={'username': 'student', 'password': secret}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertEqual(response.json()['detail'], 'invalid_input')

    async def test_week_page_has_no_manual_search_or_section_picker(self):
        page = (await self.client.get('/')).text
        self.assertNotIn('id="course-search"', page)
        self.assertNotIn('id="schedule-results"', page)
        self.assertNotIn('id="more-sections"', page)
        self.assertIn('id="week-accounts"', page)
        self.assertIn('id="account-form"', page)
