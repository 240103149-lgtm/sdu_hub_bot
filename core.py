"""Shared core of the University AI Knowledge Hub.

Both entry points import from here, so the knowledge base, the prompt, the
AI calls and the localized error messages live in exactly one place:

    main.py          -> the website / REST API (FastAPI)
    telegram_bot.py  -> the Telegram bot (US5, US3, US6)

Nothing in this module knows about HTTP or about Telegram - nor about any
particular AI company: ai.py hides whether Gemini, DeepSeek or another model
answers (AI_PROVIDER in .env).
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from dotenv import load_dotenv

import ai

load_dotenv()

# ---------------------------------------------------------------- paths ----

BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
STATIC_DIR = BASE_DIR / "static"
GUIDE_FILE = KNOWLEDGE_DIR / "registration_guide.md"  # default (Kazakh) guide

# --------------------------------------------------------------- config ----

# US5: "the chatbot must generate and show a response within 5 seconds".
# SLOW_RESPONSE is the target we measure against (every answer is timed and
# logged); RESPONSE_BUDGET is the hard cut-off after which we stop retrying
# and show the student an error instead of leaving them waiting.
SLOW_RESPONSE = float(os.getenv("SLOW_RESPONSE_SECONDS", "5"))
RESPONSE_BUDGET = float(os.getenv("RESPONSE_BUDGET_SECONDS", "20"))

MAX_QUESTION_LENGTH = 2000
HISTORY_TURNS = 10          # how many past messages are sent back to the model

# Fail at startup, with a clear message, if .env lacks the chosen provider's key.
ai.chain()

# ---------------------------------------------------------- localization ----

LANGS = {"kk": "Kazakh", "ru": "Russian", "en": "English"}
DEFAULT_LANG = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "empty": {
        "kk": "Сұрақ бос болмауы керек.",
        "ru": "Вопрос не должен быть пустым.",
        "en": "The question can't be empty.",
    },
    "too_long": {
        "kk": "Сұрақ тым ұзын (ең көбі 2000 таңба).",
        "ru": "Вопрос слишком длинный (не более 2000 символов).",
        "en": "The question is too long (2000 characters max).",
    },
    "rate_limit": {
        "kk": "Сұраулар лимитіне жеттік (тегін тариф). Бір минут күтіп, қайта жіберіңіз.",
        "ru": "Достигнут лимит запросов (бесплатный тариф). Подождите минуту и отправьте снова.",
        "en": "Request limit reached (free tier). Wait a minute and try again.",
    },
    "busy": {
        "kk": "Жасанды интеллект қазір жүктемеге ұшырап тұр. Бірнеше секундтан кейін қайта жіберіңіз.",
        "ru": "Искусственный интеллект сейчас перегружен. Отправьте вопрос снова через несколько секунд.",
        "en": "Artificial intelligence is under heavy load right now. Try again in a few seconds.",
    },
    # US5: "a user must be warned with a Connection error message if the AI
    # server is unreachable".
    "connection": {
        "kk": "Қосылым қатесі: AI серверіне жете алмадық. Интернетті тексеріп, қайталап көріңіз.",
        "ru": "Ошибка соединения: не удалось связаться с AI-сервером. Проверьте интернет и попробуйте снова.",
        "en": "Connection error: the AI server is unreachable. Check your internet and try again.",
    },
    "timeout": {
        "kk": "Жауап тым ұзақ дайындалды. Сұрағыңызды қайта жіберіп көріңіз.",
        "ru": "Ответ готовился слишком долго. Попробуйте отправить вопрос ещё раз.",
        "en": "The answer took too long. Please send your question again.",
    },
    "api_error": {
        "kk": "Жасанды интеллектке қосыла алмадық. .env-тегі API кілті мен модель атауын тексеріңіз "
        "(нақты себебі терминалда жазылған).",
        "ru": "Не удалось подключиться к искусственному интеллекту. Проверьте API-ключ и название модели в .env "
        "(точная причина указана в терминале).",
        "en": "Couldn't connect to artificial intelligence. Check the API key and model name in .env "
        "(the exact cause is in the terminal).",
    },
    "unexpected": {
        "kk": "Күтпеген қате шықты (нақты себебі терминалда жазылған).",
        "ru": "Произошла непредвиденная ошибка (точная причина указана в терминале).",
        "en": "An unexpected error occurred (the exact cause is in the terminal).",
    },
    "no_answer": {
        "kk": "Жауап бере алмадым. Сұрағыңызды басқаша құрастырып көріңіз.",
        "ru": "Не удалось подготовить ответ. Попробуйте переформулировать вопрос.",
        "en": "I couldn't produce an answer. Try rephrasing your question.",
    },
    "guide_missing": {
        "kk": "Нұсқаулық әлі жарияланбаған. Тіркеу бөлімімен (Registrar's office) хабарласыңыз.",
        "ru": "Инструкция пока не опубликована. Обратитесь в отдел регистрации (Registrar's office).",
        "en": "The guide has not been published yet. Please contact the Registrar's office.",
    },
    "schedule_missing": {
        "kk": "Сабақ кестесі жүктелмеді, сондықтан бос кабинеттерді көрсете алмаймын "
        "(нақты себебі терминалда жазылған).",
        "ru": "Расписание занятий не загружено, поэтому не могу показать свободные аудитории "
        "(точная причина указана в терминале).",
        "en": "The class schedule isn't loaded, so I can't show free rooms "
        "(the exact cause is in the terminal).",
    },
}


def normalize_lang(value: Any, default: str = DEFAULT_LANG) -> str:
    """Turn anything ('ru', 'ru-RU', None, 'Қазақша') into a supported code."""
    code = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else default


def msg(key: str, lang: str) -> str:
    return MESSAGES[key].get(normalize_lang(lang), MESSAGES[key][DEFAULT_LANG])


KAZAKH_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
KAZAKH_WORDS = (
    "қашан", "қайда", "қалай", "қандай", "қанша", "кашан", "кайда", "калай",
    "бар ма", "болады", "болды", "ма?", "ме?", "сәлем", "рахмет", "туралы",
)


def detect_lang(text: str, default: str = DEFAULT_LANG) -> str:
    """Language of the student's question: 'kk', 'ru' or 'en'.

    Decided in code (not by the model) so the answer always follows the
    language the question was written in. `default` (the interface language)
    is used only when the text has no letters to judge by.
    """
    sample = text or ""
    lowered = sample.lower()
    cyr = sum(1 for ch in sample if "а" <= ch.lower() <= "я" or ch.lower() == "ё" or ch in KAZAKH_LETTERS)
    lat = sum(1 for ch in sample if "a" <= ch.lower() <= "z")
    if any(ch in KAZAKH_LETTERS for ch in sample) or (
        cyr and any(word in lowered for word in KAZAKH_WORDS)
    ):
        return "kk"
    if cyr >= 3 and cyr * 2 >= lat:
        return "ru"
    if lat >= 2:
        return "en"
    return normalize_lang(default)


class AssistantError(Exception):
    """A failure we can explain to the student in their own language.

    `key` is a MESSAGES key, so every caller (web or Telegram) shows the same
    wording without duplicating any text.
    """

    def __init__(self, key: str, status_code: int = 502) -> None:
        super().__init__(key)
        self.key = key
        self.status_code = status_code

    def localized(self, lang: str) -> str:
        return msg(self.key, lang)


# --------------------------------------------------------------- prompt ----

SYSTEM_PROMPT = """You are the University Knowledge Hub assistant for students.

