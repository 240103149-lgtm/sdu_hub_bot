"""The unified AI assistant: tool calling, sign-in with automatic continuation,
conversation context, and both providers' request formats.

Offline: the model is a scripted stand-in (or a fake Gemini client / a fake
DeepSeek HTTP response); the portal and Moodle are test doubles.
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import copy
import json
import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from harness import BotHarness  # noqa: E402

import assistant  # noqa: E402
import core  # noqa: E402
import grade  # noqa: E402
import grade_bot  # noqa: E402
import moodle  # noqa: E402
import rooms  # noqa: E402
from assistant import Call, Reply  # noqa: E402


class ScriptedModel:
    """Stands in for Gemini / DeepSeek: replays scripted turns and records
    everything it was given, so tests can check what reached the model."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.chats = []    # {"system", "history", "question"} for every question
        self.results = []  # the tool results it received

    def new_chat(self, system, history, question):
        model = self
        model.chats.append({'system': system, 'history': list(history), 'question': question})

        class Chat:
            def ask(self, name, timeout):
                turn = model.turns.pop(0)
                if isinstance(turn, BaseException):
                    raise turn
                return turn

            def add_results(self, results):
                model.results.append(results)

        return Chat()

    def everything_seen(self):
        return json.dumps([self.chats, [[(c.name, d) for c, d in r] for r in self.results]],
                          ensure_ascii=False, default=str)


def call(name, **args):
    return Reply(text='', calls=[Call(id=f'call-{name}', name=name, args=args)])


def say(text):
    return Reply(text=text, calls=[])


class Toolbox:
    """A toolbox double for the conversation tests."""

    def __init__(self, **results):
        self.results, self.calls = results, []

    async def run(self, name, args, lang):
        self.calls.append((name, args, lang))
        return self.results[name]


