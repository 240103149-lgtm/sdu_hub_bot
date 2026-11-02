"""US13 Academic calendar: the portal's System Calendar (academic_calendar.py).

Offline. The HTML below copies the markup of my.sdu.edu.kz/index.php?mod=calendar
and of its two AJAX answers (2026 - 2027 Fall); the student number in the
permissions table is a made-up one.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import asyncio
import json
import unittest
from datetime import datetime
from urllib.parse import parse_qs

import httpx

import academic_calendar
from academic_calendar import Event
from grade import PortalError

PAGE = '''<html><body><table><tr><td>System Calendar</td></tr></table>
Year and term: <select id="ysem" class="clsInput">
<option value="20261" selected>2026 - 2027 Fall</option>
<option value="20253">2025 - 2026 Summer</option>
<option value="20252">2025 - 2026 Spring</option>
</select>
<div id="divShowSystemCalendar"></div><div id="divShowPermissions"></div></body></html>'''


def row(n, title, start, end, status, permitted=None):
    who = f'<td class="clsTd" align="center" style="color:#999999">{permitted}</td>' if permitted else ''
    return (f'<tr valign="top" height="15" align="center"> <td class="clsTd" align="center">{n}</td> '
            f'<td class="clsTd" align="left">{title}</td> {who}'
            f'<td class="clsTd" align="center">{start.replace(" ", "<br>")}</td> '
            f'<td class="clsTd" align="center">{end.replace(" ", "<br>")}</td> '
            f'<td class="clsTd" align="center" style="color:#FF0000">{status}</td> </tr>')


def table(caption, columns, rows):
    head = ''.join(f'<td class="clsTd" align="center" style="color:#999999">{c}</td>' for c in columns)
    return (f'<table cellspacing="0" class="clsTbl"> <tbody><tr><td colspan="{len(columns)}" class="clsTd" '
            f'align="center"><b>{caption}</b></td></tr> <tr style="font-weight:bold;"> {head} </tr> '
            + ' '.join(rows) + ' </tbody></table>')


FALL_2026 = table('Event Calendar', ['#', 'EVENT TITLE', 'START DATE', 'END DATE', 'STATUS'], [
    row(1, 'Course Registration (for students)', '10.08.2026 00:00', '21.08.2026 23:59', 'CLOSED'),
    row(2, 'Attendance Entry', '01.09.2026 00:00', '13.12.2026 00:00', 'OPENED'),
    row(3, 'Add - Drop', '07.09.2026 00:00', '13.09.2026 00:00', 'CLOSED'),
    row(4, 'Apply for Late Course Registration', '14.09.2026 00:00', '19.09.2026 00:00', 'CLOSED'),
    row(5, 'Early withdrawal', '21.09.2026 00:00', '17.10.2026 00:00', 'CLOSED'),
    row(6, 'Late withdrawal', '19.10.2026 00:00', '12.12.2026 00:00', 'OPENED'),
    row(7, 'Pre-final grade', '23.11.2026 00:00', '12.12.2026 00:00', 'CLOSED'),
    row(8, 'Final Exam', '14.12.2026 00:00', '05.01.2027 00:00', 'CLOSED'),
    row(9, 'apply for FX', '28.12.2026 00:00', '06.01.2027 00:00', 'CLOSED'),
    row(10, 'Resit Exam', '11.01.2027 00:00', '14.01.2027 00:00', 'CLOSED'),
])
PERMISSIONS = table('Individual Permissions', ['#', 'EVENT TITLE', 'Permitted to', 'START DATE', 'END DATE', 'STATUS'], [
    row(1, 'Course Registration (for students)', '13.08.2026 00:00', '21.08.2026 23:59', 'INACTIVE',
        permitted='200000001'),
])
LOGIN_PAGE = '<form method="post"><input name="username"><input type="password" name="password"></form>'
NOW = datetime(2026, 11, 2, 10, 40)  # campus time


def portal(page=PAGE, answers=None, calls=None):
    """A stand-in for my.sdu.edu.kz: the page, then the AJAX answers by action."""
    answers = answers or {'GetCalendar': json.dumps({'CODE': '1', 'DATA': FALL_2026}),
                          'GetPermissions': json.dumps({'CODE': '1', 'DATA': PERMISSIONS})}

    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, text=page)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if calls is not None:
            calls.append(form)
        answer = answers[form['action']]
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, text=answer)

    return httpx.MockTransport(handle)


def fetch(transport):
    return asyncio.run(academic_calendar.fetch_calendar({'PHPSESSID': 'test'}, transport=transport))


class ParseTests(unittest.TestCase):
    def test_event_rows_with_their_times(self):
        events = academic_calendar.parse_events(FALL_2026)
        self.assertEqual(len(events), 10)
        self.assertEqual(events[5], Event('Late withdrawal', datetime(2026, 10, 19), datetime(2026, 12, 12), 'OPENED'))
        self.assertEqual(events[0].end, datetime(2026, 8, 21, 23, 59))

    def test_permissions_skip_the_student_number(self):
        [mine] = academic_calendar.parse_events(PERMISSIONS)
        self.assertEqual(mine, Event('Course Registration (for students)', datetime(2026, 8, 13),
                                     datetime(2026, 8, 21, 23, 59), 'INACTIVE'))

    def test_terms_and_the_current_one(self):
        self.assertEqual(academic_calendar.parse_terms(PAGE)[:2],
                         [('20261', '2026 - 2027 Fall', True), ('20253', '2025 - 2026 Summer', False)])
        self.assertEqual(academic_calendar.parse_terms(LOGIN_PAGE), [])


class FetchTests(unittest.TestCase):
    def test_both_tables_of_the_current_term(self):
        calls = []
        calendar = fetch(portal(calls=calls))
        self.assertEqual(calendar.term, '2026 - 2027 Fall')
        self.assertEqual([e.title for e in calendar.events][:3],
                         ['Course Registration (for students)', 'Attendance Entry', 'Add - Drop'])
        self.assertEqual(len(calendar.permissions), 1)
        self.assertEqual(calls, [{'ajx': '1', 'mod': 'calendar', 'action': 'GetCalendar', 'yt': '20261'},
                                 {'ajx': '1', 'mod': 'calendar', 'action': 'GetPermissions', 'yt': '20261'}])

    def test_signed_out_is_session_expired(self):
        for transport in (portal(page=LOGIN_PAGE),  # the page itself sends back to the login form
                          portal(answers={'GetCalendar': '#!3%6$#@458#!2*/-&2@', 'GetPermissions': ''})):
            with self.assertRaises(PortalError) as caught:
                fetch(transport)
            self.assertEqual(caught.exception.key, 'session_expired')

    def test_portal_failures(self):
        down = portal(answers={'GetCalendar': httpx.Response(502, text='Bad gateway'), 'GetPermissions': ''})
        refused = portal(answers={'GetCalendar': json.dumps({'CODE': '-1', 'DATA': 'error'}), 'GetPermissions': ''})
        odd = portal(answers={'GetCalendar': 'not json', 'GetPermissions': ''})
        for transport, key in ((down, 'unavailable'), (refused, 'unavailable'), (odd, 'unexpected_page')):
            with self.assertRaises(PortalError) as caught:
                fetch(transport)
            self.assertEqual(caught.exception.key, key)

        def offline(request):
            raise httpx.ConnectError('no route')

        with self.assertRaises(PortalError) as caught:
            fetch(httpx.MockTransport(offline))
        self.assertEqual(caught.exception.key, 'unavailable')


class RenderTests(unittest.TestCase):
    def test_states_by_dates_then_the_portals_word(self):
        events = {e.title: e for e in academic_calendar.parse_events(FALL_2026)}
        states = {title: academic_calendar.state(event, NOW) for title, event in events.items()}
        self.assertEqual(states['Late withdrawal'], 'open')
        self.assertEqual(states['Early withdrawal'], 'over')
        self.assertEqual(states['Pre-final grade'], 'upcoming')
        self.assertEqual(academic_calendar.state(Event('Manually closed', datetime(2026, 11, 1), datetime(2026, 11, 9), 'CLOSED'), NOW),
                         'closed')

    def test_card_in_each_language(self):
        calendar = academic_calendar.Calendar('2026 - 2027 Fall', academic_calendar.parse_events(FALL_2026),
                                              academic_calendar.parse_events(PERMISSIONS))
        card = academic_calendar.render(calendar, 'en', NOW)
        self.assertTrue(card.startswith('🗓 <b>System Calendar · 2026 - 2027 Fall</b>\n'
                                        '<i>my.sdu.edu.kz → Information → System Calendar</i>\n\n'))
        self.assertIn('🟢 <b>Late withdrawal</b>: 19.10.2026 00:00 – 12.12.2026 00:00 · open now', card)
        self.assertIn('⚪ <b>Early withdrawal</b>: 21.09.2026 00:00 – 17.10.2026 00:00 · over', card)
        self.assertIn('🔜 <b>Final Exam</b>: 14.12.2026 00:00 – 05.01.2027 00:00 · not open yet', card)
        self.assertIn('👤 <b>Your individual permissions</b>', card)
        self.assertNotIn('200000001', card)
        self.assertIn('· қазір ашық', academic_calendar.render(calendar, 'kk', NOW))
        self.assertIn('· открыто сейчас', academic_calendar.render(calendar, 'ru', NOW))
        empty = academic_calendar.render(academic_calendar.Calendar('2026 - 2027 Spring'), 'en', NOW)
        self.assertIn('has not published the calendar for this term yet', empty)

    def test_what_the_ai_reads(self):
        calendar = academic_calendar.Calendar('2026 - 2027 Fall', academic_calendar.parse_events(FALL_2026))
        data = academic_calendar.as_data(calendar, NOW)
        # The portal closes it at 12.12 00:00: the last day to withdraw is 11.12.
        self.assertEqual(data['events'][5], {'title': 'Late withdrawal', 'from': '2026-10-19 00:00',
                                             'until': '2026-12-11', 'now': 'open'})
        self.assertEqual(data['events'][0]['until'], '2026-08-21 23:59')
        self.assertIn('a date alone means to the end of that day', academic_calendar.MEANING)


if __name__ == '__main__':
    unittest.main()