Rules:
- Today's date is {today}. When a student asks about events, compare the event date
  with today's date: if the event is earlier than today, say clearly that it has
  already passed; if it is today or later, say it is upcoming.
- Answer ONLY using the knowledge base below. Do not guess dates, deadlines,
  prerequisites or any other facts that are not written there.
- The documents may be written in Russian, Kazakh or English. Understand them in
  any of these languages and translate the relevant facts into the answer language.
- LANGUAGE RULE (STRICT, HIGHEST PRIORITY): The student's question is written in
  "{ui_language}". Write the ENTIRE answer in "{ui_language}", including fallback
  answers like "I don't have this information". Ignore the language of the
  knowledge base documents and of earlier messages in the conversation.
- FOCUS RULE: Answer only what the student asked, in 1-3 short sentences. Do not
  add other events, background details, tips, or suggestions to check Instagram
  unless the student asked for them. Do not mention today's date unless it is
  needed (for example to say an event has already passed). Answer only about the
  event the student named. If the name matches more than one event (for example
  "Welcome Party" matches both the freshmen Welcome Party and the International
  Students Welcome Party), give a short answer for each matching event, with its
  date, time and venue, and say whether it is upcoming or has passed. If the
  student specifies one of them (for example "international"), answer only about
  that one. Do not mention unrelated events.
- Terminology: at this university, "course registration" is usually called making
  (creating) the schedule - in Kazakh "расписание құру" or "сабақ кестесін құру",
  in Russian "составление расписания". Treat these phrases as the same thing and use
  the wording the student used. The menu item in the student portal is named "Course Registration".
- Free (empty) classrooms are not in the knowledge base: a separate feature works
  them out from the class schedule. If the student asks which rooms are free, do
  not guess - point them to the /rooms command in the Telegram bot or the
  "Free rooms" tab on the website (Kazakh: "Бос кабинеттер", Russian: "Свободные аудитории").
