"""Shared core of the University AI Knowledge Hub.

Both entry points import from here, so the knowledge base, the prompt, the
Gemini calls and the localized error messages live in exactly one place:

    main.py          -> the website / REST API (FastAPI)
    telegram_bot.py  -> the Telegram bot (US5, US3, US6)

Nothing in this module knows about HTTP or about Telegram.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

# ---------------------------------------------------------------- paths ----

BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
STATIC_DIR = BASE_DIR / "static"
GUIDE_FILE = KNOWLEDGE_DIR / "registration_guide.md"  # default (Kazakh) guide

# --------------------------------------------------------------- config ----

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL")  # optional, used if MODEL is overloaded

# US5: "the chatbot must generate and show a response within 5 seconds".
# SLOW_RESPONSE is the target we measure against (every answer is timed and
# logged); RESPONSE_BUDGET is the hard cut-off after which we stop retrying
# and show the student an error instead of leaving them waiting.
SLOW_RESPONSE = float(os.getenv("SLOW_RESPONSE_SECONDS", "5"))
RESPONSE_BUDGET = float(os.getenv("RESPONSE_BUDGET_SECONDS", "20"))
REQUEST_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "15"))  # per HTTP call

MAX_QUESTION_LENGTH = 2000
HISTORY_TURNS = 10          # how many past messages are sent back to the model
MAX_ATTEMPTS = 3            # attempts per model before giving up
RETRY_PAUSE = 1.5           # seconds; grows with each attempt
RETRYABLE = {429, 500, 502, 503, 504}

if not API_KEY or not MODEL:
    raise RuntimeError(
        ".env файлында GEMINI_API_KEY және GEMINI_MODEL болуы керек "
        "(.env файлы core.py-мен бір папкада тұруы тиіс). Үлгі: env.example"
    )

try:
    client = genai.Client(
        api_key=API_KEY,
        http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT * 1000)),
    )
except Exception:  # noqa: BLE001 - older google-genai builds ignore http_options
    client = genai.Client(api_key=API_KEY)

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
        "kk": "Жасанды интеллектrе қосыла алмадық. .env-тегі API кілті мен модель атауын тексеріңіз "
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
}


def normalize_lang(value: Any, default: str = DEFAULT_LANG) -> str:
    """Turn anything ('ru', 'ru-RU', None, 'Қазақша') into a supported code."""
    code = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else default


def msg(key: str, lang: str) -> str:
    return MESSAGES[key].get(normalize_lang(lang), MESSAGES[key][DEFAULT_LANG])


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
- Answer ONLY using the knowledge base below. Do not guess dates, deadlines,
  prerequisites or any other facts that are not written there.
- The documents may be written in Russian, Kazakh or English. Understand them in
  any of these languages and translate the relevant facts into the answer language.
- LANGUAGE RULE (STRICT, HIGHEST PRIORITY): The student has selected the
  interface language "{ui_language}". You MUST write your entire answer in
  "{ui_language}" — including fallback answers like "I don't have this
  information" or "this is outside the knowledge base". Do NOT switch to
  Russian or English unless "{ui_language}" is Russian or English. This rule
  applies even if the question text itself is short, ambiguous, or mixes
  languages.
- Terminology: at this university, "course registration" is usually called making
  (creating) the schedule - in Kazakh "расписание құру" or "сабақ кестесін құру",
  in Russian "составление расписания". Treat these phrases as the same thing and use
  the wording the student used. The menu item in the student portal is named "Course Registration".
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


# --------------------------------------------------------------- Gemini ----


def _is_transport_error(exc: BaseException) -> bool:
    """True for 'the server is unreachable' style failures (DNS, TLS, timeout)."""
    name = type(exc).__name__.lower()
    return any(word in name for word in ("timeout", "connect", "network", "ssl", "protocol"))


def _error_key(exc: BaseException) -> str:
    if isinstance(exc, errors.APIError):
        if exc.code == 429:
            return "rate_limit"
        if exc.code in RETRYABLE:
            return "busy"
        return "api_error"
    if _is_transport_error(exc):
        return "connection"
    return "unexpected"


def ask_gemini(contents: Sequence[Any], system_prompt: str, deadline: float | None = None):
    """Ask the main model; retry temporary errors, then try the fallback model.

    `deadline` is a time.monotonic() value: once it passes we stop retrying so
    the student is never left waiting past the response budget.
    """
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL else [])
    last_exc: BaseException | None = None

    for model in models:
        for attempt in range(MAX_ATTEMPTS):
            if deadline is not None and time.monotonic() >= deadline:
                raise AssistantError(_error_key(last_exc) if last_exc else "timeout")
            try:
                return client.models.generate_content(
                    model=model,
                    contents=list(contents),
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.2,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - classified just below
                last_exc = exc
                retryable = (
                    isinstance(exc, errors.APIError) and exc.code in RETRYABLE
                ) or _is_transport_error(exc)
                print(f"[core] Gemini error ({model}, attempt {attempt + 1}): {exc}")
                if not retryable:
                    raise
                pause = RETRY_PAUSE * (attempt + 1)
                if attempt + 1 >= MAX_ATTEMPTS:
                    break
                if deadline is not None and time.monotonic() + pause >= deadline:
                    break
                time.sleep(pause)

    if last_exc is not None:
        raise last_exc
    raise AssistantError("timeout")


# ---------------------------------------------------------------- answer ----


def _as_turn(turn: Any) -> tuple[str, str]:
    """Accept dicts, pydantic models or anything with .role / .text."""
    if isinstance(turn, dict):
        role, text = turn.get("role"), turn.get("text")
    else:
        role, text = getattr(turn, "role", None), getattr(turn, "text", None)
    return ("user" if role == "user" else "model"), str(text or "").strip()


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

    contents: list[Any] = []
    for turn in list(history or [])[-HISTORY_TURNS:]:
        role, turn_text = _as_turn(turn)
        if turn_text:
            contents.append(types.Content(role=role, parts=[types.Part(text=turn_text)]))
    contents.append(types.Content(role="user", parts=[types.Part(text=text)]))

    system_prompt = SYSTEM_PROMPT.format(ui_language=LANGS[lang], knowledge=load_knowledge())

    started = time.monotonic()
    try:
        response = ask_gemini(contents, system_prompt, deadline=started + RESPONSE_BUDGET)
    except AssistantError:
        raise
    except Exception as exc:  # noqa: BLE001 - show the real cause in the terminal
        key = _error_key(exc)
        if key == "unexpected":
            print(f"[core] unexpected error: {exc!r}")
        raise AssistantError(key) from exc

    elapsed = time.monotonic() - started
    flag = "  <-- slower than the US5 target" if elapsed > SLOW_RESPONSE else ""
    print(f"[core] answered in {elapsed:.1f}s (target {SLOW_RESPONSE:.0f}s){flag}")

    return (response.text or "").strip() or msg("no_answer", lang)


async def answer_async(
    message: str, history: Iterable[Any] | None = None, lang: str = DEFAULT_LANG
) -> str:
    """`answer` for async callers: the blocking call runs in a worker thread,
    so the Telegram bot keeps serving other students while Gemini thinks."""
    return await asyncio.to_thread(answer, message, history, lang)
