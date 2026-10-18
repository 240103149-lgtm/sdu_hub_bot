"""Shared offline harness: real Telegram updates through the real Application.

Telegram, the AI and the documents database are replaced by test doubles, so
no network request is ever made. Used by test_user_stories.py.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, Update, User
from telegram.ext import ExtBot

import deadline_bot
import documents
import grade_bot
import telegram_bot


class BotHarness(unittest.IsolatedAsyncioTestCase):
    LANG = 'en'

    async def asyncSetUp(self):
        grade_bot._CACHE.clear()
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
        self.edit = mocked('edit_message_text', new=AsyncMock(return_value=message))
        self.answer = mocked('answer_callback_query', new=AsyncMock(return_value=True))
        self.delete = mocked('delete_message', new=AsyncMock(return_value=True))
        for name in ('send_chat_action', 'set_my_commands', 'delete_my_commands'):
            mocked(name, new=AsyncMock(return_value=True))
        mocked('get_me', new=AsyncMock(return_value=User(999, 'Hub', True, username='hub_test_bot')))
        for name in ('send_document', 'send_photo', 'edit_message_caption', 'edit_message_reply_markup'):
            mocked(name, new=AsyncMock(return_value=message))
        for p in (
            patch.object(telegram_bot, 'STATE_FILE', Path(self.temp.name) / 'state.pickle'),
            patch.object(documents, 'DB_PATH', Path(self.temp.name) / 'documents.db'),
            patch.dict(documents._stores, clear=True),
        ):
            self.patches.append(p)
            p.start()
        ai = patch.object(telegram_bot.core, 'answer_async', new=AsyncMock(return_value='AI answer'))
        self.patches.append(ai)
        self.ai = ai.start()
        self.app = telegram_bot.build_application()
        message.set_bot(self.app.bot)
        await self.app.initialize()
        self.app.user_data[42]['lang'] = self.LANG
        self.errors = []

        async def error_handler(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(error_handler)
        self.counter = 0

    async def asyncTearDown(self):
        self.assertEqual(self.errors, [])
        await self.app.shutdown()
        for p in reversed(self.patches):
            p.stop()
        deadline_bot.ACTIVE.clear()
        grade_bot.PENDING.clear()
        grade_bot._CACHE.clear()  # transcripts must not leak between tests
        self.temp.cleanup()

    # ---------------------------------------------------------------- input ----

    def user(self, user_id):
        return {'id': user_id, 'is_bot': False, 'first_name': 'Student'}

    def message(self, user_id=42, chat_type='private', **fields):
        return {'message_id': self.counter + 1000, 'date': int(time.time()),
                'chat': {'id': user_id, 'type': chat_type}, 'from': self.user(user_id), **fields}

    async def process(self, payload):
        self.counter += 1
        payload['update_id'] = self.counter
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    async def text(self, text, user_id=42, chat_type='private'):
        fields = {'text': text}
        if text.startswith('/'):
            fields['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
        await self.process({'message': self.message(user_id, chat_type, **fields)})

    async def tap(self, data, user_id=42, chat_type='private'):
        await self.process({'callback_query': {
            'id': str(self.counter), 'from': self.user(user_id), 'chat_instance': 'test', 'data': data,
            'message': self.message(user_id, chat_type, text='menu')}})

    # --------------------------------------------------------------- output ----

    def sent_texts(self):
        return [c.kwargs.get('text', '') for c in self.send.call_args_list]

    def last_text(self):
        return self.sent_texts()[-1]

    def last_buttons(self):
        markup = self.send.call_args.kwargs.get('reply_markup')
        return [[(b.text, b.callback_data or b.url) for b in row] for row in markup.inline_keyboard] if markup else []

    def last_edit(self):
        return self.edit.call_args.kwargs.get('text', '')
