"""Published campus events, read from the existing knowledge document.

No AI request is required: dates/status are computed locally and published
facts are displayed verbatim in their source language.
"""
from __future__ import annotations

from datetime import datetime, date
from pathlib import Path
import re

from telegram.ext import CommandHandler

import core
import i18n
from rooms import CAMPUS_TZ
from telegram_bot import add_command, reply, user_lang

EVENTS_FILE = core.KNOWLEDGE_DIR / "campus_events.md"
TEXTS = i18n.by_lang("events")  # locales/<lang>.json, section "events"


def render_events(source: str, lang: str, today: date | None = None) -> str:
    lang = core.normalize_lang(lang)
    labels = TEXTS[lang]
    today = today or datetime.now(CAMPUS_TZ).date()
    sections = re.split(r"^##\s+", source, flags=re.MULTILINE)[1:]
    if not sections:
        return labels["missing"]
    lines = [f"# {labels['title']}", labels["source"]]
    for section in sections:
        heading, _, body = section.strip().partition("\n")
        match = re.search(r"^- (?:Event date|Date):\s*(.+)$", body, re.MULTILINE)
        status = ""
        if match:
            raw = re.sub(r"\s*\([^)]*\)", "", match.group(1))
            raw = re.sub(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+", "", raw)
            try:
                event_date = datetime.strptime(raw.strip(), "%B %d, %Y").date()
                key = "past" if event_date < today else "today" if event_date == today else "upcoming"
                status = f" · {labels[key]}"
            except ValueError:
                pass  # Retain the published date even if a future format changes.
        lines += ["", f"## {heading}{status}", body.strip()]
    return "\n".join(lines)


async def cmd_events(update, context):
    lang = user_lang(update, context)
    try:
        source = EVENTS_FILE.read_text(encoding="utf-8")
    except OSError:
        await reply(update.effective_message, TEXTS[lang]["missing"])
        return
    await reply(update.effective_message, render_events(source, lang))


def register_events(app):
    add_command("events",
        {lang: texts["menu"] for lang, texts in TEXTS.items()},
        {lang: texts["help_line"] for lang, texts in TEXTS.items()})
    app.add_handler(CommandHandler("events", cmd_events))
