"""US8 Document Submission: offline tests (no Telegram, portal or AI calls).

QA scenarios from the user-story sheet (US8QATest):
  1. All required documents uploaded in the correct format -> "Submission successful"
  2. A required document is missing -> "Please upload the missing document", blocked
plus the test scripts: invalid format, verification (pass) and "under review" (deferred).
"""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from telegram import Chat, Message, Update, User
from telegram.ext import ExtBot

import documents
import documents_bot
import deadline_bot
import grade_bot
import telegram_bot

GRANT_REQUIRED = ['application_form', 'school_certificate', 'unt_certificate', 'medical_075',
                  'fluorography', 'vaccination_063', 'photo_3x4', 'student_fee_receipt', 'grant_certificate']


class CatalogTests(unittest.TestCase):
    def test_official_checklist_loads_for_both_tracks(self):
        catalog = documents.load_catalog()
        self.assertEqual(catalog.source, 'https://sdu.edu.kz/en/admission-3-2/')
        self.assertEqual([d.id for d in catalog.required('grant')], GRANT_REQUIRED)
        paid = [d.id for d in catalog.required('paid')]
        self.assertIn('tuition_receipt', paid)
        self.assertNotIn('grant_certificate', paid)
        self.assertEqual(len(paid), 9)
        optional = [d.id for d in catalog.for_track('paid') if not d.required]
        self.assertEqual(optional, ['english_certificate', 'pedagogical_exam', 'interview_protocol', 'military_id'])
        self.assertEqual(catalog.get('photo_3x4').formats, ('jpg', 'png'))
        for doc in catalog.documents:
            for lang in ('kk', 'ru', 'en'):
                self.assertTrue(doc.names.get(lang) and doc.shorts.get(lang), (doc.id, lang))
                if not doc.required:
                    self.assertTrue(doc.when.get(lang), (doc.id, lang))

    def test_malformed_checklist_is_refused(self):
        base = {'tracks': [{'id': 'grant', 'name': {'en': 'Grant'}}],
                'documents': [{'id': 'x', 'tracks': ['grant'], 'formats': ['pdf'], 'name': {'en': 'X'}}]}
        documents.parse_catalog(base)
        for broken in (
            {**base, 'documents': [{**base['documents'][0], 'formats': ['docx']}]},
            {**base, 'documents': [{**base['documents'][0], 'tracks': ['other']}]},
            {**base, 'documents': base['documents'] * 2},
            {**base, 'documents': [{**base['documents'][0], 'id': 'Bad Id!'}]},
        ):
            with self.assertRaises(ValueError):
                documents.parse_catalog(broken)
        with patch.object(documents, 'CATALOG_FILE', Path(tempfile.gettempdir()) / 'no-such-file.json'):
            with self.assertRaises(documents.CatalogError):
                documents.load_catalog()


