"""Acceptance checks for user stories 1-7 (numbers as in the project's main sheet).

Offline: no Telegram, portal, Moodle or AI requests are made.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import core


class US1ChatbotTests(unittest.TestCase):
    """US1 Free-format AI chatbot query."""

    def test_no_answer_within_the_budget_is_a_connection_error(self):
        def slow(contents, system_prompt, deadline=None):
            time.sleep(1.0)
            return SimpleNamespace(text='too late')

        with patch.object(core, 'RESPONSE_BUDGET', 0.2), patch.object(core, 'ask_ai', slow):
            started = time.monotonic()
            with self.assertRaises(core.AssistantError) as error:
                core.answer('How do I get a transcript?', [], 'en')
            waited = time.monotonic() - started
        self.assertEqual(error.exception.key, 'connection')
        self.assertLess(waited, 0.8)  # stopped at the budget, did not wait for the reply
        self.assertTrue(error.exception.localized('en').startswith('Connection error'))

    def test_unreachable_server_is_a_connection_error(self):
        def unreachable(contents, system_prompt, deadline=None):
            raise httpx.ConnectError('no route to host')

        with patch.object(core, 'ask_ai', unreachable):
            with self.assertRaises(core.AssistantError) as error:
                core.answer('Сабақ кестесін қалай құрамын?', [], 'en')
        self.assertEqual(error.exception.key, 'connection')
        self.assertTrue(error.exception.localized('kk').startswith('Қосылым қатесі'))

    def test_fallback_sentence_is_fixed_and_in_the_question_language(self):
        prompts = []

        def fake(contents, system_prompt, deadline=None):
            prompts.append(system_prompt)
            return SimpleNamespace(text='ok')

        with patch.object(core, 'ask_ai', fake):
            self.assertEqual(core.answer('What is the capital of France?', [], 'kk'), 'ok')
            core.answer('Где находится столовая?', [], 'en')
        self.assertIn('"I don\'t have this information, please contact the administration."', prompts[0])
        self.assertIn('"У меня нет этой информации, пожалуйста, обратитесь в администрацию."', prompts[1])
        self.assertIn('Answer ONLY using the knowledge base below', prompts[0])


if __name__ == '__main__':
    unittest.main()
