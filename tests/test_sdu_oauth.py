"""OAuth PKCE, one-time state, per-visitor encrypted server records and provider failures."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import httpx
import grade
import main
import sdu_oauth


class OAuthTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.env = patch.dict(os.environ, {'VERCEL': '', 'SDU_OAUTH_REDIS_URL': '', 'SDU_OAUTH_REDIS_TOKEN': '',
             'WEB_SESSION_SECRET': 'offline-oauth-secret', 'SDU_OAUTH_DB_PATH': self.temp.name + '/oauth.db',
             'SDU_OAUTH_CLIENT_ID': 'public-test-client', 'SDU_OAUTH_REDIRECT_URI': 'https://hub.test/api/me/sdu/callback'})
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test')
        self.headers = {'X-SDU-Hub': '1', 'Origin': 'https://hub.test'}
        self.real_client = httpx.AsyncClient

    async def asyncTearDown(self):
        await self.client.aclose(); self.env.stop(); self.temp.cleanup()

    async def begin(self, client=None):
        response = await (client or self.client).post('/api/me/sdu/connect', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        values = parse_qs(urlsplit(response.json()['authorization_url']).query)
        self.assertEqual(values['scope'], ['profile:read schedule:read'])
        self.assertEqual(values['code_challenge_method'], ['S256'])
        return values

    def provider(self, callback):
        return patch.object(sdu_oauth.httpx, 'AsyncClient', side_effect=lambda **kw: self.real_client(transport=httpx.MockTransport(callback), **kw))

    async def test_successful_callback_pkce_and_no_token_in_browser(self):
        values = await self.begin()
        def exchange(request):
            fields = parse_qs(request.content.decode())
            self.assertEqual(fields['grant_type'], ['authorization_code'])
            challenge = base64.urlsafe_b64encode(hashlib.sha256(fields['code_verifier'][0].encode()).digest()).decode().rstrip('=')
            self.assertEqual(challenge, values['code_challenge'][0])
            return httpx.Response(200, json={'access_token': 'secret-access-token', 'token_type': 'Bearer', 'expires_in': 3600,
                                           'scope': sdu_oauth.SCOPES})
        with self.provider(exchange):
            response = await self.client.get('/api/me/sdu/callback', params={'state': values['state'][0], 'code': 'test-code'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/?sdu=connected')
        self.assertNotIn('secret-access-token', str(response.headers) + response.text)
        self.assertNotIn(b'secret-access-token', Path(self.temp.name + '/oauth.db').read_bytes())
        replay = await self.client.get('/api/me/sdu/callback', params={'state': values['state'][0], 'code': 'test-code'})
        self.assertEqual(replay.status_code, 400)

    async def test_wrong_missing_state_wrong_visitor_and_denial(self):
        values = await self.begin()
        for query in ({'code': 'test'}, {'state': 'x' * 43, 'code': 'test'}):
            self.assertEqual((await self.client.get('/api/me/sdu/callback', params=query)).status_code, 400)
        async with self.real_client(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test') as other:
            result = await other.get('/api/me/sdu/callback', params={'state': values['state'][0], 'code': 'test'})
            self.assertEqual(result.status_code, 400)
        denied = await self.client.get('/api/me/sdu/callback', params={'state': values['state'][0], 'error': 'access_denied'})
        self.assertEqual(denied.headers['location'], '/?sdu=denied')
        self.assertEqual((await self.client.get('/api/me/sdu/callback', params={'state': values['state'][0], 'code': 'test'})).status_code, 400)

    async def test_expired_attempt_and_failed_pkce_exchange(self):
        values = await self.begin()
        store = sdu_oauth.Store(); attempt_key = 'attempt:' + sdu_oauth.key(values['state'][0])
        attempt = await store.get(attempt_key); attempt['expires'] = time.time() - 1
        await store.put(attempt_key, attempt, 600)
        self.assertEqual((await self.client.get('/api/me/sdu/callback', params={'state': values['state'][0], 'code': 'test'})).status_code, 400)
        values = await self.begin()
        with self.provider(lambda request: httpx.Response(400, json={'error': 'invalid_grant'})):
            response = await self.client.get('/api/me/sdu/callback', params={'state': values['state'][0], 'code': 'bad-code'})
        self.assertEqual(response.status_code, 502)
        sid = self.client.cookies.get(sdu_oauth.SID_COOKIE)
        self.assertIsNone(await store.get('token:' + sdu_oauth.key(sid)))

    async def test_tokens_are_bound_to_visitors_and_expire(self):
        await self.begin(); sid = self.client.cookies.get(sdu_oauth.SID_COOKIE)
        store = sdu_oauth.Store()
        await store.put('token:' + sdu_oauth.key(sid), {'access_token': 'private', 'expires': time.time() + 30, 'scope': sdu_oauth.SCOPES}, 30)
        async with self.real_client(transport=httpx.ASGITransport(app=main.app), base_url='https://hub.test') as other:
            other_values = await self.begin(other); other_sid = other.cookies.get(sdu_oauth.SID_COOKIE)
            self.assertNotEqual(other_sid, sid)
            self.assertIsNone(await store.get('token:' + sdu_oauth.key(other_sid)))
        await store.put('token:' + sdu_oauth.key(sid), {'access_token': 'expired', 'expires': time.time() - 1}, -1)
        self.assertIsNone(await store.get('token:' + sdu_oauth.key(sid)))

    async def test_mysdu_outage_code_required_and_scope_failures_are_distinct(self):
        session = {'access_token': 'private', 'scope': 'schedule:read'}
        for status, payload, expected in [(502, {}, 'unavailable'), (401, {}, 'session_expired'),
             (403, {}, 'permission_denied'), (409, {'detail': {'code': 'sdu_2fa_required'}}, 'two_factor_required')]:
            with self.provider(lambda request: httpx.Response(status, json=payload)):
                with self.assertRaises(grade.PortalError) as error:
                    await sdu_oauth.read(session, 'schedule', 'schedule:read')
            self.assertEqual(error.exception.key, expected)
        with self.assertRaises(grade.PortalError) as error:
            await sdu_oauth.read(session, 'transcript', 'transcript:read')
        self.assertEqual(error.exception.key, 'permission_denied')

    async def test_disconnect_revokes_actual_token_and_clears_local_access(self):
        await self.begin(); sid = self.client.cookies.get(sdu_oauth.SID_COOKIE)
        store = sdu_oauth.Store()
        await store.put('token:' + sdu_oauth.key(sid), {'access_token': 'actual-token', 'scope': sdu_oauth.SCOPES, 'expires': time.time() + 60}, 60)
        def revoke(request):
            self.assertEqual(parse_qs(request.content.decode())['token'], ['actual-token'])
            return httpx.Response(200, json={})
        with self.provider(revoke):
            response = await self.client.post('/api/me/disconnect', json={'account': 'mysdu'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await store.get('token:' + sdu_oauth.key(sid)))

    async def test_vercel_fails_closed_without_durable_server_storage(self):
        with patch.dict(os.environ, {'VERCEL': '1'}):
            response = await self.client.post('/api/me/sdu/connect', headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'oauth_storage_not_configured')
