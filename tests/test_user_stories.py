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
import rooms  # noqa: E402
import telegram_bot  # noqa: E402

from datetime import date, datetime, timedelta, timezone  # noqa: E402


def next_weekday(weekday):
    """The next date (after today, campus time) that falls on `weekday` (0 = Monday)."""
    day = datetime.now(rooms.CAMPUS_TZ).date() + timedelta(days=1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


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


class US3IntervalTests(unittest.TestCase):
    """US3 Find available classrooms: a date, a start time and an end time."""

    NOW = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)  # Sunday, 15:00 in Almaty

    def test_search_formats_and_invalid_input(self):
        tuesday = date(2026, 10, 20)
        for text in ('20.10 14:00-16:00', '2026-10-20 14:00 16:00', '20.10.2026 14:00 – 16:00'):
            self.assertEqual(rooms.parse_search(text, self.NOW), rooms.Search(tuesday, 840, 960), text)
        self.assertEqual(rooms.parse_search('14:00-16:00', self.NOW).date, date(2026, 10, 18))
        for text in ('', '14:30'):  # not an interval: the class-period view, as before
            self.assertIsNone(rooms.parse_search(text, self.NOW))
        for text, key in (('20.10 16:00-14:00', 'invalid'), ('20.10 14:00-14:00', 'invalid'),
                          ('31.02 10:00-11:00', 'invalid'), ('20.10 14:00', 'invalid'), ('17.10 14:00-16:00', 'past')):
            with self.assertRaises(rooms.SearchError, msg=text) as error:
                rooms.parse_search(text, self.NOW)
            self.assertEqual(error.exception.key, key)
        search = rooms.Search(tuesday, 840, 960)
        self.assertEqual(rooms.search_from_code(search.code()), search)

    def test_only_rooms_free_for_the_whole_interval(self):
        schedule = rooms.parse_schedule([
            {'day': 'Tu', 'start': '12:30', 'end': '14:00', 'room': 'A 101', 'building': 'Main'},    # ends at start
            {'day': 'Tu', 'start': '16:00', 'end': '17:00', 'room': 'A 102', 'building': 'Main'},    # starts at end
            {'day': 'Tu', 'start': '15:30', 'end': '16:20', 'room': 'A 103', 'building': 'Main'},    # overlaps the end
            {'day': 'Tu', 'start': '13:30', 'end': '14:20', 'room': 'B 201', 'building': 'Law'},     # overlaps the start
            {'day': 'Tu', 'start': '08:30', 'end': '22:20', 'room': 'C 301', 'building': 'Main'},
            {'day': 'Mo', 'start': '14:30', 'end': '15:20', 'room': 'B 202', 'building': 'Law'},     # another day
        ])
        with patch.object(rooms, 'load_schedule', return_value=schedule):
            free = rooms.free_rooms_between('Tu', 840, 960)
            self.assertEqual([item.room.name for item in free], ['A 101', 'A 102', 'B 202'])
            self.assertEqual({item.room.name: item.until for item in free}, {'A 101': None, 'A 102': 960, 'B 202': None})
            rooms.check_search(rooms.Search(date(2026, 10, 20), 840, 960))
            for search, key in ((rooms.Search(date(2026, 10, 25), 840, 960), 'no_classes'),
                                (rooms.Search(date(2026, 10, 20), 420, 540), 'hours')):
                with self.assertRaises(rooms.SearchError) as error:
                    rooms.check_search(search)
                self.assertEqual(error.exception.key, key)


