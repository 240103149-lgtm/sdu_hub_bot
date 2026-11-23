# SDU Hub — campus events and reminders

Updated: 23 November 2026. Existing US1–US13 numbering is retained; these additions are US14 and US15. US4 is extended to use SDU Life.

| Story | User story | Acceptance criteria |
| --- | --- | --- |
| US4 — live campus events | As an SDU student, I want to discover university events from SDU Life so that I can take part in campus activities. | The bot and website show published titles, dates, campus-local times, venues, organizers and descriptions from the anonymous SDU Life homepage. Missing information is not guessed. Event details link to SDU Life; registration may require sign-in. Past events are hidden by default. A failed refresh is labelled when cached data is used. |
| US14 — deadline reminder | As a student with linked Moodle, I want a reminder 12 hours before an assignment is due so that I have time to complete it. | The student enables deadline reminders in Account → Reminders. Within the last 12 hours the bot sends the course, task, due time and time remaining. No reminders are sent before this window or after the due time. Completed tasks disappear from Moodle's action-events feed. Opening events are excluded. Each due date is notified once; a changed due date creates a new reminder. Disabling reminders or unlinking Moodle stops future reminders. |
| US15 — class reminder | As a student, I want my course and room 15 minutes before class so that I can get there on time. | The student enables class reminders through /notify. Classes come only from the linked MySDU Course Schedule; no section picker or campus-export fallback is used. Messages include the course, start time and room. No reminder is sent before the 15-minute window, after the class starts or outside the configured teaching term. Unlinking MySDU or disabling class reminders stops future messages. Different students' accounts and preferences are isolated. |
| Website — shared features | As a student, I want one clear place for university answers, events and my week so that I can plan without searching different pages. | Responsive Apple-inspired layout; three interface languages; keyboard-accessible navigation; live events with detail dialogs; working AI chat and registration guide; automatic account-backed MySDU timetable and Moodle deadlines; no manual course search or section picker. Samples on the homepage are labelled as previews. |

## Reminder setup

1. Install the updated dependencies: `pip install -r requirements.txt`.
2. Fill the existing `.env` with your own AI and Telegram credentials (see `env.example`).
3. Run **one** persistent bot process: `python bot.py`. If the token already has a webhook, remove that webhook before switching to polling. Never run two polling instances for the same token.
4. Open `/notify` (also in the /start message and Telegram command menu). Link MySDU and Moodle using the buttons if needed. Accounts connected only on the website are separate from Telegram accounts.
5. Enable class and/or deadline reminders. Each enable action checks for due reminders immediately; subsequent minute checks use the same deduplication. Linking an account alone does not opt the student in. No separate confirmation message is sent.
6. `/schedule` shows the authenticated MySDU timetable or a sign-in button; no campus-section picker is shown. The five-command Telegram menu includes `/notify`.

For a persistent webhook deployment, use the existing README webhook configuration and start:

```bash
uvicorn main:app --host 0.0.0.0 --port "$PORT"
```

The application starts the same scheduler during its normal lifecycle. Do not additionally start `bot.py` with the same token. Set `REMINDERS_ENABLED=off` to disable the scheduler for an entire host.

The scheduler checks every 60 seconds. Delivery can be delayed by up to a check interval plus Moodle/Telegram request latency. Enabling within a reminder window sends one catch-up reminder; it does not claim a full 12 hours or 15 minutes remain. Failed sends are retried within the remaining window. An expired MySDU or Moodle session produces one relink notice. A Telegram block suspends that student's delivery until they open `/start` or explicitly enable reminders again.

`bot_state.pickle` stores preferences, existing account sessions and sent-reminder identifiers. Set `BOT_STATE_PATH=/var/data/bot_state.pickle` (or your host's mounted volume path) to keep it on a persistent disk, restrict access to the service account and run a single bot instance. Do not commit it. Successful sends are persisted on each scheduler tick. A process crash between Telegram accepting a message and the disk write can cause a duplicate after restart; there is no exactly-once transaction across Telegram and local storage.

The settings card is intentionally short. It does not display hosting diagnostics or section-selection instructions. The On/Off buttons describe saved preferences; they cannot start infrastructure that is not running.

### Hosting boundary

**Vercel serves the website and webhook requests but does not run a persistent scheduler.** `notifications.install()` deliberately skips Vercel. Its temporary filesystem also cannot reliably retain subscriptions between cold starts. To activate automatic reminders, move the bot to a persistent host with durable storage (or run polling continuously on a computer). The website can remain on Vercel.

Do not add a Vercel cron over ephemeral state and assume reminders work. A future serverless implementation needs a durable shared datastore, authenticated scheduled triggers and coordinated delivery.

### Timetable validity

`data/schedule_meta.json` now includes `valid_from` and `valid_until`. The current export is dated 4 October 2026 and covers teaching through 12 December 2026, matching the academic calendar in `knowledge/sdu_academic_calendar_2026_2027.md`. Replace the export and validity range each term. Class notifications stop outside this range. Last-minute room changes and ad hoc cancellations are not synchronized with the portal; verify them there.

The website now has its own authenticated My week: MySDU classes and Moodle deadlines load automatically after the student connects each account. Search and manual section selection have been removed from the website. Telegram reminder subscriptions retain their existing settings. Website connections are separate from the bot and do not automatically subscribe the student to notifications. See [PERSONAL_WEEK.md](PERSONAL_WEEK.md).

## Event source

`campus_events.py` reads only `https://life.sdu.edu.kz/en` without credentials. It parses the publicly rendered Next.js Flight event array, including UTF-8 long-text records, without executing source scripts. Only approved HTTPS image hosts are retained. A five-minute in-memory cache coalesces concurrent reads. On a failed refresh, data up to one hour old is marked `stale`; older data is discarded. A changed source format is an availability error, not a claim that there are zero events. The Telegram interface can fall back to the existing curated event file; the website offers retry.

`/api/events` returns `events`, `source`, `fetched_at` (Unix seconds) and `stale`. `/api/schedule` searches the existing public campus export, returns at most 30 sections per page and includes the export's validity metadata. Neither endpoint returns personal student data.

## Verification

```bash
python -m unittest discover -s tests -v
node --check static/app.js
```

The test harness disables live events and replaces Telegram, Moodle, the portal and AI services. New tests cover reminder boundaries, duplicate suppression, persisted state reload, next-week delivery, term expiry, failed-send retry, expired sessions, private routing, student isolation, personal schedule sign-in, legacy deep links, immediate delivery from both accounts, public APIs and event cache/parser failures. GitHub Actions runs these checks on pushes and pull requests.

Live Moodle login and actual Telegram delivery require credentials and must be checked on the deployment. Reading public SDU Life events is verified separately from the offline suite.
