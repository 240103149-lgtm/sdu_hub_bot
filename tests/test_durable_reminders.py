"""Reminders on Vercel: durable bot state in Redis and the scheduled check.

Redis is an in-memory fake behind httpx.MockTransport that speaks the
Upstash REST protocol, so no network request is made.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import asyncio
import copy
import fnmatch
import json
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import bot_store
import core
import main
import moodle
import notifications
import portal_schedule
import rooms
from harness import BotHarness

NOW = datetime(2026, 11, 23, 12, 15, tzinfo=rooms.CAMPUS_TZ)
PORTAL = {'student_id': '123', 'cookies': {'sid': 'one'}}
LESSON = {'day': 'Mo', 'start': '12:30', 'end': '13:20', 'room': 'G107', 'course': 'CSS 217',
          'title': 'Software Architecture', 'section': '01-N'}


class FakeRedis:
    """The few Redis commands bot_store uses, over the Upstash REST protocol."""

    def __init__(self):
        self.values, self.sets, self.commands, self.down = {}, {}, [], False

    def run(self, command):
        name, *args = command
        name = name.upper()
        self.commands.append(name)
        if name == 'GET':
            return self.values.get(args[0])
        if name == 'SET':
            key, value, *options = args
            if 'NX' in [option.upper() for option in options] and key in self.values:
                return None
            self.values[key] = value
            return 'OK'
        if name == 'MGET':
            return [self.values.get(key) for key in args]
        if name == 'DEL':
            return sum(self.values.pop(key, None) is not None for key in args)
        if name == 'SADD':
            members = self.sets.setdefault(args[0], set())
            before = len(members)
            members.update(args[1:])
            return len(members) - before
        if name == 'SREM':
            members = self.sets.setdefault(args[0], set())
            removed = len(members & set(args[1:]))
            members.difference_update(args[1:])
            return removed
        if name == 'SMEMBERS':
            return sorted(self.sets.get(args[0], set()))
        raise AssertionError(f'unexpected command {name}')

    def handler(self, request):
        if self.down:
            raise httpx.ConnectError('offline')
        assert request.headers['authorization'] == 'Bearer test-token'
        body = json.loads(request.content)
        assert all(isinstance(part, str) for command in (body if request.url.path.endswith('/pipeline') else [body])
                   for part in command)
        if request.url.path.endswith('/pipeline'):
            return httpx.Response(200, json=[{'result': self.run(command)} for command in body])
        return httpx.Response(200, json={'result': self.run(body)})

    def store(self):
        return bot_store.Store('https://redis.test', 'test-token', bot_store._cipher(),
                               transport=httpx.MockTransport(self.handler))


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_are_encrypted_and_round_trip(self):
        redis = FakeRedis()
        store = redis.store()
        data = {'lang': 'kk', 'portal': PORTAL, 'class_notifications': True, 'history': [{'role': 'user', 'text': 'сәлем'}]}
        await store.save_user(42, data)
        sealed = redis.values['sduhub:bot:user:42']
        self.assertNotIn('one', sealed)  # the portal cookie is not readable in Redis
        self.assertNotIn('kk', sealed)
        self.assertEqual(await store.load_user(42), data)
        self.assertEqual(await store.reminder_users(), [42])
        self.assertIsNone(await store.load_user(43))

    async def test_a_value_json_cannot_hold_stays_in_memory_only(self):
        dumped = json.loads(bot_store.dump({'lang': 'en', 'odd': object(), 'tags': {'a'}}))
        self.assertEqual(dumped, {'lang': 'en', 'tags': ['a']})

    async def test_claims_are_atomic_and_releasable(self):
        store = FakeRedis().store()
        self.assertTrue(await store.claim('42:class:x'))
        self.assertFalse(await store.claim('42:class:x'))
        await store.release('42:class:x')
        self.assertTrue(await store.claim('42:class:x'))

    async def test_configuration_names_and_https_only(self):
        clean = {name: '' for pair in bot_store.ENV_PAIRS for name in pair}
        with patch.dict(os.environ, {**clean, 'KV_REST_API_URL': 'https://kv.test/', 'KV_REST_API_TOKEN': 't'}):
            self.assertEqual(bot_store.redis_config(), ('https://kv.test', 't'))
            self.assertIsNotNone(bot_store.current())
        with patch.dict(os.environ, {**clean, 'UPSTASH_REDIS_REST_URL': 'http://plain.test', 'UPSTASH_REDIS_REST_TOKEN': 't'}):
            self.assertIsNone(bot_store.redis_config())
            self.assertIsNone(bot_store.current())
        with patch.dict(os.environ, clean):
            self.assertIsNone(bot_store.current())

    async def test_new_secret_still_reads_records_sealed_with_the_bot_token(self):
        redis = FakeRedis()
        with patch.dict(os.environ, {'WEB_SESSION_SECRET': ''}):
            await redis.store().save_user(42, {'lang': 'ru'})
        with patch.dict(os.environ, {'WEB_SESSION_SECRET': 'a-new-secret'}):
            self.assertEqual(await redis.store().load_user(42), {'lang': 'ru'})


class DurableWebhookTests(BotHarness):
    """Updates through POST /telegram/webhook, as Telegram sends them to Vercel."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.redis = FakeRedis()
        self.store = self.redis.store()
        for p in (patch.object(bot_store, 'current', return_value=self.store),
                  patch.object(main, 'TELEGRAM_MODE', 'webhook'), patch.object(main, 'WEBHOOK_SECRET', ''),
                  patch.object(main, '_telegram_app', self.app),
                  patch.dict(os.environ, {'VERCEL': '1', 'CRON_SECRET': ''})):
            self.patches.append(p)
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def process(self, payload):
        self.counter += 1
        payload['update_id'] = self.counter
        response = await self.client.post('/telegram/webhook', json=payload)
        self.assertEqual(response.status_code, 200)

    def cold_start(self):
        """A new Vercel instance: nothing in memory."""
        for data in self.app.user_data.values():
            data.clear()

    async def cron(self, **headers):
        self.redis.values.pop('sduhub:bot:tick', None)  # as if 50 seconds had passed
        return await self.client.post('/api/cron/reminders', headers=headers)

    async def test_settings_survive_a_cold_start_and_the_card_says_paused_until_checks_run(self):
        await self.text('/notify')
        self.assertIn('paused', self.last_text())  # no scheduled check has run yet
        await self.tap('notify:deadline')
        self.assertTrue(self.app.user_data[42]['deadline_notifications'])
        self.cold_start()
        await self.text('/notify')
        self.assertTrue(self.app.user_data[42]['deadline_notifications'])
        self.assertIn('Deadlines · 12 hours · On', str(self.last_buttons()))
        self.assertEqual(await self.store.reminder_users(), [42])
        await self.cron()
        await self.text('/notify')
        self.assertNotIn('paused', self.last_text())

    async def test_scheduled_check_sends_once_across_instances_and_runs(self):
        self.app.user_data[42].update(portal=PORTAL, class_notifications=True)
        await self.text('/notify')  # saved to Redis with this update
        self.cold_start()
        with patch.object(portal_schedule, 'fetch_schedule', new=AsyncMock(return_value=[LESSON])) as fetch, patch.object(
                notifications, 'datetime') as clock, patch.object(main, 'datetime') as main_clock:
            clock.now.return_value = main_clock.now.return_value = NOW
            clock.fromtimestamp = datetime.fromtimestamp
            sent = self.send.await_count
            result = (await self.cron()).json()
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['students'], 1)
            self.assertEqual(self.send.await_count, sent + 1)
            self.assertIn('Software Architecture', self.last_text())
            self.assertIn('G107', self.last_text())
            self.assertNotIn('Software', json.dumps(result))  # counts only, never student data
            await self.cron()  # the next run, maybe on another instance
            # An "enable" click on an instance that never saw this reminder.
            self.app.user_data[42].update(await self.store.load_user(42))
            self.app.user_data[42].pop('reminders_sent')
            await notifications.check_now(self.app, 42)
            self.assertEqual(self.send.await_count, sent + 1)
            fetch.assert_awaited_once()  # the timetable cache was saved with the record
        self.assertNotIn('class:', ' '.join(self.redis.values))  # claim names are hashed

    async def test_second_trigger_within_50_seconds_does_nothing(self):
        first = await self.cron()
        self.assertEqual(first.json()['status'], 'ok')
        second = await self.client.post('/api/cron/reminders')
        self.assertEqual(second.json(), {'status': 'skipped', 'reason': 'ran_recently'})

    async def test_cron_secret_is_required_when_set(self):
        with patch.dict(os.environ, {'CRON_SECRET': 'correct-horse'}):
            self.assertEqual((await self.cron()).status_code, 401)
            self.assertEqual((await self.cron(Authorization='Bearer wrong')).status_code, 401)
            self.assertEqual((await self.cron(Authorization='Bearer correct-horse')).json()['status'], 'ok')

    async def test_expired_session_relinked_during_the_check_is_kept(self):
        self.app.user_data[42].update(moodle={'mode': 'token', 'token': 'old'}, deadline_notifications=True)
        await self.text('/notify')

        async def expired_and_relinked(session, **kwargs):
            # The student links Moodle again while the check is running.
            record = await self.store.load_user(42)
            record['moodle'] = {'mode': 'token', 'token': 'new'}
            await self.store.save_user(42, record)
            raise moodle.MoodleError('session_expired')

        with patch.object(moodle, 'fetch_deadlines', new=expired_and_relinked):
            await self.cron()
        self.assertEqual((await self.store.load_user(42))['moodle'], {'mode': 'token', 'token': 'new'})
        self.assertIn('Moodle session expired', self.last_text())

    async def test_unsubscribed_and_blocked_students_leave_the_index(self):
        self.app.user_data[42].update(deadline_notifications=True, moodle={'mode': 'token', 'token': 't'})
        await self.text('/notify')
        self.assertEqual(await self.store.reminder_users(), [42])
        await self.tap('notify:deadline')  # off
        self.assertEqual(await self.store.reminder_users(), [])
        await self.tap('notify:deadline')  # on again
        record = await self.store.load_user(42)
        record['reminders_blocked'] = True
        await self.store.save_user(42, record)
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock()) as fetch:
            await self.cron()
        fetch.assert_not_awaited()
        self.assertEqual(await self.store.reminder_users(), [])

    async def test_redis_outage_never_overwrites_the_stored_record(self):
        self.app.user_data[42]['deadline_notifications'] = True
        await self.text('/notify')
        self.redis.down = True
        self.cold_start()
        await self.text('/lang')  # still answered from memory
        self.redis.down = False
        self.assertTrue((await self.store.load_user(42))['deadline_notifications'])
        self.redis.down = True
        response = await self.cron()
        self.assertEqual(response.status_code, 503)

    async def test_password_after_a_lost_signin_step_never_reaches_the_ai(self):
        await self.text('/grade')
        await self.tap('grade:register')
        await self.text('240103149')  # the student number; the password is asked next
        # A new instance: the saved data comes from Redis, the conversation step does not.
        self.cold_start()
        for handler in self.app.handlers[0]:
            if hasattr(handler, '_conversations'):
                handler._conversations.clear()
        await self.text('Secret123!')
        self.ai.assert_not_awaited()
        self.delete.assert_awaited()
        self.assertIn('password', self.last_text())
        self.assertNotIn('login_flow', self.app.user_data[42])
        self.assertNotIn('Secret123!', json.dumps(dict(self.app.user_data[42]), ensure_ascii=False))
        await self.text('What is the GPA scale?')  # a question still reaches the AI
        self.ai.assert_awaited_once()

    async def test_without_redis_the_check_is_skipped_and_the_card_says_paused(self):
        with patch.object(bot_store, 'current', return_value=None):
            response = await self.cron()
            self.assertEqual(response.json(), {'status': 'skipped', 'reason': 'storage_not_configured'})
            await self.text('/notify')
            self.assertIn('paused', self.last_text())
            with patch.dict(os.environ, {'VERCEL': ''}):
                await self.text('/notify')  # a persistent host runs the minute job itself
                self.assertNotIn('paused', self.last_text())