class US3RoomsFlowTests(BotHarness):
    async def test_interval_result_lists_building_label_interval_and_data_date(self):
        tuesday = next_weekday(1)
        await self.text(f'/rooms {tuesday:%d.%m} 14:00-16:00')
        body = self.last_text()
        self.assertIn(f'Tuesday, {tuesday:%d.%m.%Y}, 14:00–16:00', body)
        self.assertIn('✅ Available according to schedule', body)
        self.assertIn('🏢 Main building - Engineering block', body)
        self.assertIn('Schedule data as of 04.10.2026', body)
        schedule = rooms.load_schedule()
        for name in [line for line in body.split('\n') if line.startswith('<b>')]:
            for room in name.split('— ', 1)[1].split(', '):
                key = rooms.room_key(room.split(' (')[0])
                busy = schedule.busy.get(('Tu', key), [])
                self.assertFalse(any(c.start < 960 and 840 < c.end for c in busy), room)

    async def test_invalid_search_is_not_submitted(self):
        await self.text('/rooms 20.10 16:00-14:00')
        self.assertTrue(self.last_text().startswith('Select a date and an end time later than the start time.'))
        await self.text(f'/rooms {next_weekday(6):%d.%m} 14:00-16:00')
        self.assertIn('There are no classes in the schedule on', self.last_text())

    async def test_no_rooms_and_loading_failure_are_different(self):
        tuesday = next_weekday(1)
        with patch.object(rooms, 'free_rooms_between', return_value=[]):
            await self.text(f'/rooms {tuesday:%d.%m} 14:00-16:00')
        self.assertIn('No classrooms available for the selected time.', self.last_text())
        self.assertNotIn('temporarily unavailable', self.last_text())

        with patch.object(rooms, 'load_schedule', side_effect=rooms.ScheduleError('missing')):
            await self.text(f'/rooms {tuesday:%d.%m} 14:00-16:00')
        self.assertEqual(self.last_text(), 'Classroom availability is temporarily unavailable. Please try again.')
        retry = self.last_buttons()[0][0]
        self.assertEqual(retry, ('🔄 Retry', f'roomsq:{tuesday:%Y%m%d}:1400:1600'))
        await self.tap(retry[1])  # the data is back: same date and interval
        self.assertIn(f'{tuesday:%d.%m.%Y}, 14:00–16:00', self.last_edit())

        with patch.object(rooms, 'load_schedule', side_effect=rooms.ScheduleError('missing')):
            await self.text('/rooms')
        self.assertEqual(self.last_buttons(), [[('🔄 Retry', 'rooms:now')]])


SAMPLE_EVENTS = """# Events
## 1. Late concert
- Date: November 20, 2026
- Venue: Blue Hall
## 2. Early lecture
- Date: November 20, 2026
- Time: 09:00
## 3. Cancelled fair
- Date: November 5, 2026
- Time: 12:00
- Venue: Main hall
- Status: Cancelled
## 4. Moved workshop
- Date: November 1, 2026
- Status: Postponed
- New date: December 3, 2026
## 5. Moved talk
- Date: November 2, 2026
- Status: Postponed
## 6. Secret draft
- Date: November 10, 2026
- Status: Draft
"""


