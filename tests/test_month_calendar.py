"""Full-month calendar, source isolation and verified Moodle completion states."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import json
from datetime import datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs
import httpx
import grade
import main
import moodle
import rooms
import web_accounts


class MoodleMonthTests(IsolatedAsyncioTestCase):
    async def test_full_month_includes_completed_events_and_deduplicates_calendar_cells(self):
        events = [{'id': 1, 'name': 'Submitted lab is due', 'timestart': 1793552400, 'modulename': 'assign',
                   'instance': 11, 'course': {'id': 4, 'fullname': 'INF 345 System Administration'}, 'eventtype': 'due'},
                  {'id': 2, 'name': 'Pending lab is due', 'timestart': 1793638800, 'modulename': 'assign',
                   'instance': 12, 'course': {'id': 4, 'fullname': 'INF 345 System Administration'}, 'eventtype': 'due'}]
        seen = []
        def upstream(request):
            args = parse_qs(request.content.decode()); function = args['wsfunction'][0]; seen.append(function)
            if function == 'core_calendar_get_calendar_monthly_view':
                self.assertEqual(args['month'], ['11'])
                return httpx.Response(200, json={'weeks': [{'days': [{'events': events}, {'events': events[:1]}]}]})
            if function == 'core_webservice_get_site_info':
                return httpx.Response(200, json={'userid': 42})
            self.assertEqual(args['userid'], ['42'])
            return httpx.Response(200, json={'statuses': [{'cmid': 11, 'tracking': 1, 'state': 1}, {'cmid': 12, 'tracking': 1, 'state': 0}]})
        result = await moodle.fetch_month({'mode': 'token', 'token': 'test'}, 2026, 11, httpx.MockTransport(upstream))
        self.assertEqual([row.completed for row in result], [True, False])
        self.assertEqual(len(result), 2)
        self.assertNotIn(moodle.FUNCTION, seen)  # The action feed drops already completed work.

    async def test_completion_permission_failure_is_unknown_not_falsely_completed(self):
        event = {'id': 1, 'name': 'Old task', 'timestart': 1000, 'modulename': 'assign', 'instance': 11,
                 'course': {'id': 4, 'fullname': 'INF 345'}}
        def upstream(request):
            function = parse_qs(request.content.decode())['wsfunction'][0]
            return httpx.Response(200, json={'weeks': [{'days': [{'events': [event]}]}]} if 'monthly' in function else
                                  {'exception': 'webservice_access_exception', 'errorcode': 'invalidrecord'})
        rows = await moodle.fetch_month({'mode': 'token', 'token': 'test'}, 2026, 11, httpx.MockTransport(upstream))
        self.assertIsNone(rows[0].completed)

    async def test_http_503_html_is_unavailable_not_expired_account(self):
        with self.assertRaises(moodle.MoodleError) as error:
            await moodle.fetch_month({'mode': 'token', 'token': 'test'}, 2026, 11,
                                      httpx.MockTransport(lambda request: httpx.Response(503, text='Maintenance')))
        self.assertEqual(error.exception.key, 'unavailable')

    async def test_web_session_uses_ajax_month_function(self):
        def upstream(request):
            self.assertIn('MoodleSession=test', request.headers['cookie'])
            body = json.loads(request.content)
            self.assertEqual(body[0]['methodname'], 'core_calendar_get_calendar_monthly_view')
            return httpx.Response(200, json=[{'error': False, 'data': {'weeks': []}}])
        rows = await moodle.fetch_month({'mode': 'web', 'cookies': {'MoodleSession': 'test'}, 'sesskey': 'key'},
                                      2026, 11, httpx.MockTransport(upstream))
        self.assertEqual(rows, [])


class MonthApiTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'WEB_SESSION_SECRET': 'offline-calendar'})
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test')

    async def asyncTearDown(self):
        await self.client.aclose(); self.env.stop()

    def session(self, account):
        data = {'mode': 'token', 'token': 'test'} if account == 'moodle' else {'cookies': {'SID': 'test'}}
        token = web_accounts._cipher().encrypt(json.dumps({'account': account, 'session': data}).encode()).decode()
        self.client.cookies.set(web_accounts.PREFIX + account, token, domain='hub.test', path='/api/me')

    async def test_month_contains_all_occurrences_and_true_completion_status(self):
        self.session('mysdu'); self.session('moodle')
        lesson = {'course': 'INF 345', 'title': 'Systems', 'day': 'Mo', 'start': '12:30', 'end': '13:20', 'room': 'G107'}
        due = int(datetime(2026, 11, 2, tzinfo=rooms.CAMPUS_TZ).timestamp())
        task = moodle.Deadline('INF 345', 'Submitted lab', due, completed=True, event_id=12)
        with patch.object(web_accounts.portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[lesson])), patch.object(
                moodle, 'fetch_month', new=AsyncMock(return_value=[task])) as fetch:
            response = await self.client.get('/api/me/month?month=2026-11-15')
        data = response.json()
        self.assertEqual(data['month_start'], '2026-11-01')
        self.assertEqual(data['month_end'], '2026-11-30')
        self.assertEqual(len(data['mysdu']['items']), 5)
        self.assertTrue(data['moodle']['items'][0]['completed'])
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertEqual(fetch.call_args.args[1:], (2026, 11))

    async def test_mysdu_outage_preserves_moodle_and_is_not_an_empty_schedule(self):
        self.session('mysdu'); self.session('moodle')
        with patch.object(web_accounts.portal_schedule, 'fetch_schedule', new=AsyncMock(side_effect=grade.PortalError('unavailable'))), patch.object(
                moodle, 'fetch_month', new=AsyncMock(return_value=[])):
            data = (await self.client.get('/api/me/month?month=2026-11-01')).json()
        self.assertEqual(data['mysdu']['status'], 'unavailable')
        self.assertEqual(data['mysdu']['error'], 'unavailable')
        self.assertEqual(data['moodle']['status'], 'ready')

    async def test_provider_code_required_is_distinct_from_outage(self):
        self.session('mysdu')
        with patch.object(web_accounts, 'portal_classes', new=AsyncMock(side_effect=grade.PortalError('two_factor_required'))):
            data = (await self.client.get('/api/me/month')).json()
        self.assertEqual(data['mysdu']['status'], 'code_required')


class PortalFailureTests(IsolatedAsyncioTestCase):
    async def test_maintenance_page_after_password_never_becomes_connected(self):
        login = '<form action="/login"><input name="student"><input name="password" type="password"></form>'
        def upstream(request):
            return httpx.Response(200, text=login if request.method == 'GET' else '<h1>Temporarily unavailable</h1>',
                                  headers={'set-cookie': 'SID=not-authenticated; Path=/'})
        client = grade.PortalLogin(httpx.MockTransport(upstream))
        try:
            with self.assertRaises(grade.PortalError) as error:
                await client.submit_credentials('123', 'test-password')
            self.assertEqual(error.exception.key, 'unavailable')
        finally:
            await client.close()
