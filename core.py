"""Shared core of the University AI Knowledge Hub.

Both entry points import from here, so the knowledge base, the prompt, the
AI calls and the localized error messages live in exactly one place:

    main.py          -> the website / REST API (FastAPI)
    telegram_bot.py  -> the Telegram bot (US5, US3, US6)

Nothing in this module knows about HTTP or about Telegram.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from types import SimpleNamespace
import httpx
from zoneinfo import ZoneInfo

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

import i18n
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()

# ---------------------------------------------------------------- paths ----

BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
STATIC_DIR = BASE_DIR / "static"
GUIDE_FILE = KNOWLEDGE_DIR / "registration_guide.md"  # default guide (currently in English)

# --------------------------------------------------------------- config ----

AI_PROVIDER = os.getenv("AI_PROVIDER", "deepseek" if os.getenv("DEEPSEEK_API_KEY") else "gemini").strip().lower()
if AI_PROVIDER not in {"deepseek", "gemini"}:
    raise RuntimeError("AI_PROVIDER must be deepseek or gemini")
PREFIX = AI_PROVIDER.upper()
API_KEY = os.getenv(f"{PREFIX}_API_KEY")
MODEL = os.getenv(f"{PREFIX}_MODEL") or ("deepseek-flash" if AI_PROVIDER == "deepseek" else None)
FALLBACK_MODEL = os.getenv(f"{PREFIX}_FALLBACK_MODEL")

# US1 (chatbot): "the chatbot must generate and show a response within 5
# seconds"; QA: if the AI server is unreachable or the response takes longer
# than 5 seconds, stop waiting and show "Connection error".
# SLOW_RESPONSE is the target every answer is measured and logged against;
# RESPONSE_BUDGET is the hard cut-off. Raise it in .env only if the model
# you use cannot answer within 5 seconds.
SLOW_RESPONSE = float(os.getenv("SLOW_RESPONSE_SECONDS", "5"))
RESPONSE_BUDGET = float(os.getenv("RESPONSE_BUDGET_SECONDS", "5"))
# One HTTP request may not outlive the budget by much: after the cut-off the
# request finishes in the background, and this bounds how long it can run.
REQUEST_TIMEOUT = float(os.getenv(f"{PREFIX}_TIMEOUT_SECONDS", str(RESPONSE_BUDGET + 1)))

MAX_QUESTION_LENGTH = 2000
HISTORY_TURNS = 10          # how many past messages are sent back to the model
MAX_ATTEMPTS = 3            # attempts per model before giving up
RETRY_PAUSE = 1.5           # seconds; grows with each attempt
RETRYABLE = {429, 500, 502, 503, 504}

if not API_KEY or not MODEL:
    raise RuntimeError(
        f"Environment variables {PREFIX}_API_KEY and {PREFIX}_MODEL must be configured. "
        "(.env файлы core.py-мен бір папкада тұруы тиіс). Үлгі: env.example"
    )

client = None
if AI_PROVIDER == "gemini":
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

# The texts are in locales/<lang>.json, section "core" (US6).
MESSAGES: dict[str, dict[str, str]] = i18n.by_key("core")


def normalize_lang(value: Any, default: str = DEFAULT_LANG) -> str:
    """Turn anything ('ru', 'ru-RU', None, 'Қазақша') into a supported code."""
    code = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else default


def msg(key: str, lang: str) -> str:
    return MESSAGES[key].get(normalize_lang(lang), MESSAGES[key][DEFAULT_LANG])


KAZAKH_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
# Phrases that only Kazakh uses, also when typed without the Kazakh letters.
KAZAKH_PHRASES = ("бар ма", "бар ме", "ма?", "ме?", "ба?", "бе?", "па?", "пе?")
# Whole words. Many Kazakh students type without ә/ғ/қ/ң/ө/ұ/ү/һ/і, so the
# "flat" spellings (калай, кашан, жок ...) are listed too.
KAZAKH_WORDS = frozenset("""
    ма ме ба бе па пе бар жок керек болады бола болса болмайды мен сен биз сиз
    олар неше нешеде кашан калай кайда кандай канша ким кимге кимнен айтшы айтыныз
    берши берш бериниз туралы жане ушин деген кейин бурын иа ия рахмет салем
    алу алам аламын барам барамын барсам болсам калсам керекпе ашылады жабылады
    кашаннан кайдан неге
""".split())
# Endings of Kazakh verbs and nouns that Russian words do not have.
KAZAKH_ENDINGS = ("мын", "мыз", "пын", "бын", "сыз", "ныз", "сиз", "низ", "лары", "дары", "тары")
RUSSIAN_WORDS = frozenset("""
    что как где когда какой какая какое какие каком сколько почему зачем куда
    откуда кто чем есть ли это этот эта эти мне меня мой моя мои можно нужно надо
    или если чтобы для при про после из от на в во и с со у к по о об за я вы ты
    он она они мы нет да уже еще ещё тоже также только будет был была было быть
    могу хочу скажи скажите подскажите пожалуйста здравствуйте привет спасибо
    находится работает получить сдать открыт открывается делать сдал сдала
