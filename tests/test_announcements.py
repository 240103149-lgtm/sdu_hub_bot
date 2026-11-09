"""Academic announcements stay separate from the campus events calendar."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from harness import BotHarness

import announcements


class AnnouncementsTests(BotHarness):
    async def test_command_aliases_and_questions_in_three_languages(self):
        with patch.object(announcements, 'answer', new=AsyncMock(return_value='Academic news')) as answer:
            await self.text('/uni_announcments')
            await self.text('/uni_announcements')
            await self.text('SDU-да білім туралы қандай жаңалық бар?')
            await self.text('Какие новости об образовании в СДУ?')
            await self.text('What is the latest academic news at SDU?')
        self.assertEqual([call.args[0] for call in answer.await_args_list], ['en', 'en', 'kk', 'ru', 'en'])
        self.assertIn('Academic news', self.last_text())
        self.ai.assert_not_awaited()

    async def test_start_refreshes_private_menu_and_preserves_events(self):
        await self.text('/start')
        call = self.app.bot.set_my_commands.await_args
        self.assertEqual(call.kwargs['scope'].chat_id, 42)
        self.assertTrue({'events', 'uni_announcments'} <= {item.command for item in call.args[0]})

    async def test_filters_parties_and_survives_social_failure(self):
        now = datetime.now(timezone.utc)
        research = announcements.News('Research grant announced', 'https://sdu.edu.kz/research', now, '', 'SDU website')
        party = announcements.News('Welcome Party for freshmen', 'https://sdu.edu.kz/party', now, 'Education', 'SDU website')
        with patch.object(announcements, 'website_news', new=AsyncMock(return_value=[party, research])), \
             patch.object(announcements, 'social_news', new=AsyncMock(side_effect=TimeoutError)):
            text = await announcements.answer('en')
        self.assertIn(research.url, text)
        self.assertNotIn(party.url, text)
        self.assertIn(announcements.label('limited', 'en'), text)