class US4EventsTests(unittest.TestCase):
    """US4 View campus events."""

    def test_order_statuses_and_missing_details(self):
        import events_bot
        events = events_bot.parse_events(SAMPLE_EVENTS)
        # By start date and time; a postponed event starts on its new date; no date yet -> last.
        self.assertEqual([e.title for e in events],
                         ['Cancelled fair', 'Early lecture', 'Late concert', 'Moved workshop', 'Moved talk'])
        text = events_bot.render_events(SAMPLE_EVENTS, 'en', date(2026, 10, 18))
        self.assertNotIn('Secret draft', text)
        self.assertIn('Cancelled fair** · ❌ Cancelled', text)
        self.assertIn('Moved workshop** · ⏸ Postponed, new date: 03.12.2026', text)
        self.assertIn('Moved talk** · ⏸ Postponed, New date to be confirmed', text)
        late = next(e for e in events if e.title == 'Late concert')
        self.assertIn('🕒 To be confirmed · 📍 Blue Hall', events_bot.render_details(late, 'en', date(2026, 10, 18)))
        early = next(e for e in events if e.title == 'Early lecture')
        self.assertIn('🕒 09:00 · 📍 To be confirmed', events_bot.render_details(early, 'en', date(2026, 10, 18)))

    def test_range_is_inclusive_and_uses_the_new_date_of_a_postponed_event(self):
        import events_bot
        text = events_bot.render_events(SAMPLE_EVENTS, 'en', date(2026, 10, 18), date(2026, 11, 5), date(2026, 11, 20))
        for title in ('Cancelled fair', 'Early lecture', 'Late concert'):
            self.assertIn(title, text)
        for title in ('Moved workshop', 'Moved talk', 'Secret draft'):
            self.assertNotIn(title, text)
        self.assertIn('Moved workshop', events_bot.render_events(SAMPLE_EVENTS, 'en', date(2026, 10, 18),
                                                                 date(2026, 12, 3), date(2026, 12, 3)))
        self.assertEqual(events_bot.parse_range('05.11-20.11', date(2026, 10, 18)), (date(2026, 11, 5), date(2026, 11, 20)))
        self.assertEqual(events_bot.parse_range('2026-11-05', date(2026, 10, 18)), (date(2026, 11, 5), date(2026, 11, 5)))
        self.assertIsNone(events_bot.parse_range('', date(2026, 10, 18)))
        for bad in ('20.11-05.11', '31.02-01.03', 'soon'):
            with self.assertRaises(ValueError, msg=bad):
                events_bot.parse_range(bad, date(2026, 10, 18))

    def test_real_events_file(self):
        import events_bot
        events = events_bot.parse_events(events_bot.EVENTS_FILE.read_text(encoding='utf-8'))
        self.assertEqual([e.date for e in events], sorted(e.date for e in events))
        icbcb = next(e for e in events if 'ICBCB' in e.title)
        self.assertIsNone(icbcb.time)  # not published: shown as "To be confirmed"
        party = next(e for e in events if e.title.startswith('International Students'))
        self.assertEqual((party.time, party.venue), ('17:30', 'program'))


class US4EventsFlowTests(BotHarness):
    async def test_range_details_and_messages(self):
        import events_bot
        await self.text('/events 09.10-15.10')
        body = self.last_text()
        self.assertIn('Selected dates: 09.10.2026 – 15.10.2026', body)
        self.assertIn('Welcome Party 2026', body)
        self.assertIn('ICBCB-2026', body)
        self.assertNotIn('Transformation Seminar', body)
        self.assertNotIn('SDU FEST', body)
        buttons = self.last_buttons()[0]
        self.assertEqual([label for label, _ in buttons], ['ℹ️ 1', 'ℹ️ 2'])
        await self.tap(buttons[1][1])
        details = self.last_text()
        self.assertIn('Application (submission) deadline: September 20, 2026', details)
        self.assertIn('does not register you', details)

        await self.text('/events 15.10-09.10')
        self.assertTrue(self.last_text().startswith('Select a start date and an end date on or after the start date.'))
        await self.text('/events 01.01.2027-31.01.2027')
        self.assertIn('No campus events found for the selected dates.', self.last_text())

        with patch.object(events_bot, 'EVENTS_FILE', Path(self.temp.name)):  # a folder: exists, can't be read
            await self.text('/events 09.10-15.10')
        self.assertEqual(self.last_text(), 'Campus events are temporarily unavailable. Please try again.')
        self.assertNotIn('No campus events found', self.last_text())
        retry = self.last_buttons()[0][0]
        self.assertEqual(retry[1], 'events:retry:20261009-20261015')
        await self.tap(retry[1])  # the source is back; the chosen dates are kept
        self.assertIn('Selected dates: 09.10.2026 – 15.10.2026', self.last_text())

        with patch.object(events_bot, 'EVENTS_FILE', Path(self.temp.name) / 'missing.md'):
            await self.text('/events')
        self.assertEqual(self.last_text(), 'Event information has not been published yet.')
        self.ai.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