# ------------------------------------------------------------ the loop ----


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, model, toolbox, question='What deadlines do I have?', history=(), lang='en'):
        with patch.object(assistant, 'new_chat', model.new_chat):
            return await assistant.run(question, list(history), lang, toolbox)

    async def test_plain_answer_keeps_the_us1_rules(self):
        model = ScriptedModel(say("I don't have this information, please contact the administration."))
        answer = await self.ask(model, Toolbox(), 'When does the canteen open?')
        self.assertTrue(answer.text.startswith("I don't have this information"))
        self.assertIn('ssc@sdu.edu.kz', answer.text)  # the fallback always comes with contacts
        self.assertEqual(answer.tools, [])

    async def test_tool_result_goes_back_to_the_model(self):
        toolbox = Toolbox(get_my_deadlines=assistant.ToolResult(
            {'deadlines': [{'course': 'CSS 206', 'title': 'Lab 3', 'due': '2026-11-03 23:59'}]},
            summary='[shown: Moodle deadlines, 1]'))
        model = ScriptedModel(call('get_my_deadlines'), say('Your nearest deadline is Lab 3 (CSS 206).'))
        answer = await self.ask(model, toolbox)
        self.assertEqual(answer.text, 'Your nearest deadline is Lab 3 (CSS 206).')
        self.assertEqual(answer.tools, ['get_my_deadlines'])
        self.assertEqual(answer.summaries, ['[shown: Moodle deadlines, 1]'])
        (sent,), = model.results
        self.assertEqual(sent[0].name, 'get_my_deadlines')
        self.assertEqual(sent[1]['deadlines'][0]['title'], 'Lab 3')

    async def test_sign_in_needed_stops_before_asking_the_model_again(self):
        model = ScriptedModel(call('get_my_grades'), say('never asked'))
        answer = await self.ask(model, Toolbox(get_my_grades=assistant.ToolResult({}, auth='portal')),
                                'Show me my grades')
        self.assertEqual(answer.auth, 'portal')
        self.assertEqual(answer.text, '')
        self.assertEqual(model.turns, [say('never asked')])  # the second turn was not used

    async def test_prompt_offers_tools_sources_and_never_asks_for_passwords(self):
        model = ScriptedModel(say('ok'))
        await self.ask(model, Toolbox(), 'Жатақханаға қалай өтініш беремін?', lang='en')
        prompt = model.chats[0]['system']
        self.assertIn('TOOLS: you can act', prompt)
        self.assertIn('NEVER ask for a password', prompt)
        self.assertNotIn('say to use /deadline', prompt)  # the website's command rule is replaced
        self.assertIn('OFFICIAL SOURCES', prompt)
        self.assertIn('"Дереккөз:"', prompt)  # the source label in the question's language
        self.assertIn('Student House (dormitory', prompt)  # the knowledge base is there

    async def test_follow_up_gets_the_earlier_turns(self):
        model = ScriptedModel(say('F101 and F104.'))
        history = [{'role': 'user', 'text': 'Free rooms now?'},
                   {'role': 'assistant', 'text': 'Here they are.\n[shown: 3 free rooms for Mo 10:30-11:20: F101, F104, G305]'}]
        await self.ask(model, Toolbox(), 'Which of them are in block F?', history)
        self.assertEqual(model.chats[0]['history'], [
            ('user', 'Free rooms now?'),
            ('assistant', 'Here they are.\n[shown: 3 free rooms for Mo 10:30-11:20: F101, F104, G305]'),
        ])

    async def test_slow_or_unreachable_model_is_a_connection_error(self):
        class Slow:
            def new_chat(self, *args):
                return SimpleNamespace(ask=lambda name, timeout: time.sleep(1.0))

        with patch.object(core, 'RESPONSE_BUDGET', 0.2), patch.object(assistant, 'new_chat', Slow().new_chat):
            started = time.monotonic()
            with self.assertRaises(core.AssistantError) as error:
                await assistant.run('Free rooms?', [], 'en', Toolbox())
        self.assertEqual(error.exception.key, 'connection')
        self.assertLess(time.monotonic() - started, 0.8)

        model = ScriptedModel(*[httpx.ConnectError('no route to host')] * 10)
        with patch.object(core, 'RESPONSE_BUDGET', 0.5), patch.object(core, 'RETRY_PAUSE', 0.01):
            with self.assertRaises(core.AssistantError) as error:
                await self.ask(model, Toolbox(), 'Free rooms?')
        self.assertEqual(error.exception.key, 'connection')

    async def test_empty_and_too_long_questions(self):
        for question, key in (('  ', 'empty'), ('x' * 2001, 'too_long')):
            with self.assertRaises(core.AssistantError) as error:
                await self.ask(ScriptedModel(), Toolbox(), question)
            self.assertEqual(error.exception.key, key)


# ------------------------------------------------------- the providers ----


