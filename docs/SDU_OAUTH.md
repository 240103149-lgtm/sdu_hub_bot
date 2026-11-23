# SDU Platform connection

SDU Platform is the provider at https://api-sdu.javazhan.tech; it is not the
university's official OAuth issuer. The student signs in, completes SDU 2FA
when required, and grants consent on that provider's page. SDU Hub does not
render an SDU password or code form in the new website connection flow.
The existing Telegram portal client remains independent.

Production callback:
`https://sdu-hub-bot.vercel.app/api/me/sdu/callback`

Registered public client: `l_rLszJkrkLdXdz7m_TTrw`.
Registered/requested scopes: `profile:read schedule:read` only.
The public client ID and exact redirect URI are in `sdu_oauth_client.json`.
There is no client secret. Registration is not repeated during login.
Grades through this OAuth connection require a separately authorized client
scope; reconnecting with the same scopes cannot grant it. Existing Telegram
transcript tools continue to use the student's own linked portal session.

## Required Vercel configuration

Keep `WEB_SESSION_SECRET` stable and secret (existing fallback:
`TELEGRAM_BOT_TOKEN`). Configure a durable Redis REST database with
`SDU_OAUTH_REDIS_URL` and `SDU_OAUTH_REDIS_TOKEN` in Vercel environment settings,
then redeploy. Never send these secret values through chat or commit them.
`SDU_OAUTH_CLIENT_ID` and `SDU_OAUTH_REDIRECT_URI` may override the public config.
Production callback must exactly match the registered URL, including its path.

Vercel is intentionally blocked from storing OAuth tokens in ephemeral `/tmp`.
Until Redis is configured, Connect MySDU returns
`503 oauth_storage_not_configured` and the page explains the setup problem;
it does not pretend the account is connected or collect a password instead.
On a persistent host/local development, set `SDU_OAUTH_DB_PATH` to a durable
SQLite database. SQLite records and Redis values are encrypted with Fernet.
Browsers receive only a random HttpOnly SameSite=Lax local session identifier.
Access tokens, PKCE verifiers and provider cookies never reach frontend code,
AI prompts, URLs, localStorage or HTML. State is short-lived, visitor-bound
and consumed atomically exactly once before a token exchange.

## Errors

MySDU/SDU Platform source errors are distinguished from empty results:
401 → reconnect; 403 → missing permission; 409 sdu_2fa_required → reconnect
and complete 2FA at the provider; 502/504 → temporarily unavailable.
The native Telegram portal login likewise rejects maintenance/unknown pages
instead of accepting an HTTP 200 and a cookie as proof of authentication.
The public login page responding with HTTP 200 does not prove that student
sign-in or live data reads work.

The provider's schedule read is request-time data, not a stored snapshot.
Moodle stays separately connected through its native service: the provider's
Moodle deadline endpoint exposes only future deadlines and cannot satisfy the
requested full-month/completed-work calendar.

## Verification

`python -m unittest discover -s tests` and `node --check static/app.js`.
Tests cover callback success, PKCE, denial, wrong/missing/replayed/expired state,
visitor isolation, expired/revoked tokens, permissions, provider 2FA/outages,
local disconnect/revocation, encrypted records and Vercel fail-closed storage.
No real student login or 2FA was performed. A human still needs to verify the
provider login after deployment and storage configuration.
