"""US6: every interface text comes from locales/<lang>.json, in all three languages."""
import os
os.environ.setdefault('GEMINI_API_KEY', 'offline-test-key')
os.environ.setdefault('GEMINI_MODEL', 'offline-test-model')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:offline-test-token')

import json
import re
import unittest

import httpx

import i18n

PLACEHOLDER = re.compile(r'\{([a-z_]+)\}')


def walk(value, path=()):
    """(path, leaf) for every text or list of texts in a locale file."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, path + (key,))
    else:
        yield path, value


class LocaleFileTests(unittest.TestCase):
    def test_all_languages_have_the_same_keys_and_placeholders(self):
        files = {lang: json.loads((i18n.LOCALES_DIR / f'{lang}.json').read_text(encoding='utf-8')) for lang in i18n.LANGS}
        leaves = {lang: dict(walk(data)) for lang, data in files.items()}
        self.assertEqual(set(leaves['kk']), set(leaves['en']))
        self.assertEqual(set(leaves['ru']), set(leaves['en']))
        for path, english in leaves['en'].items():
            for lang in ('kk', 'ru'):
                other = leaves[lang][path]
                self.assertEqual(type(other), type(english), (lang, path))
                if isinstance(english, list):
                    self.assertEqual(len(other), len(english), (lang, path))
                elif isinstance(english, str):
                    self.assertTrue(other.strip(), (lang, path))
                    self.assertEqual(set(PLACEHOLDER.findall(other)), set(PLACEHOLDER.findall(english)), (lang, path))

    def test_modules_read_their_texts_from_the_files(self):
        import core
        import telegram_bot
        kk = json.loads((i18n.LOCALES_DIR / 'kk.json').read_text(encoding='utf-8'))
        self.assertEqual(core.msg('connection', 'kk'), kk['core']['connection'])
        self.assertEqual(telegram_bot.text('lang_set', 'kk'), kk['bot']['lang_set'])
        self.assertEqual(telegram_bot.home_keyboard('kk').inline_keyboard[0][0].text, kk['home']['deadline'])
        with self.assertRaises(i18n.LocaleError):
            i18n.section('kk', 'no_such_section')


class WebsiteTextsTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_receives_all_three_languages(self):
        import main
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.get('/')
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertNotIn('/*I18N*/null', page)
        self.assertIn(i18n.section('kk', 'web')['tabChat'], page)
        self.assertIn(i18n.section('ru', 'web')['send'], page)
        self.assertNotIn('</script><', page.split('const I18N = ', 1)[1].split(';\n', 1)[0])


if __name__ == '__main__':
    unittest.main()
