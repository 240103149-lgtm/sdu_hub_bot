"""Reminder boundaries, persistence, private routing and public data integration."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from telegram.error import NetworkError
from telegram.ext import PicklePersistence

import campus_events
import grade
import portal_schedule
import moodle
import notifications
import rooms
import student_schedule
from harness import BotHarness

NOW = datetime(2026, 11, 23, 12, 15, tzinfo=rooms.CAMPUS_TZ)
GROUP = {'id': 'a' * 16, 'course': 'CSS 217', 'title': 'Software Architecture', 'type': 'Theory',
         'section': '01-N', 'sessions': [{'day': 'Mo', 'start': '12:30', 'end': '13:20', 'room': 'G107', 'teacher': 'Lecturer'}]}


def fake_app(data):
    return SimpleNamespace(user_data={42: data}, bot=SimpleNamespace(send_message=AsyncMock()),
                           mark_data_for_update_persistence=Mock())


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_boundary_dedup_and_restart_persistence(self):
        stamp = int(NOW.timestamp())
        items = [moodle.Deadline('CSS 217', 'Assignment', stamp + 43200),
                 moodle.Deadline('CSS 217', 'Later', stamp + 43201),
                 moodle.Deadline('CSS 217', 'Expired', stamp),
                 moodle.Deadline('CSS 217', 'Opens', stamp + 200, kind='open')]
        data = {'lang': 'en', 'moodle': {'mode': 'token', 'token': 'test'}, 'deadline_notifications': True}
        app = fake_app(data)
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=items)) as fetch:
            await notifications.check_user(app, 42, NOW)
            fetch.assert_awaited_once_with(data['moodle'], upcoming_only=True)
            app.bot.send_message.assert_awaited_once()
            self.assertIn('12h 0m', app.bot.send_message.call_args.kwargs['text'])
            self.assertIn('24.11.2026 00:15', app.bot.send_message.call_args.kwargs['text'])
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / 'state.pickle'
                store = PicklePersistence(filepath=path)
                await store.update_user_data(42, data)
                await store.flush()
                restored = await PicklePersistence(filepath=path).get_user_data()
                app2 = fake_app(restored[42])
                await notifications.check_user(app2, 42, NOW)
                app2.bot.send_message.assert_not_awaited()

    async def test_class_15_minute_boundary_room_and_next_week(self):
        app = fake_app({'lang': 'kk', 'schedule_groups': [GROUP['id']], 'class_notifications': True})
        with patch.object(student_schedule, 'groups', return_value=[GROUP]):
            await notifications.check_user(app, 42, NOW - timedelta(seconds=1))
            app.bot.send_message.assert_not_awaited()
            await notifications.check_user(app, 42, NOW)
            await notifications.check_user(app, 42, NOW + timedelta(minutes=2))
            app.bot.send_message.assert_awaited_once()
            self.assertIn('G107', app.bot.send_message.call_args.kwargs['text'])
            self.assertIn('Software Architecture', app.bot.send_message.call_args.kwargs['text'])
            await notifications.check_user(app, 42, NOW + timedelta(days=7))
            self.assertEqual(app.bot.send_message.await_count, 2)

    async def test_no_notifications_after_term_start_of_class_or_opt_out(self):
        with patch.object(student_schedule, 'groups', return_value=[GROUP]):
            for now in [NOW.replace(hour=12, minute=30), NOW.replace(month=12, day=14)]:
                app = fake_app({'schedule_groups': [GROUP['id']], 'class_notifications': True})
                await notifications.check_user(app, 42, now)
                app.bot.send_message.assert_not_awaited()
            app = fake_app({'schedule_groups': [GROUP['id']], 'class_notifications': False})
            with patch.object(moodle, 'fetch_deadlines', new=AsyncMock()) as fetch:
                await notifications.check_user(app, 42, NOW)
                app.bot.send_message.assert_not_awaited()
                fetch.assert_not_awaited()

    async def test_delivery_error_retries_without_marking_sent(self):
        app = fake_app({'schedule_groups': [GROUP['id']], 'class_notifications': True})
        app.bot.send_message.side_effect = [NetworkError('offline'), None]
        with patch.object(student_schedule, 'groups', return_value=[GROUP]):
            await notifications.check_user(app, 42, NOW)
            self.assertFalse(app.user_data[42]['reminders_sent'])
            await notifications.check_user(app, 42, NOW)
            self.assertEqual(app.bot.send_message.await_count, 2)
            self.assertTrue(app.user_data[42]['reminders_sent'])

    async def test_expired_moodle_warns_once_and_keeps_class_reminder(self):
        app = fake_app({'moodle': {'token': 'expired'}, 'deadline_notifications': True,
                        'schedule_groups': [GROUP['id']], 'class_notifications': True})
        with patch.object(student_schedule, 'groups', return_value=[GROUP]), patch.object(
                moodle, 'fetch_deadlines', new=AsyncMock(side_effect=moodle.MoodleError('session_expired'))):
            await notifications.check_user(app, 42, NOW)
            self.assertNotIn('moodle', app.user_data[42])
            self.assertEqual(app.bot.send_message.await_count, 2)
            await notifications.check_user(app, 42, NOW)
            self.assertEqual(app.bot.send_message.await_count, 2)


    async def test_portal_schedule_is_automatic_cached_and_deduplicated(self):
        data = {'portal': {'student_id': '123', 'cookies': {'sid': 'one'}},
                'schedule_groups': [GROUP['id']]}
        app = fake_app(data)
        lesson = {**GROUP['sessions'][0], 'course': 'INF 345', 'title': 'Personal course', 'section': '02-P'}
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[lesson])) as fetch, patch.object(
                student_schedule, 'upcoming') as fallback:
            await notifications.check_user(app, 42, NOW - timedelta(seconds=1))
            app.bot.send_message.assert_not_awaited()
            await notifications.check_user(app, 42, NOW)
            await notifications.check_user(app, 42, NOW + timedelta(minutes=1))
            fetch.assert_awaited_once()
            fallback.assert_not_called()
            app.bot.send_message.assert_awaited_once()
            self.assertIn('Personal course', app.bot.send_message.call_args.kwargs['text'])
            self.assertIn('G107', app.bot.send_message.call_args.kwargs['text'])
            data['class_notifications'] = False
            await notifications.check_user(app, 42, NOW + timedelta(days=7))
            app.bot.send_message.assert_awaited_once()

    async def test_personal_cache_invalidated_on_relink_and_expiry(self):
        data = {'portal': {'cookies': {'sid': 'one'}}}
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[])) as fetch, patch.object(
                notifications.time, 'time', return_value=1000) as clock:
            await notifications.personal_classes(data)
            await notifications.personal_classes(data)
            self.assertEqual(fetch.await_count, 1)
            data['portal'] = {'cookies': {'sid': 'two'}}
            await notifications.personal_classes(data)
            self.assertEqual(fetch.await_count, 2)
            clock.return_value = 1901
            await notifications.personal_classes(data)
            self.assertEqual(fetch.await_count, 3)

    async def test_portal_failure_never_uses_university_sections(self):
        app = fake_app({'portal': {'cookies': {}}, 'schedule_groups': [GROUP['id']]})
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(side_effect=grade.PortalError('session_expired'))), patch.object(
                student_schedule, 'upcoming') as fallback:
            await notifications.check_user(app, 42, NOW)
            fallback.assert_not_called()
            app.bot.send_message.assert_not_awaited()

    async def test_tick_includes_linked_users_without_explicit_preferences(self):
        app = fake_app({'portal': {'cookies': {'sid': 'one'}}})
        app.update_persistence = AsyncMock()
        with patch.object(notifications, 'check_user', new=AsyncMock()) as check:
            await notifications.tick(SimpleNamespace(application=app))
            self.assertEqual(check.await_args.args[1], 42)
        app.update_persistence.assert_awaited_once()


class ReminderRoutingTests(BotHarness):
    async def test_schedule_selection_settings_and_user_isolation(self):
        with patch.object(student_schedule, 'groups', return_value=[GROUP]):
            await self.text('/schedule CSS 217')
            self.assertIn('G107', self.last_text())
            await self.tap('schedule:toggle:' + GROUP['id'])
            self.assertTrue(self.app.user_data[42]['class_notifications'])
            self.assertIn(GROUP['id'], self.app.user_data[42]['schedule_groups'])
            await self.text('/notifications', user_id=43)
            self.assertNotIn('schedule_groups', self.app.user_data[43])
            await self.tap('notify:class')
            self.assertFalse(self.app.user_data[42]['class_notifications'])
            await self.tap('schedule:toggle:' + GROUP['id'])
            self.assertEqual(self.app.user_data[42]['schedule_groups'], [])

    async def test_buttons_edit_existing_card_without_confirmation_messages(self):
        await self.text('/notifications')
        sent = self.send.await_count
        await self.tap('notify:class')
        self.assertEqual(self.send.await_count, sent)
        self.assertTrue(self.app.user_data[42]['class_notifications'])
        keyboard = self.edit.call_args.kwargs['reply_markup']
        self.assertIn('On', keyboard.inline_keyboard[1][0].text)
        self.answer.assert_awaited()
        self.assertTrue(all(not call.kwargs.get('text') for call in self.answer.call_args_list))
        await self.tap('notify:class')
        self.assertEqual(self.send.await_count, sent)
        self.assertFalse(self.app.user_data[42]['class_notifications'])

    async def test_schedule_displays_portal_classes_without_section_selection(self):
        self.app.user_data[42]['portal'] = {'cookies': {'sid': 'one'}}
        lesson = {**GROUP['sessions'][0], 'course': 'INF 345', 'title': 'My course', 'section': '02-P'}
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[lesson])):
            await self.text('/schedule')
        self.assertIn('INF 345', self.last_text())
        self.assertIn('G107', self.last_text())
        self.assertNotIn('schedule_groups', self.app.user_data[42])
        sent = self.send.await_count
        await self.tap('notify:class')
        self.assertFalse(self.app.user_data[42]['class_notifications'])
        self.assertEqual(self.send.await_count, sent)

    async def test_start_deep_link_opens_choice_without_subscribing(self):
        with patch.object(student_schedule, 'groups', return_value=[GROUP]):
            await self.text('/start section_' + GROUP['id'])
        self.assertIn('G107', self.last_text())
        self.assertNotIn('schedule_groups', self.app.user_data[42])

    async def test_reminders_exit_signin_and_group_cannot_change_settings(self):
        await self.text('/deadline')
        await self.tap('deadline:register')
        await self.tap('notify:home')
        self.assertNotIn('login_flow', self.app.user_data[42])
        await self.tap('notify:deadline', user_id=-100, chat_type='group')
        self.assertNotIn('deadline_notifications', self.app.user_data[-100])
        self.ai.assert_not_awaited()


class FeedTests(unittest.TestCase):
    def page(self):
        description = 'Математика 🌀\nSee you at Blue Hall!'
        row = {'id': 303, 'title': 'FIBONACCI DAY', 'date': '2026-11-23T07:30:00Z',
               'hasTime': True, 'location': 'Blue Hall', 'description': '$aa', 'image': 'javascript:alert(1)'}
        payload = '1:' + json.dumps({'events': [row]}) + '\naa:T' + format(len(description.encode()), 'x') + ',' + description
        # Flight chunks may split in the middle of a JSON field or text record.
        chunks = [payload[:40], payload[40:-9], payload[-9:]]
        return ''.join('<script>self.__next_f.push(' + json.dumps([1, chunk]) + ')</script>' for chunk in chunks)

    def test_flight_chunks_unicode_text_record_and_safe_image(self):
        events = campus_events.parse_page(self.page())
        self.assertEqual(len(events), 1)
        self.assertIn('Математика 🌀', events[0]['description'])
        self.assertIsNone(events[0]['image'])
        self.assertEqual(events[0]['venue'], 'Blue Hall')
        self.assertEqual(datetime.fromisoformat(events[0]['date']).astimezone(rooms.CAMPUS_TZ).hour, 12)

    def test_format_change_is_an_error_not_empty_events(self):
        with self.assertRaises(campus_events.FeedError):
            campus_events.parse_page('<html>Login</html>')


class PublicApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_events_schedule_and_safe_config(self):
        import main
        feed = {'events': campus_events.parse_page(FeedTests().page()), 'source': campus_events.SOURCE,
                'fetched_at': 100, 'stale': False}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            with patch.object(campus_events, 'fetch_events', new=AsyncMock(return_value=feed)):
                response = await client.get('/api/events')
                self.assertEqual(response.json()['events'][0]['id'], 303)
            with patch.object(campus_events, 'fetch_events', new=AsyncMock(side_effect=campus_events.FeedError())):
                self.assertEqual((await client.get('/api/events')).status_code, 503)
            result = (await client.get('/api/schedule?course=CSS217')).json()
            self.assertTrue(result['groups'])
            self.assertTrue(all(g['course'] == 'CSS 217' for g in result['groups']))
            self.assertEqual((await client.get('/api/schedule')).json()['groups'], [])
            self.assertEqual((await client.get('/api/schedule?offset=-1')).status_code, 422)
            with patch.dict(os.environ, {'TELEGRAM_PUBLIC_URL': 'https://attacker.example/bot'}):
                self.assertEqual((await client.get('/api/config')).json()['telegram_url'], 'https://t.me/sdu_hub_bot')


class FeedCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_stale_label_and_expiry(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = httpx.Response(200, text=FeedTests().page(), request=httpx.Request('GET', campus_events.SOURCE))
        with patch.dict(os.environ, {'CAMPUS_EVENTS_LIVE': 'on'}), patch.object(campus_events, '_cache', {}), patch.object(
                campus_events.httpx, 'AsyncClient', return_value=client), patch.object(campus_events.time, 'time', return_value=1000) as clock:
            self.assertFalse((await campus_events.fetch_events())['stale'])
            self.assertFalse((await campus_events.fetch_events())['stale'])
            self.assertEqual(client.get.await_count, 1)
            clock.return_value = 1301
            client.get.side_effect = httpx.ReadTimeout('offline')
            self.assertTrue((await campus_events.fetch_events())['stale'])
            clock.return_value = 5001
            with self.assertRaises(campus_events.FeedError):
                await campus_events.fetch_events()