class MergeTests(unittest.TestCase):
    def test_results_move_over_only_for_the_same_session(self):
        before = {'portal': PORTAL, 'moodle': {'token': 'a'}, 'reminders_sent': {'old': 1}}
        after = {'reminders_sent': {'new': 2}, 'personal_schedule_cache': {'session': 's', 'items': []}}
        latest = copy.deepcopy(before) | {'lang': 'ru', 'moodle': {'token': 'b'}}
        self.assertTrue(notifications._merge(latest, before, after))
        self.assertNotIn('portal', latest)             # expired during the check
        self.assertEqual(latest['moodle'], {'token': 'b'})  # relinked meanwhile: kept
        self.assertEqual(latest['reminders_sent'], {'new': 2})
        self.assertEqual(latest['lang'], 'ru')         # the student's own change stays
        self.assertNotIn('personal_schedule_cache', latest)


class DeploymentTests(unittest.TestCase):
    def test_every_module_is_in_the_vercel_bundle(self):
        root = Path(core.BASE_DIR)
        included = json.loads((root / 'vercel.json').read_text())['builds'][0]['config']['includeFiles']
        missing = [path.name for path in root.glob('*.py')
                   if path.name not in ('bot.py', 'selftest.py')
                   and not any(fnmatch.fnmatch(path.name, pattern) for pattern in included)]
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