- If the answer is not in the knowledge base, say so clearly and suggest
  contacting the relevant university office (for example, the Registrar's office).
- Be concise. For procedures, give short numbered steps. Use plain text;
  you may use **bold** for key words, but no tables or headings.

KNOWLEDGE BASE:
{knowledge}
"""

# ------------------------------------------------- knowledge base (RAG) ----

KNOWLEDGE_SUFFIXES = {".md", ".txt", ".docx"}
# Notes for us, not facts for students: never fed to the model.
KNOWLEDGE_IGNORED = {"readme.md", "readme.txt"}
_doc_cache: dict[Path, tuple[float, str]] = {}


def read_docx(path: Path) -> str:
    """Text of a Word file: paragraphs and tables, in document order."""
    doc = Document(str(path))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, doc).text.strip()
            if text:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = cell.text.strip()
                    if value and (not cells or cells[-1] != value):  # skip merged duplicates
                        cells.append(value)
                if cells:
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def read_document(path: Path) -> str:
    """Read a document, re-reading it only when the file has changed on disk."""
    mtime = path.stat().st_mtime
    cached = _doc_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    text = read_docx(path) if path.suffix.lower() == ".docx" else path.read_text(encoding="utf-8")
    _doc_cache[path] = (mtime, text)
    return text


def knowledge_files() -> list[Path]:
    """Every approved document under knowledge/, subfolders included."""
    if not KNOWLEDGE_DIR.exists():
        return []
    return sorted(
        path
        for path in KNOWLEDGE_DIR.rglob("*")
        if path.is_file()
        and not path.name.startswith("~$")           # Word lock files
        and not path.name.startswith("_")            # drafts you park here
        and path.name.lower() not in KNOWLEDGE_IGNORED
        and path.suffix.lower() in KNOWLEDGE_SUFFIXES
    )


def load_knowledge() -> str:
    """The approved University Knowledge Base, as one block of text.

    Files are re-read when they change, so edits apply without restarting
    the server or the bot.
    """
    parts: list[str] = []
    for path in knowledge_files():
        try:
            parts.append(f"### Document: {path.name}\n{read_document(path)}")
        except Exception as exc:  # noqa: BLE001 - a broken file must not stop the bot
            print(f"[core] could not read {path.name}: {exc}")
    return "\n\n".join(parts) if parts else "(knowledge base is empty)"


def load_guide(lang: str) -> str | None:
    """US3: the registration guide for one language, or None if not published.

    Uses knowledge/registration_guide.<lang>.md when it exists, otherwise the
    default knowledge/registration_guide.md.
    """
    lang = normalize_lang(lang, default="kk")
    for path in (KNOWLEDGE_DIR / f"registration_guide.{lang}.md", GUIDE_FILE):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


# ---------------------------------------------------------------- answer ----


def _as_turn(turn: Any) -> tuple[str, str]:
    """Accept dicts, pydantic models or anything with .role / .text."""
    if isinstance(turn, dict):
        role, text = turn.get("role"), turn.get("text")
    else:
        role, text = getattr(turn, "role", None), getattr(turn, "text", None)
    return ("user" if role == "user" else "assistant"), str(text or "").strip()


def answer(message: str, history: Iterable[Any] | None = None, lang: str = DEFAULT_LANG) -> str:
    """US5: answer one student question from the approved knowledge base.

    Raises AssistantError with a key the caller turns into a localized message.
    """
    lang = normalize_lang(lang)
    text = (message or "").strip()
    if not text:
        raise AssistantError("empty", status_code=400)
    if len(text) > MAX_QUESTION_LENGTH:
        raise AssistantError("too_long", status_code=400)

    messages: list[ai.Message] = []
    for turn in list(history or [])[-HISTORY_TURNS:]:
        role, turn_text = _as_turn(turn)
        if turn_text:
            messages.append(ai.Message(role, turn_text))
    messages.append(ai.Message("user", text))

    # Today's date (Almaty time) so the model can tell passed events from upcoming ones.
    today = datetime.now(ZoneInfo("Asia/Almaty")).strftime("%A, %d %B %Y")
    reply_lang = detect_lang(text, lang)  # the language the question is written in
    system_prompt = SYSTEM_PROMPT.format(
        ui_language=LANGS[reply_lang], knowledge=load_knowledge(), today=today
    )

    started = time.monotonic()
    try:
        reply = ai.generate(system_prompt, messages, deadline=started + RESPONSE_BUDGET)
    except ai.ProviderError as exc:  # its kind is a MESSAGES key: rate_limit, busy, ...
        if exc.kind == "unexpected":
            print(f"[core] unexpected error: {exc}")
        raise AssistantError(exc.kind) from exc

    elapsed = time.monotonic() - started
    flag = "  <-- slower than the US5 target" if elapsed > SLOW_RESPONSE else ""
    print(f"[core] answered in {elapsed:.1f}s (target {SLOW_RESPONSE:.0f}s){flag}")

    return reply.strip() or msg("no_answer", reply_lang)


async def answer_async(
    message: str, history: Iterable[Any] | None = None, lang: str = DEFAULT_LANG
) -> str:
    """`answer` for async callers: the blocking call runs in a worker thread,
    so the Telegram bot keeps serving other students while the model thinks."""
    return await asyncio.to_thread(answer, message, history, lang)
