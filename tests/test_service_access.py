"""Assistant access to real service facts; all upstreams and model calls are offline."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import json
from datetime import datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx
import assistant
import campus_events
import grade
import main
import moodle
import portal_schedule
import rooms
import service_data
import web_accounts
import web_assistant
from test_assistant import ScriptedModel, call, say
from harness import BotHarness

EVENT = {'id': 303, 'title': 'FIBONACCI DAY', 'date': '2026-11-23T07:30:00+00:00',
         'has_time': True, 'venue': 'Blue Hall', 'organizer': 'Math Club',
         'description': 'Math games and a workshop. Register with the Math Club at the entrance.',
         'categories': ['Science'], 'url': 'https://life.sdu.edu.kz/en/events/303'}
FEED = {'events': [EVENT], 'source': campus_events.SOURCE, 'stale': False, 'fetched_at': 100}
LESSON = {'course': 'INF 345', 'title': 'System Administration', 'day': 'Mo', 'start': '12:30',
          'end': '13:20', 'room': 'G107', 'section': '01-N', 'source': 'mysdu'}


class TelegramServiceFactsTests(BotHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ai_patcher.stop()
        self.patches.remove(self.ai_patcher)
        self.model = ScriptedModel()
        model = patch.object(assistant, 'new_chat', self.model.new_chat)
        self.patches.append(model)
        model.start()

    def script(self, *turns):
        self.model.turns += list(turns)

    def all_sent(self):
        return '\n'.join(self.sent_texts())

    async def test_live_event_description_organizer_and_followup_reach_the_model(self):
        self.script(call('get_campus_events', query='FIBONACCI'), say('Math Club organizes it in Blue Hall.'),
                    call('get_campus_events', event_id=303), say('Register at the entrance.'))
        with patch.object(campus_events, 'fetch_events', new=AsyncMock(return_value=FEED)):
            await self.text('Tell me about the Fibonacci event')
            data = self.model.results[-1][0][1]['events'][0]
            self.assertEqual(data['organizer'], 'Math Club')
            self.assertIn('at the entrance', data['description'])
            self.assertTrue(data['date'].endswith('+05:00'))
            await self.text('How do I register for it?')
        self.assertIn('at the entrance', self.last_text() + self.all_sent())
        self.assertIn('303 FIBONACCI', self.model.chats[-1]['history'][-1][1])

    async def test_personal_timetable_fields_reach_model_without_account_secrets(self):
        self.app.user_data[42]['portal'] = {'student_id': '123', 'cookies': {'SID': 'secret-cookie'}}
        self.script(call('get_my_schedule'), say('INF 345 starts at 12:30 in G107.'))
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[LESSON])):
            await self.text('Where is my next class?')
        data = self.model.results[-1][0][1]
        self.assertEqual(data['classes'][0]['room'], 'G107')
        self.assertNotIn('secret-cookie', self.model.everything_seen())

    async def test_new_canteen_and_account_tools_are_executable(self):
        self.script(call('get_canteen_menu', query='tea'), say('The menu lists tea prices.'),
                    call('get_account_status'), say('Your accounts are not connected.'))
        await self.text('What tea is in the canteen menu?')
        self.assertTrue(self.model.results[-1][0][1]['items'])
        await self.text('Which of my accounts are connected?')
        self.assertFalse(self.model.results[-1][0][1]['mysdu_connected'])


class WebsiteServiceTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {'WEB_SESSION_SECRET': 'offline-web-service-tests'})
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test')
        self.headers = {'X-SDU-Hub': '1', 'Origin': 'https://hub.test'}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()

    def session(self, account, session):
        token = web_accounts._cipher().encrypt(json.dumps({'account': account, 'session': session}).encode()).decode()
        self.client.cookies.set(web_accounts.PREFIX + account, token, domain='hub.test', path='/api/me')
        return token

    async def ask(self, model, message, path='/api/me/chat', headers=None):
        with patch.object(assistant, 'new_chat', model.new_chat):
            response = await self.client.post(path, json={'message': message, 'lang': 'en'},
                                              headers=headers if headers is not None else self.headers)
        self.assertEqual(response.status_code, 200)
        return response

    async def test_website_chat_fetches_live_event_details_instead_of_static_only_answers(self):
        model = ScriptedModel(call('get_campus_events', query='FIBONACCI'), say('Math Club runs a workshop in Blue Hall.'))
        with patch.object(campus_events, 'fetch_events', new=AsyncMock(return_value=FEED)) as fetch:
            response = await self.ask(model, 'Who runs the Fibonacci event?')
        self.assertIn('Blue Hall', response.json()['reply'])
        self.assertIn('no-store', response.headers['cache-control'])
        fetch.assert_awaited_once()
        facts = model.results[-1][0][1]
        self.assertNotIn(assistant.SHOWN_NOTE, facts.values())
        self.assertIn('workshop', facts['events'][0]['description'])
        self.assertIn('NOT displayed as separate cards', model.chats[0]['system'])

    async def test_website_read_access_is_limited_to_the_current_encrypted_account(self):
        token = self.session('mysdu', {'cookies': {'SID': 'secret-session'}})
        model = ScriptedModel(call('get_my_schedule'), say('Your class is in G107.'))
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[LESSON])) as fetch:
            await self.ask(model, 'Where is my next class?')
        fetch.assert_awaited_once_with({'SID': 'secret-session'})
        seen = model.everything_seen()
        self.assertIn('G107', seen)
        self.assertNotIn('secret-session', seen)
        self.assertNotIn(token, seen)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test') as other:
            self.client, original = other, self.client
            try:
                model2 = ScriptedModel(call('get_my_schedule'))
                with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock()) as fetch2:
                    result = await self.ask(model2, 'Where is my next class?')
                self.assertEqual(result.json()['sign_in'], 'mysdu')
                fetch2.assert_not_awaited()
            finally:
                self.client = original

    async def test_public_chat_never_uses_private_sessions(self):
        self.session('moodle', {'token': 'secret-token'})
        model = ScriptedModel(call('get_my_deadlines'))
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock()) as fetch:
            result = await self.ask(model, 'My deadlines?', path='/api/chat')
        self.assertEqual(result.json()['sign_in'], 'moodle')
        fetch.assert_not_awaited()

    async def test_private_chat_requires_same_origin_and_header(self):
        for headers in ({}, {'X-SDU-Hub': '1', 'Origin': 'https://other.test'}):
            response = await self.client.post('/api/me/chat', json={'message': 'My grades?'}, headers=headers)
            self.assertEqual(response.status_code, 403)

    async def test_moodle_deadlines_and_grades_are_available_without_exposing_credentials(self):
        self.session('moodle', {'token': 'private-token'})
        model = ScriptedModel(call('get_my_deadlines'), say('Homework is due tomorrow.'))
        item = moodle.Deadline('INF 345', 'Homework', int(datetime.now(rooms.CAMPUS_TZ).timestamp()) + 1000)
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=[item])):
            await self.ask(model, 'My next deadline?')
        self.assertIn('Homework', model.everything_seen())
        self.assertNotIn('private-token', model.everything_seen())
        self.session('mysdu', {'cookies': {'SID': 'test'}})
        transcript = [grade.Semester('Fall', courses=[grade.Course('INF 345', 'System Administration', '3', '5', '', 'IP', '', '')])]
        model = ScriptedModel(call('get_my_grades'), say('Your final grade is not published.'))
        with patch.object(grade, 'fetch_transcript', new=AsyncMock(return_value=transcript)):
            await self.ask(model, 'My grades?')
        self.assertEqual(model.results[-1][0][1]['semesters'][0]['courses'][0]['grade'], 'not published')

    async def test_expired_session_clears_cookie_and_offers_secure_signin(self):
        self.session('mysdu', {'cookies': {'SID': 'expired'}})
        model = ScriptedModel(call('get_my_schedule'))
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(side_effect=grade.PortalError('session_expired'))):
            response = await self.ask(model, 'Show my timetable')
        self.assertEqual(response.json()['sign_in'], 'mysdu')
        self.assertIn('Max-Age=0', response.headers['set-cookie'])
        self.assertNotIn('expired', model.everything_seen())

    async def test_live_feed_failure_is_availability_error_not_no_access(self):
        model = ScriptedModel(call('get_campus_events'), say('SDU Life is temporarily unavailable.'))
        with patch.object(campus_events, 'fetch_events', new=AsyncMock(side_effect=campus_events.FeedError())):
            result = await self.ask(model, 'What events are coming up?')
        self.assertIn('unavailable', model.results[-1][0][1]['error'])
        self.assertIn('temporarily unavailable', result.json()['reply'])

    async def test_written_tool_call_is_executed_and_then_answered_without_raw_tags(self):
        model = ScriptedModel(say('<tool_call>{"name":"get_campus_events","arguments":{"query":"FIBONACCI"}}</tool_call>'),
                              say('Math Club organizes the event.'))
        with patch.object(campus_events, 'fetch_events', new=AsyncMock(return_value=FEED)):
            result = await self.ask(model, 'Who organizes the Fibonacci event?')
        self.assertEqual(result.json()['reply'], 'Math Club organizes the event.')
        self.assertEqual(model.results[-1][0][1]['events'][0]['organizer'], 'Math Club')

    async def test_event_pagination_and_named_past_event(self):
        past = {**EVENT, 'id': 1, 'date': '2020-01-01T10:00:00+00:00'}
        data = service_data.event_facts({**FEED, 'events': [past]}, {'query': 'FIBONACCI'})
        self.assertEqual(data['total'], 1)
        self.assertEqual(service_data.event_facts({**FEED, 'events': [past]}, {})['total'], 0)
        many = [{**EVENT, 'id': number, 'date': '2099-01-01T10:00:00+00:00'} for number in range(20)]
        first = service_data.event_facts({**FEED, 'events': many}, {})
        second = service_data.event_facts({**FEED, 'events': many}, {'offset': first['next_offset']})
        self.assertEqual(len(first['events']) + len(second['events']), 20)
        self.assertIsNone(second['next_offset'])