class ValidationTests(unittest.TestCase):
    def test_file_format_needs_matching_extension_and_mime(self):
        cases = [
            ('scan.pdf', 'application/pdf', 'pdf'),
            ('Photo.JPEG', 'image/jpeg', 'jpg'),
            ('photo.png', None, 'png'),
            (None, 'image/png', 'png'),
            ('scan.pdf', 'application/octet-stream', 'pdf'),
            ('essay.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', None),
            ('picture.pdf', 'image/png', None),   # a picture renamed to .pdf
            ('noextension', 'application/zip', None),
        ]
        for name, mime, expected in cases:
            self.assertEqual(documents.file_format(name, mime), expected, (name, mime))

    def test_rules_per_document(self):
        catalog = documents.load_catalog()
        photo, unt = catalog.get('photo_3x4'), catalog.get('unt_certificate')
        limit = catalog.max_bytes
        self.assertEqual(documents.check_file(photo, 'photo.pdf', 'application/pdf', 1000, limit), 'bad_format')
        self.assertIsNone(documents.check_file(photo, 'photo.png', 'image/png', 1000, limit))
        self.assertIsNone(documents.check_file(unt, 'unt.pdf', 'application/pdf', limit, limit))
        self.assertEqual(documents.check_file(unt, 'unt.pdf', 'application/pdf', limit + 1, limit), 'too_large')
        self.assertEqual(documents.describe_format('essay.docx', None), 'DOCX')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = documents.DocumentStore(Path(self.temp.name) / 'documents.db')
        self.catalog = documents.load_catalog()

    def tearDown(self):
        self.temp.cleanup()

    def save(self, doc_type, name='scan.pdf'):
        return self.db.save(self.catalog, '240103149', 42, doc_type, kind='document', file_id='f-' + doc_type,
                            file_name=name, mime_type='application/pdf', file_size=1000)

    def test_submit_is_blocked_until_every_required_document_is_there(self):
        self.db.start(self.catalog, '240103149', 42, 'grant')
        self.save('unt_certificate')
        result = self.db.submit(self.catalog, '240103149', 42)
        self.assertEqual([d.id for d in result.missing], [d for d in GRANT_REQUIRED if d != 'unt_certificate'])
        self.assertEqual(result.sent, [])
        self.assertEqual(result.submission.status, documents.DRAFT)
        self.assertEqual(result.submission.documents['unt_certificate'].status, documents.UPLOADED)

        for doc in GRANT_REQUIRED:
            if doc != 'unt_certificate':
                self.save(doc)
        result = self.db.submit(self.catalog, '240103149', 42)
        self.assertEqual(result.missing, [])
        self.assertEqual(len(result.sent), 9)
        self.assertEqual(result.submission.status, documents.UNDER_REVIEW)
        self.assertTrue(result.submission.submitted_at)
        self.assertEqual(len(self.db.pending_reviews()), 9)
        again = self.db.submit(self.catalog, '240103149', 42)
        self.assertEqual(again.sent, [])  # nothing new

    def test_review_reject_reupload_and_verify(self):
        self.db.start(self.catalog, '240103149', 42, 'grant')
        for doc in GRANT_REQUIRED:
            self.save(doc)
        sent = {d.doc_type: d for d in self.db.submit(self.catalog, '240103149', 42).sent}

        rejected = self.db.review(self.catalog, sent['photo_3x4'].id, verified=False, reviewer='staff', reason='unreadable')
        self.assertEqual(rejected.submission.status, documents.NEEDS_CORRECTION)
        self.assertEqual(rejected.document.reason, 'unreadable')
        self.assertIsNone(self.db.review(self.catalog, sent['photo_3x4'].id, verified=True, reviewer='staff'))
        self.assertEqual([d.id for d in self.db.submit(self.catalog, '240103149', 42).missing], ['photo_3x4'])

        self.save('photo_3x4', 'new.pdf')
        self.assertEqual(self.db.get('240103149').status, documents.DRAFT)
        resent = self.db.submit(self.catalog, '240103149', 42).sent
        self.assertEqual([d.doc_type for d in resent], ['photo_3x4'])

        completed = []
        for doc in [sent[t] for t in GRANT_REQUIRED if t != 'photo_3x4'] + resent:
            result = self.db.review(self.catalog, doc.id, verified=True, reviewer='staff')
            completed.append(result.completed)
        self.assertEqual(completed, [False] * 8 + [True])
        self.assertEqual(self.db.get('240103149').status, documents.VERIFIED)

    def test_replaced_file_cannot_be_reviewed_and_track_change_recomputes(self):
        self.db.start(self.catalog, '240103149', 42, 'grant')
        for doc in GRANT_REQUIRED:
            self.save(doc)
        old = {d.doc_type: d for d in self.db.submit(self.catalog, '240103149', 42).sent}
        self.save('unt_certificate', 'better.pdf')  # replaced while under review
        self.assertIsNone(self.db.review(self.catalog, old['unt_certificate'].id, verified=True, reviewer='staff'))
        submission = self.db.start(self.catalog, '240103149', 42, 'paid')
        self.assertEqual(submission.status, documents.DRAFT)
        self.assertEqual([d.id for d in documents.missing_required(self.catalog, 'paid', submission.documents)],
                         ['tuition_receipt'])
        with self.assertRaises(ValueError):
            self.db.start(self.catalog, '240103149', 42, 'unknown')
        with self.assertRaises(LookupError):
            self.db.save(self.catalog, 'nobody', 1, 'unt_certificate', kind='document', file_id='x',
                         file_name='x.pdf', mime_type='application/pdf', file_size=1)


