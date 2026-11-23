# Personal month calendar

The website calendar now shows a full month instead of a weekly list. It reads
only the visitor's own linked MySDU classes and native Moodle calendar. No
campus-section picker or sample personal records are used.

## Student flow

1. Open My calendar and Connect MySDU. SDU Platform handles the university
   login, consent and any 2FA in its own browser page. See `SDU_OAUTH.md` for
   the registered callback, client ID and required server storage.
2. Connect Moodle separately with the existing Moodle sign-in dialog.
3. Navigate previous/current/next month. Select a day to read all of its
   classes and tasks; on narrow screens the grid uses compact indicators.
4. The default All filter includes completed tasks. Moodle completion states
   are used for the Completed/Not completed filters. If tracking is unavailable,
   the task is labelled Status unknown; elapsed deadlines never imply completion.
5. Disconnect accounts independently. This also clears chat history and ignores
   in-flight chat responses from the previous account context.

All dates use Asia/Almaty. Source errors remain visible independently: MySDU
unavailability does not erase valid Moodle records, and neither an outage nor a
2FA challenge is treated as a successful connection or an empty personal schedule.
The current Apple-inspired typography, quiet surfaces and blue controls are retained.

## Sessions and AI access

New MySDU connections use server-side encrypted OAuth records with random opaque
browser identifiers. Production Vercel needs durable Redis REST storage; no `/tmp`
fallback is used. Native Moodle sessions still use encrypted HttpOnly SameSite=Strict
cookies under `/api/me`. Legacy direct MySDU routes remain for compatibility/testing;
the website does not render their password or 2FA form for Connect MySDU.

`/api/me/chat` uses the same verified service facts as the bot and can read only
this request's linked-account data when answering a relevant question. Account
secrets are never sent to AI. `/api/chat` is limited to public service data.
Private API responses use `private, no-store`, and state-changing requests and
private chat require same-origin checks. Website account sessions remain separate
from Telegram links and reminder preferences.

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /api/me/month?month=YYYY-MM-DD` | Full month, independent source statuses and explicit completion values. |
| `POST /api/me/sdu/connect` | Starts a visitor-bound PKCE flow; returns the provider authorization URL. |
| `GET /api/me/sdu/callback` | Validates and consumes state once, then exchanges the code server-side. |
| `POST /api/me/moodle/login` | Connects native Moodle. |
| `POST /api/me/disconnect` | Revokes/removes the selected connection. |
| `POST /api/me/chat` | Verified public facts and this visitor's authorized personal data. |
| `GET /api/me/week` | Retained for backward compatibility; no longer used by the frontend. |

Moodle reads `core_calendar_get_calendar_monthly_view`, which includes activities
that the action-events timeline omits after completion. Completion comes from
`core_completion_get_activities_completion_status`. Limited Moodle function
permissions are reflected as unavailable completion status, not invented results.
MySDU publishes recurring weekday classes; ad hoc holidays/cancellations depend
on what its source exposes. Public campus-export availability is not personal data.

## Verification

Offline tests cover full-month ranges, repeated weekdays, source isolation,
Moodle REST/AJAX calendar reads, deduplication, completion true/false/unknown,
maintenance responses, OAuth/PKCE lifecycle, per-visitor access and API error states.
No real student's SDU sign-in, 2FA or Moodle completion records were accessed.
