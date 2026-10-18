"""Acceptance checks for user stories 1-7 (numbers as in the project's main sheet).

Offline: no Telegram, portal, Moodle or AI requests are made.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from harness import BotHarness  # noqa: E402

import core  # noqa: E402
import telegram_bot  # noqa: E402


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


class US2GuideRenderingTests(unittest.TestCase):
    """US2 View course registration guide: what Telegram actually shows."""

    def test_markdown_guide_is_readable_in_telegram(self):
        chunks = telegram_bot.render_chunks(core.load_guide('en').strip())
        joined = '\n'.join(chunks)
        for leftover in ('**', '](', ':----', '\\'):
            self.assertNotIn(leftover, joined)
        self.assertIn('<b>Overview</b>', joined)
        self.assertIn('<a href="https://my.sdu.edu.kz/">my.sdu.edu.kz</a>', joined)
        self.assertIn('<b>Stage · Dates in 2026</b>', joined)
        self.assertIn('• Course Registration — August 10–21', joined)
        self.assertIn('• Add - Drop — September 7–13', joined)
        self.assertIn('• Light green — In basket — The course has been added to your list', joined)
        for chunk in chunks:
            self.assertLessEqual(telegram_bot.tg_len(chunk), telegram_bot.TELEGRAM_LIMIT)
        self.assertEqual(telegram_bot.strip_tags('<b>See</b> <a href="https://x.kz/?a=1&amp;b=2">x.kz</a>'), 'See x.kz')

    def test_text_that_only_looks_like_markdown_stays_safe(self):
        self.assertEqual(telegram_bot.render_line('a | b'), 'a | b')
        self.assertEqual(telegram_bot.render_line('[x](javascript:alert(1))'), '[x](javascript:alert(1))')
        self.assertEqual(telegram_bot.render_line('<script>'), '&lt;script&gt;')
        self.assertEqual(telegram_bot.tables_to_lines('| only a row |\n---'), '| only a row |\n---')


class US2GuideFlowTests(BotHarness):
    async def test_guide_has_the_portal_button(self):
        await self.text('/guide')
        self.assertIn(('🔗 Open the registration portal', 'https://my.sdu.edu.kz/'), self.last_buttons()[0])

    async def test_not_published_guide_is_never_replaced_by_another(self):
        with patch.object(core, 'load_guide', return_value=None):
            await self.text('/guide')
        self.assertEqual(self.last_text(), 'Guide not available for this semester. Please contact the Registrar\'s office.')
        self.assertEqual(self.last_buttons(), [])

    async def test_temporary_failure_offers_retry(self):
        with patch.object(core, 'load_guide', side_effect=core.GuideError('disk busy')):
            await self.text('/guide')
        self.assertEqual(self.last_text(), 'Registration guide is temporarily unavailable. Please try again.')
        self.assertEqual(self.last_buttons(), [[('🔄 Retry', 'guide:retry')]])
        await self.tap('guide:retry')  # the service is back
        self.assertIn('SDU Portal Course Registration Guide', '\n'.join(self.sent_texts()[-3:]))
        self.ai.assert_not_awaited()

    async def test_website_api_separates_missing_and_temporary(self):
        import main
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            with patch.object(core, 'load_guide', return_value=None):
                missing = await client.get('/api/guide?lang=ru')
            with patch.object(core, 'load_guide', side_effect=core.GuideError('x')):
                broken = await client.get('/api/guide?lang=en')
            ok = await client.get('/api/guide?lang=en')
        self.assertEqual(missing.status_code, 404)
        self.assertIn('Инструкция для этого семестра', missing.json()['detail'])
        self.assertEqual(broken.status_code, 503)
        self.assertEqual(ok.status_code, 200)


if __name__ == '__main__':
    unittest.main()