class FakePortalLogin:
    """Stands in for grade.PortalLogin: the portal accepts the password, no 2FA."""

    def __init__(self, *args, **kwargs):
        pass

    async def submit_credentials(self, student_id, password):
        return False

    def cookies(self):
        return {'session': 'portal-cookie'}

    async def close(self):
        pass


class FlowTests(unittest.IsolatedAsyncioTestCase):
    """Real Telegram updates through the real Application, as in test_unified_bot."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = []

        def mocked(name, **kwargs):
            p = patch.object(ExtBot, name, **kwargs)
            self.patches.append(p)
            return p.start()

        async def initialize(bot):
            bot._bot_user = User(999, 'Hub', True, username='hub_test_bot')

        mocked('initialize', new=initialize)
        mocked('shutdown', new=AsyncMock())
        message = Message(1, datetime.now(timezone.utc), Chat(42, 'private'), text='reply')
        self.send = mocked('send_message', new=AsyncMock(return_value=message))
        self.edit = mocked('edit_message_text', new=AsyncMock(return_value=message))
        self.answer = mocked('answer_callback_query', new=AsyncMock(return_value=True))
        mocked('delete_message', new=AsyncMock(return_value=True))
        mocked('send_chat_action', new=AsyncMock(return_value=True))
        mocked('set_my_commands', new=AsyncMock(return_value=True))
        mocked('delete_my_commands', new=AsyncMock(return_value=True))
        mocked('get_me', new=AsyncMock(return_value=User(999, 'Hub', True, username='hub_test_bot')))
        self.send_document = mocked('send_document', new=AsyncMock(return_value=message))
        self.send_photo = mocked('send_photo', new=AsyncMock(return_value=message))
        self.edit_caption = mocked('edit_message_caption', new=AsyncMock(return_value=message))
        self.edit_markup = mocked('edit_message_reply_markup', new=AsyncMock(return_value=message))
        for p in (
            patch.object(telegram_bot, 'STATE_FILE', Path(self.temp.name) / 'state.pickle'),
            patch.object(documents, 'DB_PATH', Path(self.temp.name) / 'documents.db'),
            patch.dict(documents._stores, clear=True),
            patch.dict(os.environ, {'DOCUMENTS_REVIEWER_IDS': '777'}),
            patch.object(telegram_bot.core, 'answer_async', new=AsyncMock(return_value='AI answer')),
        ):
            self.patches.append(p)
            started = p.start()
            if isinstance(started, AsyncMock):
                self.ai = started
        self.app = telegram_bot.build_application()
        message.set_bot(self.app.bot)
        await self.app.initialize()
        self.app.user_data[42]['lang'] = 'en'
        self.errors = []

        async def error_handler(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(error_handler)
        self.counter = 0

    async def asyncTearDown(self):
        self.assertEqual(self.errors, [])
        await self.app.shutdown()
        for p in reversed(self.patches):
            p.stop()
        deadline_bot.ACTIVE.clear()
        grade_bot.PENDING.clear()
        self.temp.cleanup()

    # ------------------------------------------------------------ helpers ----

    def user(self, user_id):
        return {'id': user_id, 'is_bot': False, 'first_name': 'Staff' if user_id == 777 else 'Student'}

    async def process(self, payload):
        self.counter += 1
        payload['update_id'] = self.counter
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    def message(self, user_id=42, chat_type='private', **fields):
        return {'message_id': self.counter + 1000, 'date': int(time.time()),
                'chat': {'id': user_id, 'type': chat_type}, 'from': self.user(user_id), **fields}

    async def text(self, text, user_id=42, chat_type='private'):
        fields = {'text': text}
        if text.startswith('/'):
            fields['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
        await self.process({'message': self.message(user_id, chat_type, **fields)})

    async def tap(self, data, user_id=42, chat_type='private', **message_fields):
        message = self.message(user_id, chat_type, **(message_fields or {'text': 'menu'}))
        await self.process({'callback_query': {'id': str(self.counter), 'from': self.user(user_id),
                                               'chat_instance': 'test', 'data': data, 'message': message}})

    async def upload(self, name='scan.pdf', mime='application/pdf', size=50_000, photo=False):
        n = self.counter
        if photo:
            sizes = [{'file_id': f'small-{n}', 'file_unique_id': f's{n}', 'width': 90, 'height': 120, 'file_size': 900},
                     {'file_id': f'photo-{n}', 'file_unique_id': f'p{n}', 'width': 900, 'height': 1200, 'file_size': size}]
            await self.process({'message': self.message(photo=sizes)})
        else:
            document = {'file_id': f'file-{n}', 'file_unique_id': f'u{n}', 'file_name': name,
                        'mime_type': mime, 'file_size': size}
            await self.process({'message': self.message(document=document)})

    def sent_texts(self, chat_id=None):
        return [c.kwargs.get('text', '') for c in self.send.call_args_list
                if chat_id is None or c.kwargs.get('chat_id') == chat_id]

    def last_text(self):
        return self.sent_texts()[-1]

    def link_portal(self, student_id='240103149'):
        self.app.user_data[42]['portal'] = {'student_id': student_id, 'cookies': {}}

    async def upload_all(self, required):
        for doc in required:
            await self.tap(f'docs:up:{doc}')
            if doc == 'photo_3x4':
                await self.upload(photo=True)
            else:
                await self.upload(name=f'{doc}.pdf')

    def submission(self):
        return documents.store().get('240103149')

    # -------------------------------------------------------------- tests ----

    async def test_sign_in_first_then_checklist_opens(self):
        await self.text('/documents')
        self.assertIn('first sign in to the university portal', self.last_text())
        buttons = self.send.call_args.kwargs['reply_markup'].inline_keyboard[0]
        self.assertEqual([b.callback_data for b in buttons], ['docs:signin', 'docs:nosignin'])
        self.assertEqual(self.app.user_data[42]['login_flow'], 'documents')

        await self.tap('docs:signin')
        self.assertIn('student number', self.edit.call_args.kwargs['text'])
        await self.text('240103149')
        with patch.object(grade_bot.grade, 'PortalLogin', FakePortalLogin):
            await self.text('secret-password')
        self.assertEqual(self.app.user_data[42]['portal']['student_id'], '240103149')
        self.assertNotIn('login_flow', self.app.user_data[42])
        self.assertNotIn('secret-password', repr(dict(self.app.user_data[42])))
        self.assertIn('How are you enrolling?', self.last_text())  # the checklist continues by itself
        keyboard = self.send.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.callback_data for b in keyboard[0]], ['docs:track:grant', 'docs:track:paid'])
        self.ai.assert_not_awaited()

    async def test_declining_sign_in_and_group_chats(self):
        await self.text('/documents')
        await self.tap('docs:nosignin')
        self.assertIn('Send /documents', self.edit.call_args.kwargs['text'])
        self.assertNotIn('login_flow', self.app.user_data[42])
        self.app.user_data[-100]['lang'] = 'en'
        await self.text('/documents', user_id=-100, chat_type='group')
        self.assertIn('private chat', self.last_text())
        await self.tap('menu:documents', user_id=-100, chat_type='group')
        self.assertIn('private chat', self.last_text())
        self.assertNotIn('login_flow', self.app.user_data[-100])
        self.ai.assert_not_awaited()

    async def test_qa_scenario_2_missing_document_blocks_submission(self):
        self.link_portal()
        await self.tap('menu:documents')
        self.assertIn('How are you enrolling?', self.last_text())
        await self.tap('docs:track:grant')
        board = self.edit.call_args.kwargs
        self.assertIn('Status: draft', board['text'])
        self.assertIn('⬜ UNT certificate — missing', board['text'])
        self.assertIn('sdu.edu.kz/en/admission-3-2', board['text'])
        await self.tap('docs:up:unt_certificate')
        self.assertIn('Send «UNT certificate» as a file', self.last_text())
        await self.upload(name='unt.pdf')
        self.assertIn('✅ «UNT certificate» uploaded: unt.pdf', self.last_text())
        self.assertIn('📎 UNT certificate — uploaded, not submitted yet', self.last_text())

        await self.tap('docs:submit')
        warning = self.last_text()
        self.assertIn('Please upload the missing document', warning)
        self.assertIn('«School certificate with transcript»', warning)
        self.assertNotIn('UNT certificate', warning)
        upload_buttons = [row[0].callback_data for row in self.send.call_args.kwargs['reply_markup'].inline_keyboard]
        self.assertEqual(len(upload_buttons), 8)
        self.assertEqual(self.submission().status, documents.DRAFT)
        self.assertEqual(self.submission().documents['unt_certificate'].status, documents.UPLOADED)
        self.send_document.assert_not_awaited()

        self.app.user_data[42]['lang'] = 'kk'
        await self.tap('docs:submit')
        self.assertIn('Жетіспейтін құжатты жүктеңіз', self.last_text())

    async def test_qa_scenario_1_successful_submission_then_verification(self):
        self.link_portal()
        self.app.user_data[777]['lang'] = 'en'
        await self.text('/documents')
        await self.tap('docs:track:grant')
        await self.upload_all(GRANT_REQUIRED)
        self.assertEqual(self.submission().status, documents.DRAFT)

        await self.tap('docs:submit')
        submitted = [text for text in self.sent_texts(chat_id=42) if 'Submission successful' in text]
        self.assertEqual(len(submitted), 1)
        self.assertIn('Status: under review (since', submitted[0])  # deferred: waits for staff
        self.assertIn('⏳ UNT certificate — under review', submitted[0])
        self.assertEqual(self.submission().status, documents.UNDER_REVIEW)
        header = [c for c in self.send.call_args_list if c.kwargs.get('chat_id') == 777]
        self.assertIn('Student number: 240103149', header[0].kwargs['text'])
        self.assertEqual(self.send_document.await_count, 8)
        self.assertEqual(self.send_photo.await_count, 1)
        review_buttons = self.send_document.call_args.kwargs['reply_markup'].inline_keyboard[0]
        self.assertEqual([b.text for b in review_buttons], ['✅ Verify', '❌ Reject'])

        sent = sorted(self.submission().documents.values(), key=lambda d: d.id)
        for doc in sent:
            await self.tap(f'docr:ok:{doc.id}', user_id=777, caption='240103149 · State grant', document={
                'file_id': doc.file_id, 'file_unique_id': f'r{doc.id}'})
        texts = self.sent_texts()
        self.assertIn('✅ Your document «UNT certificate» has been verified.', texts)
        self.assertEqual(texts[-1], '🎉 All required documents are verified. Your document submission is complete!')
        self.assertIn('Verified · Staff', self.edit_caption.call_args.kwargs['caption'])
        self.assertEqual(self.submission().status, documents.VERIFIED)
        await self.text('/documents')
        self.assertIn('all required documents are verified', self.last_text())
        self.ai.assert_not_awaited()

    async def test_wrong_format_and_size_are_refused_and_nothing_is_stored(self):
        self.link_portal()
        await self.text('/documents')
        await self.tap('docs:track:paid')
        await self.tap('docs:up:photo_3x4')
        await self.upload(name='photo.pdf', mime='application/pdf')
        self.assertIn('«Photo 3×4 cm» was not accepted: wrong file format (PDF). Allowed: JPG, PNG', self.last_text())
        await self.upload(name='essay.docx', mime='application/msword')
        self.assertIn('wrong file format (DOCX)', self.last_text())
        await self.upload(name='huge.png', mime='image/png', size=11 * 1024 * 1024)
        self.assertIn('the file is too large (11 MB, the limit is 10 MB)', self.last_text())
        self.assertNotIn('photo_3x4', self.submission().documents)
        await self.upload(name='me.png', mime='image/png')  # the document is still chosen
        self.assertEqual(self.submission().documents['photo_3x4'].file_name, 'me.png')
        self.ai.assert_not_awaited()

    async def test_rejected_document_is_uploaded_again(self):
        self.link_portal()
        await self.text('/documents')
        await self.tap('docs:track:grant')
        await self.upload_all(GRANT_REQUIRED)
        await self.tap('docs:submit')
        unt = self.submission().documents['unt_certificate']

        await self.tap(f'docr:no:{unt.id}', user_id=777, caption='x', document={'file_id': 'a', 'file_unique_id': 'b'})
        reasons = [row[0].callback_data for row in self.edit_markup.call_args.kwargs['reply_markup'].inline_keyboard]
        self.assertEqual(reasons, [f'docr:no:{unt.id}:unreadable', f'docr:no:{unt.id}:wrong_document',
                                   f'docr:no:{unt.id}:invalid', f'docr:back:{unt.id}'])
        await self.tap(f'docr:no:{unt.id}:unreadable', user_id=777, caption='x',
                       document={'file_id': 'a', 'file_unique_id': 'b'})
        self.assertIn('«UNT certificate» was rejected: the file is unreadable', self.last_text())
        self.assertEqual(self.send.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data,
                         'docs:up:unt_certificate')
        self.assertEqual(self.submission().status, documents.NEEDS_CORRECTION)

        await self.tap('docs:up:unt_certificate')
        await self.upload(name='unt-clear.pdf')
        self.send_document.reset_mock()
        await self.tap('docs:submit')
        self.assertEqual(self.send_document.await_count, 1)  # only the new file goes to staff
        self.assertEqual(self.submission().status, documents.UNDER_REVIEW)
        # The old file's buttons no longer change anything.
        await self.tap(f'docr:ok:{unt.id}', user_id=777, caption='x', document={'file_id': 'a', 'file_unique_id': 'b'})
        self.assertTrue(self.answer.call_args.kwargs.get('show_alert'))
        self.assertEqual(self.submission().documents['unt_certificate'].status, documents.UNDER_REVIEW)

    async def test_only_admissions_staff_can_review(self):
        self.link_portal()
        await self.text('/documents')
        await self.tap('docs:track:grant')
        await self.upload_all(GRANT_REQUIRED)
        await self.tap('docs:submit')
        unt = self.submission().documents['unt_certificate']
        await self.tap(f'docr:ok:{unt.id}')  # the student tries to verify their own file
        self.assertEqual(self.answer.call_args.kwargs.get('text'), 'Only admissions staff can do this.')
        await self.text('/review')
        self.assertEqual(self.last_text(), 'Only admissions staff can do this.')
        self.assertEqual(self.submission().documents['unt_certificate'].status, documents.UNDER_REVIEW)

        self.send_document.reset_mock()
        self.send_photo.reset_mock()
        await self.text('/review', user_id=777)
        self.assertIn('Files waiting for review: 9', self.last_text())
        self.assertEqual(self.send_document.await_count + self.send_photo.await_count, 9)

    async def test_files_without_a_chosen_document_and_cancel(self):
        await self.upload(name='screenshot.png', mime='image/png')  # not using /documents
        self.assertEqual(self.last_text(), telegram_bot.text('not_text', 'en'))
        self.link_portal()
        await self.text('/documents')
        await self.tap('docs:track:grant')
        await self.upload(name='scan.pdf')
        self.assertIn('Which document is this?', self.last_text())
        self.assertEqual(self.submission().documents, {})

        await self.tap('docs:up:medical_075')
        await self.text('/cancel')
        self.assertEqual(self.last_text(), 'File upload cancelled.')
        self.assertNotIn('doc_pending', self.app.user_data[42])
        await self.upload(name='scan.pdf')
        self.assertIn('Which document is this?', self.last_text())

        await self.tap('docs:up:medical_075')
        self.app.user_data[42]['doc_pending']['at'] -= documents_bot.UPLOAD_TTL + 1
        await self.upload(name='late.pdf')
        self.assertIn('Which document is this?', self.last_text())
        self.ai.assert_not_awaited()

    async def test_documents_command_replaces_another_sign_in(self):
        await self.text('/grade')
        await self.tap('grade:register')
        await self.text('240103149')
        self.assertEqual(self.app.user_data[42]['grade_sid'], '240103149')
        await self.text('/documents')
        self.assertNotIn('grade_sid', self.app.user_data[42])
        self.assertEqual(self.app.user_data[42]['login_flow'], 'documents')
        await self.text('/cancel')
        self.assertIn('Send /documents', self.last_text())
        self.assertNotIn('login_flow', self.app.user_data[42])
        names = [name for name, _ in telegram_bot.COMMAND_LABELS['en']]
        self.assertIn('documents', names)
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('/documents', telegram_bot.text('help', 'kk'))
        self.ai.assert_not_awaited()

    async def test_missing_checklist_file_is_explained(self):
        self.link_portal()
        with patch.object(documents, 'CATALOG_FILE', Path(self.temp.name) / 'missing.json'):
            await self.text('/documents')
        self.assertIn('has not been published yet', self.last_text())
        self.assertNotIn('login_flow', self.app.user_data[42])


if __name__ == '__main__':
    unittest.main()
