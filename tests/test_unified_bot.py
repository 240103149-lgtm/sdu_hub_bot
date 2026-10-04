"""Offline regression tests: actual Telegram update routing, no live accounts."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram import Update, User, Message, Chat
from telegram.ext import ExtBot

import telegram_bot
import grade_bot
import deadline_bot
import moodle
import rooms
import events_bot


class DataTests(unittest.TestCase):
    def test_events_status_uses_event_date_not_submission_deadline(self):
        source = events_bot.EVENTS_FILE.read_text()
        result = events_bot.render_events(source, 'en', date(2026, 10, 4))
        self.assertEqual(result.count(' · Upcoming'), 3)
        self.assertEqual(result.count(' · Already passed'), 2)
        self.assertIn('ICBCB-2026', result)
        self.assertIn('Application (submission) deadline: September 20', result)
        result = events_bot.render_events(source, 'kk', date(2026, 10, 9))
        self.assertIn(' · Бүгін', result)
        for chunk in telegram_bot.render_chunks(result):
            self.assertLessEqual(telegram_bot.tg_len(chunk), telegram_bot.TELEGRAM_LIMIT)

    def test_long_deadlines_fit_telegram_and_escape_html(self):
        items = [moodle.Deadline('CSS & 206', '<Lab>' * 80, 200 + n, 'https://example.org/?x=' + '&' * 400) for n in range(30)]
        pages = deadline_bot.render_pages(items, 'en', now=100)
        self.assertGreater(len(pages), 1)
        self.assertIn('10 more', pages[-1])
        self.assertEqual(sum(p.count('📌') for p in pages), 20)
        for page in pages:
            self.assertLessEqual(telegram_bot.tg_len(page), 4000)
            self.assertNotIn('<Lab>', page)

    def test_rooms_overlap_and_next_class(self):
        schedule = rooms.parse_schedule([
            {'day': 'Mo', 'start': '08:30', 'end': '09:20', 'room': 'H 201'},
            {'day': 'Mo', 'start': '10:30', 'end': '11:20', 'room': 'H 202'},
            {'day': 'Mo', 'start': '08:30', 'end': '09:20', 'room': 'Online', 'virtual': True},
        ])
        with patch.object(rooms, 'load_schedule', return_value=schedule):
            view = rooms.pick_view('Mo', '08:30')
            free = rooms.free_rooms(view)
            self.assertEqual([x.room.name for x in free], ['H 202'])
            self.assertEqual(free[0].until, 630)
            self.assertEqual(rooms.current_view(datetime(2026, 10, 4, 12, tzinfo=timezone.utc)).status, 'day_off')

    def test_real_schedule_loads(self):
        schedule = rooms.load_schedule()
        self.assertGreater(len(schedule.rooms), 0)
        for day in schedule.days:
            for slot in schedule.slots:
                busy = schedule.busy
                for item in rooms.free_rooms(rooms.View(day, slot)):
                    self.assertFalse(any(c.start < slot.end and slot.start < c.end for c in busy.get((day, rooms.room_key(item.room.name)), [])))


class MoodleTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_login_and_deadline_parsing(self):
        def respond(request):
            if request.url.path.endswith('token.php'):
                return httpx.Response(200, json={'token': 'session-token'})
            return httpx.Response(200, json={'events': [
                {'timesort': 200, 'activityname': 'Lab 3 is due', 'course': {'fullname': 'CSS 206 Database'}},
                {'timesort': 100, 'name': 'Quiz closes', 'course': {'shortname': 'MAT 101'}},
            ]})
        transport = httpx.MockTransport(respond)
        session = await moodle.login('student', 'password', transport)
        self.assertNotIn('password', session)
        deadlines = await moodle.fetch_deadlines(session, transport)
        self.assertEqual([(d.course, d.title, d.due) for d in deadlines], [('MAT 101', 'Quiz', 100), ('CSS 206', 'Lab 3', 200)])

    async def test_invalid_credentials_do_not_retry_web_login(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'errorcode': 'invalidlogin'})
        with self.assertRaises(moodle.MoodleError) as error:
            await moodle.login('student', 'wrong', httpx.MockTransport(respond))
        self.assertEqual(error.exception.key, 'bad_credentials')
        self.assertEqual(len(requests), 1)

    async def test_expired_session(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'errorcode': 'invalidtoken'}))
        with self.assertRaises(moodle.MoodleError) as error:
            await moodle.fetch_deadlines({'mode': 'token', 'token': 'expired'}, transport)
        self.assertEqual(error.exception.key, 'session_expired')


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = []
        def mocked(name, **kwargs):
            p = patch.object(ExtBot, name, **kwargs)
            self.patches.append(p)
            return p.start()
        async def initialize(bot):
            bot._bot_user = User(999, 'Hub', True, username='hub_test_bot')
        mocked('initialize', new=initialize)
        mocked('shutdown', new=AsyncMock())
        message = Message(1, datetime.now(timezone.utc), Chat(42, 'private'), text='reply')
        self.send = mocked('send_message', new=AsyncMock(return_value=message))
        mocked('edit_message_text', new=AsyncMock(return_value=message))
        mocked('answer_callback_query', new=AsyncMock(return_value=True))
        self.delete = mocked('delete_message', new=AsyncMock(return_value=True))
        mocked('send_chat_action', new=AsyncMock(return_value=True))
        self.set_commands = mocked('set_my_commands', new=AsyncMock(return_value=True))
        self.delete_commands = mocked('delete_my_commands', new=AsyncMock(return_value=True))
        mocked('get_me', new=AsyncMock(return_value=User(999, 'Hub', True, username='hub_test_bot')))
        self.keypatch = patch.object(telegram_bot, 'STATE_FILE', Path(self.temp.name) / 'state.pickle')
        self.keypatch.start()
        self.app = telegram_bot.build_application()
        message.set_bot(self.app.bot)
        await self.app.initialize()
        self.app.user_data[42]['lang'] = 'kk'  # Explicit choice for existing Kazakh routing tests.
        self.errors = []
        async def error_handler(update, context):
            self.errors.append(context.error)
        self.app.add_error_handler(error_handler)
        self.counter = 0
        self.ai_patch = patch.object(telegram_bot.core, 'answer_async', new=AsyncMock(return_value='AI answer'))
        self.ai = self.ai_patch.start()

    async def asyncTearDown(self):
        self.assertEqual(self.errors, [])
        await self.app.shutdown()
        self.ai_patch.stop()
        self.keypatch.stop()
        for p in reversed(self.patches):
            p.stop()
        deadline_bot.ACTIVE.clear()
        grade_bot.PENDING.clear()
        self.temp.cleanup()

    async def update(self, text=None, callback=None, user_id=42, chat_type='private'):
        self.counter += 1
        user = {'id': user_id, 'is_bot': False, 'first_name': 'Student', 'language_code': 'kk'}
        message = {'message_id': self.counter, 'date': int(time.time()), 'chat': {'id': user_id, 'type': chat_type}, 'from': user, 'text': text or 'choose'}
        if text and text.startswith('/'):
            message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
        payload = {'update_id': self.counter, 'message': message}
        if callback:
            payload = {'update_id': self.counter, 'callback_query': {'id': str(self.counter), 'from': user, 'chat_instance': 'test', 'data': callback, 'message': message}}
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    async def test_new_users_start_in_english_despite_telegram_locale(self):
        self.app.user_data[42].pop('lang', None)
        await self.update('/start')  # Synthetic Telegram user locale is Kazakh.
        self.assertEqual(self.app.user_data[42]['lang'], 'en')
        self.assertIn('Hi, Student!', self.send.call_args.kwargs['text'])
        self.assertEqual(self.send.call_args.kwargs['reply_markup'].inline_keyboard[0][0].text, '📅 Deadlines')
        await self.update(callback='lang:ru')
        await self.update('/start')
        self.assertIn('Привет, Student!', self.send.call_args.kwargs['text'])
        self.assertEqual(self.app.user_data[42]['lang'], 'ru')
        commands = self.set_commands.call_args.args[0]
        self.assertEqual(commands[0].description, telegram_bot.COMMAND_LABELS['ru'][0][1])
        self.assertEqual(self.set_commands.call_args.kwargs['scope'].chat_id, 42)

    async def test_default_command_menu_is_english_and_old_locale_menus_are_removed(self):
        await telegram_bot._post_init(self.app)
        self.set_commands.assert_awaited_once()
        commands = self.set_commands.call_args.args[0]
        self.assertEqual([(c.command, c.description) for c in commands], telegram_bot.COMMAND_LABELS['en'])
        self.assertEqual([c.kwargs['language_code'] for c in self.delete_commands.call_args_list], ['kk', 'ru'])

    async def test_start_has_localized_overview_and_six_buttons(self):
        for lang, heading in [('kk', '📚 Оқу'), ('ru', '📚 Учёба'), ('en', '📚 Study')]:
            self.app.user_data[42]['lang'] = lang
            await self.update('/start')
            sent = self.send.call_args.kwargs
            self.assertIn('SDU Hub', sent['text'])
            self.assertIn(heading, sent['text'])
            self.assertIn('/deadline', sent['text'])
            self.assertNotIn('/unlink', sent['text'])
            keyboard = sent['reply_markup'].inline_keyboard
            self.assertEqual([len(row) for row in keyboard], [2, 2, 2])
            self.assertEqual([b.callback_data for row in keyboard for b in row],
                             ['menu:deadline', 'menu:grade', 'menu:events', 'menu:rooms', 'menu:guide', 'menu:lang'])
        self.ai.assert_not_awaited()

    async def test_menu_events_rooms_and_guide_work_without_commands(self):
        await self.update(callback='menu:events')
        self.assertTrue(any('Welcome Party' in c.kwargs['text'] for c in self.send.call_args_list))
        await self.update(callback='menu:rooms')
        self.assertIn('Бос кабинеттер', self.send.call_args.kwargs['text'])
        with patch.object(telegram_bot.core, 'load_guide', return_value='Approved registration guide'):
            await self.update(callback='menu:guide')
        self.assertIn('Approved registration guide', self.send.call_args.kwargs['text'])
        self.ai.assert_not_awaited()

    async def test_menu_account_buttons_start_and_switch_login(self):
        await self.update(callback='menu:grade')
        self.assertEqual(self.app.user_data[42]['login_flow'], 'grade')
        await self.update(callback='grade:register')
        await self.update('student-id')
        await self.update(callback='menu:deadline')
        self.assertNotIn('grade_sid', self.app.user_data[42])
        self.assertEqual(self.app.user_data[42]['login_flow'], 'deadline')
        await self.update(callback='deadline:register')
        await self.update('moodle-student')
        with patch.object(moodle, 'login', new=AsyncMock(return_value={'mode': 'token', 'token': 'test'})) as login, patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=[])):
            await self.update('secret-password')
        login.assert_awaited_once_with('moodle-student', 'secret-password')
        self.assertNotIn('login_flow', self.app.user_data[42])
        self.ai.assert_not_awaited()

    async def test_linked_account_buttons_show_existing_data(self):
        self.app.user_data[42]['moodle'] = {'mode': 'token', 'token': 'test'}
        with patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=[])) as fetch:
            await self.update(callback='menu:deadline')
        fetch.assert_awaited_once()
        self.app.user_data[42]['portal'] = {'cookies': {}}
        with patch.object(grade_bot.grade, 'fetch_transcript', new=AsyncMock(return_value=[])) as fetch:
            await self.update(callback='menu:grade')
        fetch.assert_awaited_once()
        self.assertNotIn('login_flow', self.app.user_data[42])
        self.ai.assert_not_awaited()

    async def test_menu_navigation_exits_login_and_refreshes_language(self):
        await self.update(callback='menu:deadline')
        await self.update(callback='deadline:register')
        await self.update(callback='menu:events')
        self.assertNotIn('login_flow', self.app.user_data[42])
        await self.update('ordinary question')
        self.ai.assert_awaited_once()
        await self.update(callback='menu:lang')
        keyboard = self.send.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.callback_data for b in keyboard[0]], ['lang:kk', 'lang:ru', 'lang:en'])
        await self.update(callback='menu:grade')
        await self.update(callback='lang:ru')
        self.assertNotIn('login_flow', self.app.user_data[42])
        self.assertEqual(self.app.user_data[42]['lang'], 'ru')
        self.assertIn('📚 Учёба', self.send.call_args.kwargs['text'])
        self.assertEqual(self.send.call_args.kwargs['reply_markup'].inline_keyboard[0][0].text, '📅 Дедлайны')

    async def test_help_groups_account_controls_and_group_buttons_are_safe(self):
        self.app.user_data[-100]['lang'] = 'kk'
        await self.update('/help')
        body = self.send.call_args.kwargs['text']
        self.assertIn('Әңгіме және аккаунттар', body)
        for name in ['reset', 'cancel', 'unlink', 'unlink_moodle']:
            self.assertIn('/' + name + ' —', body)
        for action in ['grade', 'deadline']:
            await self.update(callback='menu:' + action, user_id=-100, chat_type='group')
            self.assertNotIn('login_flow', self.app.user_data[-100])
            self.assertIn('жеке чатына', self.send.call_args.kwargs['text'])
        self.ai.assert_not_awaited()

    async def test_switch_login_keeps_password_in_moodle_flow(self):
        await self.update('/grade')
        await self.update(callback='grade:register')
        await self.update('240103149')
        await self.update('/deadline')
        # A previous portal keyboard must no longer change the active flow.
        await self.update(callback='grade:register')
        await self.update(callback='deadline:register')
        await self.update('moodle-student')
        with patch.object(moodle, 'login', new=AsyncMock(return_value={'mode': 'token', 'token': 'test'})) as login, patch.object(moodle, 'fetch_deadlines', new=AsyncMock(return_value=[])):
            await self.update('secret-password')
            login.assert_awaited_once_with('moodle-student', 'secret-password')
        self.ai.assert_not_awaited()
        self.delete.assert_awaited()
        self.assertNotIn('grade_sid', self.app.user_data[42])
        self.assertNotIn('deadline_user', self.app.user_data[42])
        self.assertNotIn('secret-password', repr(dict(self.app.user_data[42])))
        await self.update('How to register?')
        self.ai.assert_awaited_once()

    async def test_events_rooms_and_help_exit_signin(self):
        for command in ['/events', '/rooms 14:30', '/room 14:30', '/help']:
            await self.update('/deadline')
            await self.update(callback='deadline:register')
            await self.update(command)
            self.assertNotIn('login_flow', self.app.user_data[42])
            await self.update('ordinary question')
        self.assertEqual(self.ai.await_count, 4)
        for lang in ['kk', 'ru', 'en']:
            names = [name for name, _ in telegram_bot.COMMAND_LABELS[lang]]
            self.assertTrue({'grade', 'deadline', 'rooms', 'events', 'unlink', 'unlink_moodle'}.issubset(names))
            self.assertEqual(len(names), len(set(names)))

    async def test_portal_command_replaces_moodle_login(self):
        await self.update('/deadline')
        await self.update(callback='deadline:register')
        await self.update('moodle-user')
        await self.update('/grade')
        await self.update(callback='grade:register')
        await self.update('240103149')
        self.assertEqual(self.app.user_data[42]['grade_sid'], '240103149')
        self.assertNotIn('deadline_user', self.app.user_data[42])
        await self.update('/cancel')
        self.assertNotIn('grade_sid', self.app.user_data[42])
        self.ai.assert_not_awaited()

    async def test_users_are_isolated_and_group_login_is_rejected(self):
        await self.update('/deadline')
        await self.update(callback='deadline:register')
        await self.update('/events', user_id=43)
        self.assertEqual(self.app.user_data[42]['login_flow'], 'deadline')
        await self.update('/grade', user_id=-100, chat_type='group')
        self.assertNotIn('login_flow', self.app.user_data[-100])
        self.ai.assert_not_awaited()

    async def test_expired_login_does_not_send_password_to_ai(self):
        await self.update('/grade')
        await self.update(callback='grade:register')
        await self.update('student')
        self.app.user_data[42]['login_started'] -= 601
        await self.update('secret')
        self.ai.assert_not_awaited()
        self.assertNotIn('login_flow', self.app.user_data[42])
        await self.update('normal question')
        self.ai.assert_awaited_once()


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_initialization_registers_menu_once(self):
        import main
        fake = AsyncMock()
        with patch.object(main, '_telegram_app', None), patch.object(main, '_telegram_app_lock', asyncio.Lock()), patch.object(telegram_bot, 'build_application', return_value=fake):
            first, second = await asyncio.gather(main._get_telegram_app(), main._get_telegram_app())
            self.assertIs(first, fake)
            self.assertIs(second, fake)
            fake.initialize.assert_awaited_once()
            fake.post_init.assert_awaited_once_with(fake)

    async def test_webhook_health_and_update_dispatch(self):
        import main
        fake = AsyncMock()
        fake.bot = None
        with patch.object(main, 'TELEGRAM_MODE', 'webhook'), patch.object(main, 'WEBHOOK_SECRET', 'test-secret'), patch.object(main, '_get_telegram_app', new=AsyncMock(return_value=fake)), patch.object(main, '_telegram_update_lock', asyncio.Lock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.get('/api/health')
                self.assertEqual(response.status_code, 200)
                response = await client.post('/telegram/webhook', json={'update_id': 1})
                self.assertEqual(response.status_code, 403)
                response = await client.post('/telegram/webhook', json={'update_id': 2}, headers={'X-Telegram-Bot-Api-Secret-Token': 'test-secret'})
                self.assertEqual(response.status_code, 200)
                fake.process_update.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
