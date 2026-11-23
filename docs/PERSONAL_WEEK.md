# My week — automatic personal data

The website's My week now connects directly to the student's MySDU and Moodle accounts. It has no course search, section picker, Add/Remove buttons or browser-local timetable. The public university export is never used as the student's own schedule.

## Student flow

1. Open **My week** and connect **MySDU** using the student number and password. If MySDU asks for its email verification code, enter it in the second step.
2. Connect **Moodle** with its own username and password.
3. Classes and deadlines load together automatically. Previous week, this week, next week and refresh are the only timetable controls.
4. Disconnect either account independently. The other account's data continues to work.

Classes show the source, actual time, course and room. Moodle tasks show the source, course, task and due time. All dates use Asia/Almaty (UTC+5). An upstream failure or expired account is labelled separately; it is not shown as an empty timetable. No sample lessons are substituted for private data.

## Data and sessions

- `portal_schedule.py` opens the authenticated MySDU homepage, follows its own **Course Schedule** navigation link on the same portal, and parses a personal list table or weekly grid, including row/column spans. It does not guess a protected URL or search the campus export. Missing end times and rooms are not invented. If the page format is not recognized, that source is unavailable.
- `web_accounts.py` reuses the existing portal password/email-code and Moodle clients. Only session cookies/tokens are retained, encrypted with Fernet in HttpOnly, SameSite=Strict website cookies. HTTPS deployments also set Secure. Cookies are scoped to `/api/me`, have an eight-hour limit, and survive Vercel instance cold starts without a server-side database. Pending email-code state expires after ten minutes.
- Set a stable, random `WEB_SESSION_SECRET` to use a separate website encryption secret. When omitted, a domain-specific key is derived from the existing `TELEGRAM_BOT_TOKEN`; the token is never exposed to the browser. Rotating the secret or bot token invalidates website connections.
- Passwords and verification codes are not saved, logged, returned in API errors or sent to the AI assistant. Form fields are cleared after submission. Personal API responses use `Cache-Control: private, no-store`.
- POSTs require the same-origin UI header; cross-origin requests are rejected and no CORS access is granted. Each browser retains only its own encrypted sessions. Disconnect clears that browser's connection; it does not log the student out of the upstream university service.
- These website sessions are separate from Telegram account links and reminder subscriptions. Background notifications still require the persistent bot host described in `USER_STORIES.md`.

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /api/me/week?week=YYYY-MM-DD` | Monday–Sunday of the requested week; `mysdu` and `moodle` each have `status` and `items`. Status is `ready`, `disconnected`, `expired` or `unavailable`. |
| `POST /api/me/mysdu/login` | Student credentials; returns `connected` or `code_required`. |
| `POST /api/me/mysdu/code` | Completes the pending MySDU email-code challenge. |
| `POST /api/me/moodle/login` | Connects Moodle independently. |
| `POST /api/me/disconnect` | Clears the selected website account connection. |

Moodle requests use the selected week's exact timestamp range. MySDU exposes a recurring weekly grid, so weekday entries are placed in the requested week; ad hoc cancellations, vacations and semester changes depend on what the portal itself publishes. The existing public `/api/schedule` search endpoint remains for compatibility but is not called or displayed by My week.

## Verification limits

`tests/test_personal_week.py` uses scripted upstream HTTP and no real university credentials. It checks list/grid parsing, navigation discovery, password/email-code login across requests, encrypted and scoped cookies, expiry, disconnect, cross-site rejection, account isolation, partial upstream failures and exact Moodle week boundaries. It also checks that the website has no manual search or section picker. A real authenticated MySDU timetable cannot be verified without a student sign-in; a portal layout change must be checked against its authenticated page rather than guessed.
