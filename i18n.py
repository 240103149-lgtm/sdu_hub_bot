"""Interface texts of the bot and the website (US6), from locales/<lang>.json.

Every language file has the same sections and keys:

    core       errors shared by the bot and the website (core.py)
    bot        /start, /help and other main texts (telegram_bot.py)
    commands   the Telegram command menu        home   the /start buttons
    accounts, grade, deadline, rooms, events, documents, login
               the texts of each feature module
    web        the website (static/index.html)

To change a wording, edit the JSON files only - no Python is needed.
tests/test_i18n.py checks that all three files have the same keys and the
same {placeholders}, so a missing translation is caught before the demo.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).parent / "locales"
LANGS = ("kk", "ru", "en")
FALLBACK = "en"


class LocaleError(RuntimeError):
    """A locale file is missing, is not valid JSON or lacks a section."""


@lru_cache(maxsize=None)
def _load(lang: str) -> dict[str, Any]:
    path = LOCALES_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocaleError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocaleError(f"{path}: the file must contain a JSON object")
    return data


def section(lang: str, name: str) -> dict[str, Any]:
    """One section of one language, as a private copy the caller may change."""
    data = _load(lang)
    if name not in data:
        raise LocaleError(f"locales/{lang}.json has no {name!r} section")
    return copy.deepcopy(data[name])


def by_lang(name: str) -> dict[str, dict[str, Any]]:
    """{"kk": {key: text}, "ru": {...}, "en": {...}}"""
    return {lang: section(lang, name) for lang in LANGS}


def by_key(name: str) -> dict[str, dict[str, Any]]:
    """{key: {"kk": text, "ru": text, "en": text}} - the shape most modules use."""
    tables = by_lang(name)
    return {
        key: {lang: tables[lang][key] for lang in LANGS if key in tables[lang]}
        for key in tables[FALLBACK]
    }