""".split())
WORD_RE = re.compile(r"[a-zа-яёәғқңөұүһі]+")


def _kazakh_or_russian(lowered: str, default: str) -> str:
    """Cyrillic text without Kazakh letters: weigh Kazakh against Russian markers."""
    words = WORD_RE.findall(lowered)
    kk = sum(2 for w in words if w in KAZAKH_WORDS)
    kk += sum(1 for w in words if len(w) > 4 and w.endswith(KAZAKH_ENDINGS))
    kk += sum(2 for phrase in KAZAKH_PHRASES if phrase in lowered)
    ru = sum(2 for w in words if w in RUSSIAN_WORDS)
    ru += sum(1 for ch in lowered if ch in "ёъ")
    if kk != ru:
        return "kk" if kk > ru else "ru"
    # Nothing tells them apart (e.g. just "Медпункт"): keep the language the
    # student chose in the bot, or Russian if they never chose kk/ru.
    return default if default in ("kk", "ru") else "ru"


def detect_lang(text: str, default: str = DEFAULT_LANG) -> str:
    """Language of the student's question: 'kk', 'ru' or 'en'.

    Decided in code (not by the model) so the answer always follows the
    language the question was written in. `default` (the interface language)
    breaks ties: Cyrillic text that could be either Kazakh or Russian, and
    text with no letters to judge by.
    """
    sample = text or ""
    lowered = sample.lower()
    default = normalize_lang(default)
    cyr = sum(1 for ch in lowered if "а" <= ch <= "я" or ch == "ё" or ch in KAZAKH_LETTERS)
    lat = sum(1 for ch in lowered if "a" <= ch <= "z")
    if any(ch in KAZAKH_LETTERS for ch in sample):
        return "kk"
    if cyr >= 3 and cyr * 2 >= lat:
        return _kazakh_or_russian(lowered, default)
    if lat >= 2:
        return "en"
    return default


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
  "{ui_language}". Write the ENTIRE answer in "{ui_language}", including the
  fallback sentence below. Ignore the language of the knowledge base documents
  and of earlier messages in the conversation.
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
  Students also use informal words: "медпункт" / "медициналық орталық" = Medical
  Center; "жатақхана" / "общага" / "общежитие" / "дорм" = Student House;
  "анықтама" / "справка" = reference; "СДУ" = SDU.
- BOT COMMANDS: personal data is not in the knowledge base, but the SDU Hub
  Telegram bot has commands for it. If the student asks about their own Moodle
  deadlines, say to use /deadline; their grades - /grade; free classrooms -
  /rooms; the course registration guide - /guide; submitting enrollment
  documents - /documents; the list of campus events - /events (you can also
  answer about events from the knowledge base). Do not use the fallback sentence
  for these questions.
- FOLLOW-UP RULE: A short question often continues the conversation and refers
  to the subject of the previous messages (for example "оған қалай барсам
  болады?" / "how do I get there?" right after a question about the Medical
  Center means the Medical Center's location; "қалай хабарласамын?" / "как
  связаться?" right after you told the student to contact the administration
  means the administration's contacts). Answer it about that subject.
  Questions about how to contact the university, the administration, the SSC or
  any office ARE about the university: answer with the matching contacts from
  the knowledge base. If no office is named, give the Student Service Center
  (SSC) and the general university contacts.
- SOURCE RULE: Documents say when they were checked. If two documents disagree,
  or an older one says a fact is "not confirmed" (не подтверждено / расталмаған)
  while a newer one gives it, use the newer document and give the fact.
- Questions about grades, scores or points (for example "less than 70 points")
  are answered from the grading scale, FX/F rules and scholarship rules.
- FALLBACK RULE: If the answer is not in the knowledge base, or the question is
  not about the university, reply with exactly this sentence and nothing else:
  "{not_found}"
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


class GuideError(Exception):
    """The guide exists but could not be read: a temporary failure (US2 Retry)."""


def load_guide(lang: str) -> str | None:
    """US2: the registration guide for one language, or None if not published.

    Uses knowledge/registration_guide.<lang>.md when it exists, otherwise the
    default knowledge/registration_guide.md. Raises GuideError when the file
    is there but cannot be read.
    """
    lang = normalize_lang(lang, default="kk")
    for path in (KNOWLEDGE_DIR / f"registration_guide.{lang}.md", GUIDE_FILE):
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise GuideError(f"{path.name} could not be read: {exc}") from exc
    return None


# --------------------------------------------------------------- Gemini ----


def _is_transport_error(exc: BaseException) -> bool:
    """True for 'the server is unreachable' style failures (DNS, TLS, timeout)."""
    name = type(exc).__name__.lower()
    return any(word in name for word in ("timeout", "connect", "network", "ssl", "protocol"))


def _error_key(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return "rate_limit"
        return "busy" if exc.response.status_code in RETRYABLE else "api_error"
    if isinstance(exc, errors.APIError):
        if exc.code == 429:
            return "rate_limit"
        if exc.code in RETRYABLE:
            return "busy"
        return "api_error"
    if _is_transport_error(exc):
        return "connection"
    return "unexpected"


def _ask_deepseek(contents: Sequence[Any], system_prompt: str, model: str, timeout: float):
    messages = [{"role": "system", "content": system_prompt}]
    for content in contents:
        if isinstance(content, str):
            role, text = "user", content
        else:
            role = "assistant" if content.role == "model" else "user"
            text = "".join(part.text or "" for part in content.parts or [])
        messages.append({"role": role, "content": text})
    response = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": model, "messages": messages, "stream": False,
              "thinking": {"type": "disabled"}},
        timeout=timeout,
    )
    response.raise_for_status()
    return SimpleNamespace(text=response.json()["choices"][0]["message"]["content"])


def ask_ai(contents: Sequence[Any], system_prompt: str, deadline: float | None = None):
    """Ask the main model; retry temporary errors, then try the fallback model.

    `deadline` is a time.monotonic() value: once it passes we stop retrying so
    the student is never left waiting past the response budget.
    """
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL else [])
    last_exc: BaseException | None = None

    for model in models:
        for attempt in range(MAX_ATTEMPTS):
            if deadline is not None and time.monotonic() >= deadline:
                raise AssistantError(_error_key(last_exc) if last_exc else "connection")
            try:
                if AI_PROVIDER == "deepseek":
                    timeout = REQUEST_TIMEOUT
                    if deadline is not None:
                        timeout = min(timeout, max(0.001, deadline - time.monotonic()))
                    return _ask_deepseek(contents, system_prompt, model, timeout)
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
                ) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE) or _is_transport_error(exc)
                print(f"[core] {AI_PROVIDER} error ({model}, attempt {attempt + 1}): {exc}")
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
    raise AssistantError("connection")


# Compatibility for integrations that still import the old name.
ask_gemini = ask_ai


# ---------------------------------------------------------------- answer ----


def _as_turn(turn: Any) -> tuple[str, str]:
    """Accept dicts, pydantic models or anything with .role / .text."""
    if isinstance(turn, dict):
        role, text = turn.get("role"), turn.get("text")
    else:
        role, text = getattr(turn, "role", None), getattr(turn, "text", None)
    return ("user" if role == "user" else "model"), str(text or "").strip()


# The AI call runs in its own thread so answer() can stop waiting after
# RESPONSE_BUDGET even when the HTTP request itself is still hanging.
_AI_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="ai")


def answer(message: str, history: Iterable[Any] | None = None, lang: str = DEFAULT_LANG) -> str:
    """US1: answer one student question from the approved knowledge base.

    Raises AssistantError with a key the caller turns into a localized message;
    no answer within RESPONSE_BUDGET seconds -> "connection" (Connection error).
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

    # Today's date (Almaty time) so the model can tell passed events from upcoming ones.
    today = datetime.now(ZoneInfo("Asia/Almaty")).strftime("%A, %d %B %Y")
    reply_lang = detect_lang(text, lang)  # the language the question is written in
    system_prompt = SYSTEM_PROMPT.format(
        ui_language=LANGS[reply_lang],
        knowledge=load_knowledge(),
        today=today,
        not_found=msg("not_found", reply_lang),
    )

    started = time.monotonic()
    call = _AI_POOL.submit(ask_ai, contents, system_prompt, started + RESPONSE_BUDGET)
    try:
        response = call.result(timeout=RESPONSE_BUDGET)
    except concurrent.futures.TimeoutError:
        print(f"[core] no answer within {RESPONSE_BUDGET:g}s - stopped waiting (US1: Connection error)")
        raise AssistantError("connection") from None
    except AssistantError:
        raise
    except Exception as exc:  # noqa: BLE001 - show the real cause in the terminal
        key = _error_key(exc)
        if key == "unexpected":
            print(f"[core] unexpected error: {exc!r}")
        raise AssistantError(key) from exc

    elapsed = time.monotonic() - started
    flag = "  <-- slower than the US1 target" if elapsed > SLOW_RESPONSE else ""
    print(f"[core] answered in {elapsed:.1f}s (target {SLOW_RESPONSE:g}s){flag}")

    reply = (response.text or "").strip()
    if not reply:
        return msg("no_answer", reply_lang)
    if is_not_found(reply):
        # Keep the required US1 sentence, but never leave the student at a dead
        # end: say whom to contact and how.
        return f"{msg('not_found', reply_lang)}\n\n{msg('not_found_contacts', reply_lang)}"
    return reply


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text).casefold().split())


def is_not_found(reply: str) -> bool:
    """True when the model answered with the fallback sentence (in any language)."""
    plain = _plain(reply)
    return any(plain == _plain(msg("not_found", lang)) for lang in LANGS)


async def answer_async(
    message: str, history: Iterable[Any] | None = None, lang: str = DEFAULT_LANG
) -> str:
    """`answer` for async callers: the blocking call runs in a worker thread,
    so the Telegram bot keeps serving other students while the AI responds."""
    return await asyncio.to_thread(answer, message, history, lang)