class ProviderFormatTests(unittest.TestCase):
    def test_deepseek_tool_calling_request_and_reply(self):
        sent = []

        def post(url, headers, json, timeout):
            sent.append((url, headers, copy.deepcopy(json)))  # httpx serializes the body at once
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function',
                 'function': {'name': 'find_free_rooms', 'arguments': '{"date": "2026-11-02", "start_time": "14:00"}'}},
                {'id': 'call_2', 'type': 'function', 'function': {'name': 'get_my_grades', 'arguments': '{broken'}},
            ]}
            return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'tool_calls'}]},
                                  request=httpx.Request('POST', url))

        chat = assistant.DeepSeekChat('SYSTEM', [('user', 'hi'), ('assistant', 'hello')], 'Free rooms tomorrow 14:00?')
        with patch.object(assistant.httpx, 'post', post):
            reply = chat.ask('deepseek-flash', 5)
        url, headers, body = sent[0]
        self.assertEqual(url, 'https://api.deepseek.com/chat/completions')
        self.assertTrue(headers['Authorization'].startswith('Bearer '))
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual({t['function']['name'] for t in body['tools']}, {t.name for t in assistant.TOOLS})
        self.assertEqual(reply.calls[0], Call('call_1', 'find_free_rooms', {'date': '2026-11-02', 'start_time': '14:00'}))
        self.assertEqual(reply.calls[1].args, {})  # invalid JSON from the model: no arguments

        chat.add_results([(reply.calls[0], {'free_count': 2})])
        self.assertEqual(chat.messages[-2]['role'], 'assistant')
        self.assertEqual(chat.messages[-2]['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(chat.messages[-1], {'role': 'tool', 'tool_call_id': 'call_1', 'content': '{"free_count": 2}'})

    def test_gemini_function_calling_keeps_the_model_turn(self):
        from google.genai import types

        content = types.Content(role='model', parts=[
            types.Part(function_call=types.FunctionCall(id='fc-1', name='get_my_deadlines', args={}),
                       thought_signature=b'signature'),
        ])
        response = SimpleNamespace(candidates=[SimpleNamespace(content=content)])
        received = []

        def generate_content(model, contents, config):
            received.append((model, list(contents), config))
            return response

        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        with patch.object(core, 'client', client):
            chat = assistant.GeminiChat('SYSTEM', [('assistant', 'earlier answer')], 'What deadlines do I have?')
            reply = chat.ask('gemini-test', 5)
        self.assertEqual(reply.calls, [Call('fc-1', 'get_my_deadlines', {})])
        model, contents, config = received[0]
        self.assertEqual([c.role for c in contents], ['model', 'user'])
        self.assertTrue(config.automatic_function_calling.disable)
        names = {d.name for d in config.tools[0].function_declarations}
        self.assertEqual(names, {t.name for t in assistant.TOOLS})
        self.assertIs(chat.contents[-1], content)  # sent back as it came, with its signature

        chat.add_results([(reply.calls[0], {'deadlines': []})])
        answer = chat.contents[-1]
        self.assertEqual(answer.role, 'user')
        self.assertEqual(answer.parts[0].function_response.name, 'get_my_deadlines')
        self.assertEqual(answer.parts[0].function_response.id, 'fc-1')
        self.assertEqual(answer.parts[0].function_response.response, {'result': {'deadlines': []}})


# -------------------------------------------- the assistant in the bot ----


class FakePortal:
    """grade.PortalLogin stand-in: any password works, no 2FA code."""

    def __init__(self, *args, **kwargs):
        pass

    async def submit_credentials(self, student_id, password):
        return False

    def cookies(self):
        return {'session': 'cookie'}

    async def close(self):
        pass


def transcript():
    return [grade.Semester('2026 - 2027. 1', courses=[
        grade.Course('CSS 217', 'Software Architecture', '3', '5', '91', 'A-', '3.67', ''),
    ], ects='5', spa='3.67', gpa='3.50')]


def next_monday():
    day = datetime.now(rooms.CAMPUS_TZ).date() + timedelta(days=1)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


class AssistantBotTests(BotHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ai_patcher.stop()  # the real assistant.run, with a scripted model
        self.patches.remove(self.ai_patcher)
        self.model = ScriptedModel()
        model = patch.object(assistant, 'new_chat', self.model.new_chat)
        self.patches.append(model)
        model.start()

    def script(self, *turns):
        self.model.turns += list(turns)

    def all_sent(self):
        return '\n'.join(self.sent_texts())

    def buttons_of(self, text_part):
        for c in reversed(self.send.call_args_list):
            if text_part in c.kwargs.get('text', ''):
                markup = c.kwargs.get('reply_markup')
                return [[b.callback_data or b.url for b in row] for row in markup.inline_keyboard] if markup else []
        self.fail(f'no message with {text_part!r}')

    async def test_grades_sign_in_then_the_question_is_answered_by_itself(self):
        self.script(call('get_my_grades'))
        await self.text('Show me my grades')
        self.assertIn('university portal (my.sdu.edu.kz) account', self.last_text())
        self.assertEqual(self.last_buttons(), [[('🔐 Sign in', 'auth:portal'), ('✖️ Not now', 'auth:later')]])
        self.assertEqual(self.app.user_data[42]['pending_ask']['text'], 'Show me my grades')

        await self.tap('auth:portal')
        self.assertIn('student number', self.last_edit())
        await self.text('240103149')
        self.script(call('get_my_grades'), say('Your cumulative GPA is 3.50.'))
        with patch.object(grade_bot.grade, 'PortalLogin', FakePortal), \
             patch.object(grade_bot.grade, 'fetch_transcript', new=AsyncMock(return_value=transcript())):
            await self.text('secret-password')

        sent = self.all_sent()
        self.assertIn('✅ Now, your question: «Show me my grades»', sent)
        self.assertIn('Your cumulative GPA is 3.50.', sent)
        self.assertIn('<b>CSS 217</b> | Software Architecture | 5 ECTS | <b>A-</b>', sent)  # the /grade card
        self.assertEqual(self.app.user_data[42]['portal']['student_id'], '240103149')
        self.assertNotIn('pending_ask', self.app.user_data[42])
        self.assertTrue(self.delete.await_count >= 1)  # the password message was deleted
        self.assertNotIn('secret-password', self.model.everything_seen())  # never reached the AI
        self.assertNotIn('secret-password', repr(dict(self.app.user_data[42])))
        self.assertEqual([chat['question'] for chat in self.model.chats], ['Show me my grades'] * 2)

    async def test_deadlines_sign_in_to_moodle_then_answered_once(self):
        self.script(call('get_my_deadlines'))
        await self.text('What deadlines do I have?')
        self.assertIn('Moodle (moodle.sdu.edu.kz) account', self.last_text())
        await self.tap('auth:moodle')
        self.assertIn('Moodle', self.last_edit())
        await self.text('moodle-user')
        due = int(time.time()) + 86400
        items = [moodle.Deadline(course='CSS 206', title='Lab 3', due=due, url='https://moodle.sdu.edu.kz/mod/assign/view.php?id=1')]
        fetch = AsyncMock(return_value=items)
        self.script(call('get_my_deadlines'), say('Your nearest deadline is Lab 3 (CSS 206), tomorrow.'))
        with patch.object(moodle, 'login', new=AsyncMock(return_value={'mode': 'token', 'token': 't'})), \
             patch.object(moodle, 'fetch_deadlines', new=fetch):
            await self.text('moodle-password')
        sent = self.all_sent()
        self.assertIn('Your nearest deadline is Lab 3 (CSS 206), tomorrow.', sent)
        self.assertIn('Lab 3</a>', sent)  # the /deadline card, with the link
        fetch.assert_awaited_once()  # the list is not sent twice after the sign-in
        self.assertNotIn('moodle-password', self.model.everything_seen())
        deadlines = self.model.results[-1][0][1]['deadlines']
        self.assertEqual((deadlines[0]['course'], deadlines[0]['title'], deadlines[0]['overdue']), ('CSS 206', 'Lab 3', False))

    async def test_not_now_forgets_the_question(self):
        self.script(call('get_my_deadlines'))
        await self.text('What deadlines do I have?')
        await self.tap('auth:later')
        self.assertEqual(self.last_edit(), 'Okay. Ask me again whenever you like.')
        self.assertNotIn('pending_ask', self.app.user_data[42])

    async def test_free_rooms_from_plain_words_and_a_follow_up(self):
        monday = next_monday()
        self.script(call('find_free_rooms', date=monday.isoformat(), start_time='14:00', end_time='16:00'),
                    say('Many rooms are free then, for example F101.'))
        await self.text('Are there any free classrooms next Monday from 14 to 16?')
        sent = self.all_sent()
        self.assertIn('Many rooms are free then, for example F101.', sent)
        self.assertIn(f'{monday:%d.%m.%Y}, 14:00–16:00', sent)  # the /rooms interval card
        self.assertIn('Available according to schedule', sent)
        data = self.model.results[-1][0][1]
        self.assertGreater(data['free_count'], 0)
        self.assertEqual(data['free_count'], len(rooms.free_rooms_between('Mo', 14 * 60, 16 * 60)))
        remembered = self.app.user_data[42]['history'][-1]['text']
        self.assertIn('[shown:', remembered)

        self.script(say('F101, F102 and F103 are in block F.'))
        await self.text('Which of them are in block F?')
        self.assertIn('[shown:', self.model.chats[-1]['history'][-1][1])  # the follow-up sees the card
        self.assertEqual(self.last_text(), 'F101, F102 and F103 are in block F.')

    async def test_events_guide_and_documents_cards(self):
        self.script(call('get_campus_events'), say('All published events have already passed.'))
        await self.text('Are there any upcoming campus events?')
        self.assertIn('All published events have already passed.', self.all_sent())
        self.assertTrue(any(str(b).startswith('ev:') for row in self.buttons_of('Campus events') for b in row))

        self.script(call('get_registration_guide'), say('Here is the guide.'))
        await self.text('How do I register for courses?')
        self.assertIn('https://my.sdu.edu.kz/', [b for row in self.last_buttons() for _, b in row])

        self.script(call('get_required_documents', track='grant'), say('Here is the official list.'))
        await self.text('What documents do I need to submit?')
        card = self.last_text()
        self.assertIn('UNT certificate', card)
        self.assertIn('https://sdu.edu.kz/en/admission-3-2/', card)
        self.assertEqual(self.last_buttons(), [[('📄 Submit documents', 'menu:documents')]])

    async def test_personal_data_is_not_shown_in_a_group(self):
        self.app.user_data[-100]['lang'] = 'en'
        self.app.user_data[-100]['portal'] = {'student_id': '240103149', 'cookies': {}}
        fetch = AsyncMock(return_value=transcript())
        self.script(call('get_my_grades'), say('I can show grades only in a private chat.'))
        with patch.object(grade_bot.grade, 'fetch_transcript', new=fetch):
            await self.text('Show me my grades', user_id=-100, chat_type='group')
        fetch.assert_not_awaited()
        self.assertIn('only in a private chat with the bot', self.all_sent())
        self.assertNotIn('pending_ask', self.app.user_data[-100])

    async def test_asking_to_log_in_opens_the_sign_in(self):
        self.script(call('sign_in', account='moodle'))
        await self.text('Log me in to Moodle')
        self.assertIn('Sign in to Moodle (moodle.sdu.edu.kz)', self.last_text())
        self.assertEqual(self.last_buttons()[0][0], ('🔐 Sign in', 'auth:moodle'))
        self.assertNotIn('pending_ask', self.app.user_data[42])  # nothing to answer afterwards

    async def test_connection_error_is_reported(self):
        self.script(*[httpx.ConnectError('down')] * 10)
        with patch.object(core, 'RESPONSE_BUDGET', 0.3), patch.object(core, 'RETRY_PAUSE', 0.01):
            await self.text('What is GPA?')
        self.assertTrue(self.last_text().startswith('Connection error'))

    async def test_grade_command_shows_grades_right_after_sign_in(self):
        await self.text('/grade')
        await self.tap('grade:register')
        await self.text('240103149')
        with patch.object(grade_bot.grade, 'PortalLogin', FakePortal), \
             patch.object(grade_bot.grade, 'fetch_transcript', new=AsyncMock(return_value=transcript())):
            await self.text('secret-password')
        self.assertIn('✅', self.last_edit())  # "linked"
        self.assertIn('<b>CSS 217</b>', self.last_text())  # ...and the grades, without /grade again
        self.assertEqual(self.model.chats, [])  # commands do not go through the AI


if __name__ == '__main__':
    unittest.main()
