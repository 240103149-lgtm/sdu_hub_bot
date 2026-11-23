"""MySDU sign-in must pass its challenge and open protected data before success.

All upstream HTML and accounts are synthetic. No browser session is reused.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

import grade
import grade_bot
import notifications_bot
import portal_schedule
from harness import BotHarness

LOGIN = '<form method="post" action="/login"><input name="username"><input type="password" name="password"></form>'
# Observed MySDU form structure: the code input has no explicit type.
CODE = '<form method="post" action="verification.php"><input name="csrf" type="hidden" value="nonce"><input name="code" autocomplete="one-time-code"><button type="submit">Кодты растау</button></form>'
MENU = '<a href="index.php?mod=transkript">Transcript</a><a href="index.php?mod=course">Course Schedule</a><a href="logout.php">Sign out</a>'
TRANSCRIPT = '<h1>Transcript</h1><table><tr><td>2026 - 2027. 1</td></tr><tr><td>CSS 217</td><td>Architecture</td><td>3</td><td>5</td><td>90</td><td>A</td><td>4</td><td>Passed</td></tr></table>'
PENDING_COOKIE = {'set-cookie': 'SID=pending; Path=/'}
VERIFIED_COOKIE = {'set-cookie': 'SID=verified; Path=/'}


class PortalLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_form_shape_and_code_retry_before_protected_verification(self):
        seen = []
        def respond(request):
            seen.append(request)
            if request.url.path == '/':
                return httpx.Response(200, text=LOGIN)
            if request.url.path == '/login':
                self.assertEqual(parse_qs(request.content.decode())['password'], ['test-password'])
                return httpx.Response(303, headers={**PENDING_COOKIE, 'location': '/verification.php'})
            if request.url.path == '/verification.php':
                if request.method == 'GET':
                    return httpx.Response(200, text=CODE)
                fields = parse_qs(request.content.decode())
                self.assertEqual(fields['csrf'], ['nonce'])
                self.assertIn('SID=pending', request.headers['cookie'])
                if fields['code'] == ['wrong']:
                    return httpx.Response(200, text=CODE)
                return httpx.Response(303, headers={**VERIFIED_COOKIE, 'location': '/index.php?verified=1'})
            if request.url.query == b'verified=1':
                return httpx.Response(200, text=MENU)
            self.assertEqual(str(request.url), grade.GRADES_URL)
            self.assertIn('SID=verified', request.headers['cookie'])
            return httpx.Response(200, text=TRANSCRIPT)
        login = grade.PortalLogin(httpx.MockTransport(respond))
        try:
            self.assertTrue(await login.submit_credentials('123', 'test-password'))
            self.assertFalse(any(str(r.url) == grade.GRADES_URL for r in seen))
            with self.assertRaises(grade.PortalError) as error:
                await login.submit_code('wrong')
            self.assertEqual(error.exception.key, 'bad_code')
            await login.submit_code('123456')
            self.assertEqual(str(seen[-1].url), grade.GRADES_URL)
            self.assertIsNone(login._code_form)
            self.assertEqual(login.cookies(), {'SID': 'verified'})
            self.assertTrue(all('123456' not in str(r.url) and 'test-password' not in str(r.url) for r in seen))
        finally:
            await login.close()

    async def test_literal_html_and_script_redirects_reach_challenge(self):
        for html in (
            '<meta http-equiv="refresh" content="0; url=verification.php">',
            '<script>window.location.href = "verification.php";</script>',
            '<script>location.replace("/verification.php");</script>',
        ):
            with self.subTest(html=html):
                def respond(request):
                    if request.url.path == '/':
                        return httpx.Response(200, text=LOGIN)
                    if request.url.path == '/login':
                        return httpx.Response(200, text=MENU + html, headers=PENDING_COOKIE)
                    self.assertEqual(request.url.path, '/verification.php')
                    return httpx.Response(200, text=CODE)
                login = grade.PortalLogin(httpx.MockTransport(respond))
                try:
                    self.assertTrue(await login.submit_credentials('123', 'test-password'))
                    self.assertEqual(login._code_url, grade.PORTAL_BASE + '/verification.php')
                finally:
                    await login.close()

    async def test_menu_links_and_cookie_do_not_prove_login(self):
        def respond(request):
            return httpx.Response(200, text=LOGIN if request.url.path == '/' else MENU, headers=PENDING_COOKIE)
        login = grade.PortalLogin(httpx.MockTransport(respond))
        try:
            with self.assertRaises(grade.PortalError) as error:
                await login.submit_credentials('123', 'test-password')
            self.assertEqual(error.exception.key, 'unavailable')
        finally:
            await login.close()

    async def test_challenge_from_protected_page_is_still_requested(self):
        def respond(request):
            if request.url.path == '/':
                return httpx.Response(200, text=LOGIN)
            if request.url.path == '/login':
                return httpx.Response(200, text=MENU, headers=PENDING_COOKIE)
            return httpx.Response(200, text=CODE)
        login = grade.PortalLogin(httpx.MockTransport(respond))
        try:
            self.assertTrue(await login.submit_credentials('123', 'test-password'))
        finally:
            await login.close()

    async def test_success_without_new_challenge_still_requires_protected_records(self):
        def respond(request):
            if request.url.path == '/':
                return httpx.Response(200, text=LOGIN)
            return httpx.Response(200, text=TRANSCRIPT if request.method == 'GET' else MENU, headers=VERIFIED_COOKIE)
        login = grade.PortalLogin(httpx.MockTransport(respond))
        try:
            self.assertFalse(await login.submit_credentials('123', 'test-password'))
        finally:
            await login.close()

    async def test_after_code_a_menu_or_outage_cannot_complete_login(self):
        for body, status in ((MENU, 200), ('<h1>Maintenance</h1>', 200), ('Gateway error', 502)):
            with self.subTest(status=status, body=body):
                def respond(request):
                    if request.url.path == '/':
                        return httpx.Response(200, text=LOGIN)
                    if request.url.path == '/login':
                        return httpx.Response(200, text=CODE, headers=PENDING_COOKIE)
                    return httpx.Response(status, text=body, headers=VERIFIED_COOKIE)
                login = grade.PortalLogin(httpx.MockTransport(respond))
                try:
                    self.assertTrue(await login.submit_credentials('123', 'test-password'))
                    with self.assertRaises(grade.PortalError) as error:
                        await login.submit_code('123456')
                    self.assertEqual(error.exception.key, 'unavailable')
                finally:
                    await login.close()

    async def test_redirect_loop_and_external_targets_fail_closed(self):
        for body, headers in (
            ('<script>location="https://other.example/";</script>', {}),
            ('', {'location': 'https://other.example/'}),
            ('<script>location="/login";</script>', {}),
        ):
            with self.subTest(body=body, headers=headers):
                seen = []
                def respond(request):
                    seen.append(request)
                    self.assertEqual(request.url.host, 'my.sdu.edu.kz')
                    if request.url.path == '/':
                        return httpx.Response(200, text=LOGIN)
                    return httpx.Response(302 if headers else 200, text=body, headers=headers)
                login = grade.PortalLogin(httpx.MockTransport(respond))
                try:
                    with self.assertRaises(grade.PortalError):
                        await login.submit_credentials('123', 'test-password')
                    self.assertLessEqual(len(seen), 9)
                finally:
                    await login.close()

    async def test_external_form_never_receives_password(self):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, text=LOGIN.replace('/login', 'https://other.example/login'))
        login = grade.PortalLogin(httpx.MockTransport(respond))
        try:
            with self.assertRaises(grade.PortalError):
                await login.submit_credentials('123', 'test-password')
            self.assertEqual(len(seen), 1)
        finally:
            await login.close()

    async def test_saved_session_errors_are_not_empty_records(self):
        for body, expected in ((CODE, 'two_factor_required'), (LOGIN, 'session_expired'),
                               (MENU, 'unavailable'), ('Maintenance', 'unavailable')):
            for fetch in (grade.fetch_transcript, grade.fetch_grades):
                with self.subTest(body=body, fetch=fetch.__name__):
                    with self.assertRaises(grade.PortalError) as error:
                        await fetch({'SID': 'old'}, httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
                    self.assertEqual(error.exception.key, expected)
        empty = '<h1>Transcript</h1><p>No academic records available</p>'
        self.assertEqual(await grade.fetch_transcript({'SID': 'valid'}, httpx.MockTransport(
            lambda r: httpx.Response(200, text=empty))), [])

    async def test_schedule_challenge_is_not_empty_or_outage(self):
        with self.assertRaises(grade.PortalError) as error:
            await portal_schedule.fetch_schedule({'SID': 'pending'}, httpx.MockTransport(
                lambda r: httpx.Response(200, text=CODE)))
        self.assertEqual(error.exception.key, 'two_factor_required')


class TelegramPortalVerificationTests(BotHarness):
    async def sign_in(self):
        await self.text('/login')
        await self.tap('login:signin')
        await self.text('123')
        await self.text('test-password')

    async def test_code_is_requested_and_no_portal_is_saved_before_proof(self):
        real_login = grade.PortalLogin
        def respond(request):
            if request.url.path == '/':
                return httpx.Response(200, text=LOGIN)
            if request.url.path == '/login':
                return httpx.Response(200, text='<script>location="verification.php";</script>', headers=PENDING_COOKIE)
            if request.url.path == '/verification.php':
                return httpx.Response(200, text=CODE if request.method == 'GET' else MENU,
                                      headers=PENDING_COOKIE if request.method == 'GET' else VERIFIED_COOKIE)
            return httpx.Response(200, text=TRANSCRIPT)
        with patch.object(grade, 'PortalLogin', side_effect=lambda: real_login(httpx.MockTransport(respond))):
            await self.sign_in()
            self.assertIn('verification code', self.last_edit())
            self.assertNotIn('portal', self.app.user_data[42])
            self.assertIn(42, grade_bot.PENDING)
            await self.text('123456')
        self.assertEqual(self.app.user_data[42]['portal']['cookies'], {'SID': 'verified'})
        self.assertNotIn(42, grade_bot.PENDING)
        self.assertTrue(any('Signed in' in text for text in self.sent_texts()))
        self.assertGreaterEqual(self.delete.await_count, 2)

    async def test_false_success_becomes_visible_error_without_saving_session(self):
        real_login = grade.PortalLogin
        def respond(request):
            return httpx.Response(200, text=LOGIN if request.url.path == '/' else MENU, headers=PENDING_COOKIE)
        with patch.object(grade, 'PortalLogin', side_effect=lambda: real_login(httpx.MockTransport(respond))):
            await self.sign_in()
        self.assertIn('MySDU is temporarily unavailable', self.last_edit())
        self.assertNotIn('portal', self.app.user_data[42])
        self.assertFalse(any('Signed in' in text for text in self.sent_texts()))

    async def test_old_pending_session_shows_verification_error_and_reconnect_button(self):
        data = {'portal': {'cookies': {'SID': 'pending'}}}
        with patch.object(notifications_bot.notifications, 'personal_classes', side_effect=grade.PortalError('two_factor_required')):
            text, keyboard = await notifications_bot.personal_schedule_card(data, 'en')
        self.assertIn('MySDU requires a verification code', text)
        self.assertNotIn('portal', data)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'notify:link_portal')
