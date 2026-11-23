"""The website's data-only adapter for the same assistant tools as Telegram.

Only /api/me/chat reads this request's encrypted account cookies. Tokens,
passwords and cookies are never passed to the model or included in results.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import Request, Response, HTTPException

import academic_calendar
import assistant
import campus_events
import grade
import i18n
import moodle
import portal_schedule
import rooms
import service_data
import web_accounts


class WebToolbox:
    data_only = True

    def __init__(self, request: Request | None = None, response: Response | None = None):
        self.request, self.response = request, response
        self.portal_loaded = False
        self.portal = web_accounts.read_session(request, 'mysdu') if request else {}
        self.moodle = web_accounts.read_session(request, 'moodle') if request else {}

    def notes(self):
        return ('\nWEBSITE INTERFACE: Tool data is NOT displayed as separate cards. Answer the actual '
                'question using the returned facts; list matching items if requested. Never introduce '
                'a card that the website does not show. For sign-in use the website account dialog, '
                'never request credentials in chat. Use get_account_status to check connections.')

    def expire(self, account):
        if self.response is not None:
            web_accounts.clear_session(self.response, account)

    async def run(self, name, args, lang):
        if name not in {tool.name for tool in assistant.TOOLS}:
            return assistant.ToolResult({'error': 'Unknown service.'})
        try:
            return await self._run(name, args, lang)
        except (campus_events.FeedError, rooms.ScheduleError, rooms.SearchError, ValueError, TypeError):
            return assistant.ToolResult({'error': 'The requested service is temporarily unavailable or its filters are invalid. '
                                                 'Explain this instead of claiming no access or inventing data.'})
        except HTTPException:
            return assistant.ToolResult({"error": "The SDU authorization service is not configured or is unavailable. The administrator must configure server-side OAuth storage."})
        except grade.PortalError as exc:
            if exc.key in ('session_expired', 'two_factor_required'):
                self.portal = {}
                self.expire('mysdu')
                return assistant.ToolResult({}, auth='portal')
            return assistant.ToolResult({'error': 'This SDU connection does not include the requested data permission. The integration needs explicit additional consent; reconnecting with the same scopes will not add access.' if exc.key == 'permission_denied' else 'MySDU is temporarily unavailable. Please try again later.'})
        except moodle.MoodleError as exc:
            if exc.key == 'session_expired':
                self.moodle = {}
                self.expire('moodle')
                return assistant.ToolResult({}, auth='moodle')
            return assistant.ToolResult({'error': 'Moodle is temporarily unavailable.'})

    async def _run(self, name, args, lang):
        if name in ('get_my_schedule', 'get_my_grades', 'get_account_status', 'get_academic_calendar', 'sign_in') and not self.portal_loaded:
            if self.request:
                self.portal = await web_accounts.portal_session(self.request)
            self.portal_loaded = True
        if name == 'get_campus_events':
            return assistant.ToolResult(service_data.event_facts(await campus_events.fetch_events(), args))
        if name == 'get_canteen_menu':
            return assistant.ToolResult(service_data.menu_data(args, lang))
        if name == 'canteen_order_total':
            return assistant.ToolResult(service_data.order_data(args))
        if name == 'get_registration_guide':
            return assistant.ToolResult(service_data.guide_data(lang))
        if name == 'get_required_documents':
            return assistant.ToolResult(service_data.required_documents(args, lang))
        if name == 'find_free_rooms':
            return assistant.ToolResult(service_data.free_rooms(args))
        if name == 'get_account_status':
            return assistant.ToolResult({'mysdu_connected': bool(self.portal), 'moodle_connected': bool(self.moodle)})
        if name == 'get_reminder_settings':
            return assistant.ToolResult({'command': '/notify', 'managed_in': 'Telegram bot',
                                         'meaning': 'Website account connections are separate from Telegram notification preferences.'})
        if name == 'sign_in':
            account = 'moodle' if args.get('account') == 'moodle' else 'portal'
            linked = self.moodle if account == 'moodle' else self.portal
            return assistant.ToolResult({'already_signed_in': True, 'account': account}) if linked else assistant.ToolResult({}, auth=account)
        if name == 'get_academic_calendar':
            if self.portal and self.portal.get('mode') != 'oauth':
                try:
                    calendar = await academic_calendar.fetch_calendar(self.portal.get('cookies') or {})
                    now = datetime.now(rooms.CAMPUS_TZ).replace(tzinfo=None)
                    return assistant.ToolResult({**academic_calendar.as_data(calendar, now),
                                                 'meaning': academic_calendar.MEANING, 'source': 'MySDU System Calendar'})
                except grade.PortalError as exc:
                    if exc.key == 'session_expired':
                        self.portal = {}
                        self.expire('mysdu')
            return assistant.ToolResult({'source': 'approved academic calendar in the knowledge base',
                                         'note': 'Use the exact dates from the knowledge base. No sign-in is needed.'})
        if name in ('get_my_schedule', 'get_my_grades') and not self.portal:
            return assistant.ToolResult({}, auth='portal')
        if name == 'get_my_schedule':
            items = await web_accounts.portal_classes(self.portal)
            return assistant.ToolResult({'classes': items, 'timezone': 'Asia/Almaty', 'source': 'personal MySDU Course Schedule'})
        if name == 'get_my_grades':
            semesters = await web_accounts.portal_grades(self.portal)
            query = str(args.get('course') or '').strip()
            matches = grade.match_courses(semesters, query) if query else []
            selected = [(semester, [course]) for semester, course in matches] if query else [(s, s.courses) for s in semesters]
            return assistant.ToolResult({'semesters': [
                {'semester': s.title, 'gpa': s.gpa, 'spa': s.spa, 'courses': [
                    {'code': c.code, 'name': c.name, 'ects': c.ects,
                     'grade': (c.letter or c.score.replace('*', '').strip()) if (c.letter or c.score.replace('*', '').strip()).upper() not in ('', 'IP') else 'not published'} for c in courses]}
                for s, courses in selected], 'course_not_found': bool(query and not matches),
                'meaning': 'Only final transcript grades are available, not in-progress midterm scores.'})
        if name == 'get_my_deadlines':
            if not self.moodle:
                return assistant.ToolResult({}, auth='moodle')
            items = await moodle.fetch_deadlines(self.moodle)
            return assistant.ToolResult({'deadlines': [
                {'course': item.course, 'title': item.title,
                 'due': datetime.fromtimestamp(item.due, rooms.CAMPUS_TZ).isoformat()}
                for item in items[:100]], 'source': 'personal Moodle deadlines'})
        return assistant.ToolResult({'error': 'This service is unavailable in the website interface.'})


async def answer(message, history, lang, request=None, response=None):
    result = await assistant.run(message, history, lang, WebToolbox(request, response))
    if result.auth:
        account = 'Moodle' if result.auth == 'moodle' else 'MySDU'
        text = i18n.section(result.lang, 'web')['chatSignIn'].format(account=account)
        return {'reply': text, 'sign_in': 'moodle' if result.auth == 'moodle' else 'mysdu'}
    return {'reply': result.text or i18n.section(result.lang, 'web')['chatServiceError']}
