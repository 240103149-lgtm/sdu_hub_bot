"""/grade - link the student's university portal account to the bot.

Flow (all texts in kk / ru / en, following the language chosen in the bot):

    /grade
      |- already linked ........... show grades
      '- not linked ............... "sign in to the portal?"  [Register] [Cancel]
            |- Cancel ............. "you cancelled" - end
            '- Register
                 -> student number
                 -> password        (the message is deleted right away)
                 -> 2FA code        (sent to the student's e-mail; deleted too)
                 -> linked

What is kept: the student number and the portal *session cookies*, in the
student's own user_data. The password is never stored; the 2FA code is used
once. /unlink removes everything.

Sign-in routing is owned by accounts_bot.register_accounts.
register_grade adds the menu, transcript navigation and unlink command.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import core
import grade
from grade import PortalError
from telegram_bot import BOT, COMMAND_LABELS, keep_typing, user_lang

CHOOSE, ASK_ID, ASK_PASSWORD, ASK_CODE = range(4)

MAX_CODE_ATTEMPTS = 3
PENDING_TTL = 600  # seconds a half-finished sign-in may wait for the 2FA code

# ------------------------------------------------------------------ texts ----

G: dict[str, dict[str, str]] = {
    "need_link": {
        "kk": "Бағаларды көру үшін университет порталына (my.sdu.edu.kz) кіріп, "
        "оны ботпен байланыстыру керек. Қазір байланыстырамыз ба?",
        "ru": "Чтобы посмотреть оценки, нужно войти в портал университета (my.sdu.edu.kz) "
        "и связать его с ботом. Сделаем это сейчас?",
        "en": "To see your grades, you need to sign in to the university portal "
        "(my.sdu.edu.kz) and link it to the bot. Do it now?",
    },
    "btn_register": {"kk": "✅ Тіркелу", "ru": "✅ Зарегистрироваться", "en": "✅ Register"},
    "btn_cancel": {"kk": "✖️ Бас тарту", "ru": "✖️ Отмена", "en": "✖️ Cancel"},
    "cancelled": {
        "kk": "Сіз порталға кіру процесінен бас тарттыңыз. Қаласаңыз, /grade арқылы қайта бастай аласыз.",
        "ru": "Вы отменили вход в портал. Если передумаете, отправьте /grade снова.",
        "en": "You cancelled the portal sign-in. If you change your mind, send /grade again.",
    },
    "ask_id": {
        "kk": "Студент нөміріңізді (Student number) жазыңыз.\nБас тарту: /cancel",
        "ru": "Введите ваш студенческий номер (Student number).\nОтмена: /cancel",
        "en": "Enter your student number.\nTo cancel: /cancel",
    },
    "invalid_id": {
        "kk": "Студент нөмірі бос болмауы және бос орынсыз болуы керек. Қайта жазыңыз.",
        "ru": "Студенческий номер не должен быть пустым или содержать пробелы. Введите ещё раз.",
        "en": "The student number can't be empty or contain spaces. Please enter it again.",
    },
    "ask_password": {
        "kk": "Енді порталдың парольін жазыңыз.\n"
        "🔒 Пароль тек my.sdu.edu.kz порталына жіберіледі: ботта сақталмайды, "
        "хабарлама бірден жойылады.",
        "ru": "Теперь введите пароль от портала.\n"
        "🔒 Пароль уходит только на my.sdu.edu.kz: бот его не сохраняет, "
        "а сообщение сразу удаляется.",
        "en": "Now enter your portal password.\n"
        "🔒 It is sent only to my.sdu.edu.kz: the bot doesn't store it, "
        "and the message is deleted right away.",
    },
    "logging_in": {
        "kk": "⏳ Порталға кіріп жатырмын…",
        "ru": "⏳ Вхожу в портал…",
        "en": "⏳ Signing in to the portal…",
    },
    "ask_code": {
        "kk": "📧 Поштаңызға растау коды жіберілді. Кодты осында жазыңыз.",
        "ru": "📧 На вашу почту отправлен код подтверждения. Введите его здесь.",
        "en": "📧 A verification code was sent to your e-mail. Enter it here.",
    },
    "bad_code": {
        "kk": "Код қате немесе мерзімі өтіп кеткен. Кодты қайта жазыңыз (/cancel — бас тарту).",
        "ru": "Код неверный или просрочен. Введите код ещё раз (/cancel — отмена).",
        "en": "The code is wrong or expired. Enter it again (/cancel to stop).",
    },
    "too_many_codes": {
        "kk": "Код бірнеше рет қате енгізілді. Қауіпсіздік үшін процесс тоқтатылды. /grade арқылы қайта бастаңыз.",
        "ru": "Код введён неверно несколько раз. Для безопасности процесс остановлен. Начните заново: /grade.",
        "en": "The code was wrong several times, so the sign-in was stopped for safety. Start again with /grade.",
    },
    "bad_credentials": {
        "kk": "Студент нөмірі немесе пароль қате. /grade арқылы қайта көріңіз.",
        "ru": "Неверный студенческий номер или пароль. Попробуйте снова: /grade.",
        "en": "Wrong student number or password. Try again with /grade.",
    },
    "unavailable": {
        "kk": "Портал қазір жауап бермейді. Кейінірек қайталап көріңіз.",
        "ru": "Портал сейчас не отвечает. Попробуйте позже.",
        "en": "The portal isn't responding right now. Please try again later.",
    },
    "unexpected_page": {
        "kk": "Портал беті күтілгеннен басқаша болып шықты. Бұл туралы әзірлеушілерге хабарлаңыз.",
        "ru": "Страница портала выглядит не так, как ожидалось. Сообщите об этом разработчикам.",
        "en": "The portal page doesn't look as expected. Please tell the developers.",
    },
    "session_lost": {
        "kk": "Кіру сессиясы ескірді (10 минуттан артық болды). /grade арқылы қайта бастаңыз.",
        "ru": "Сессия входа устарела (прошло больше 10 минут). Начните заново: /grade.",
        "en": "The sign-in session expired (over 10 minutes). Start again with /grade.",
    },
    "linked_ok": {
        "kk": "✅ Портал ботпен байланыстырылды! Байланысты өшіру: /unlink",
        "ru": "✅ Портал связан с ботом! Отвязать: /unlink",
        "en": "✅ The portal is linked to the bot! To unlink: /unlink",
    },
    "session_expired": {
        "kk": "Портал сессиясы аяқталды. /grade арқылы қайта кіріңіз.",
        "ru": "Сессия на портале закончилась. Войдите заново: /grade.",
        "en": "Your portal session has expired. Sign in again with /grade.",
    },
    "grades_not_configured": {
        "kk": "Портал байланысқан, бірақ бағаларды көрсету әлі қосылмаған (келесі қадам).",
        "ru": "Портал связан, но показ оценок ещё не подключён (следующий шаг).",
        "en": "The portal is linked, but showing grades isn't connected yet (next step).",
    },
    "grades_title": {
        "kk": "📊 Бағаларыңыз",
        "ru": "📊 Ваши оценки",
        "en": "📊 Your grades",
    },
    "grades_empty": {
        "kk": "Порталда баға табылмады.",
        "ru": "На портале оценок не найдено.",
        "en": "No grades were found on the portal.",
    },
    "btn_prev": {"kk": "◀ Алдыңғы", "ru": "◀ Предыдущий", "en": "◀ Previous"},
    "btn_now": {"kk": "🔄 Қазір", "ru": "🔄 Сейчас", "en": "🔄 Now"},
    "unit_credit": {"kk": "кр.", "ru": "кр.", "en": "cr."},
    "lbl_credits": {"kk": "Кредит", "ru": "Кредиты", "en": "Credits"},
    "semester_title": {
        "kk": "{years} оқу жылы, {n}-семестр",
        "ru": "{years}, семестр {n}",
        "en": "{years}, Semester {n}",
    },
    "unlinked": {
        "kk": "Портал байланысы өшірілді, сақталған деректер жойылды.",
        "ru": "Связь с порталом удалена, сохранённые данные стёрты.",
        "en": "The portal is unlinked and the saved data has been deleted.",
    },
    "not_linked": {
        "kk": "Портал байланыстырылмаған. Бастау: /grade",
        "ru": "Портал не привязан. Начать: /grade",
        "en": "No portal is linked. Start with /grade",
    },
}

MENU = {
    "kk": ("grade", "Бағалар (портал)"),
    "ru": ("grade", "Оценки (портал)"),
    "en": ("grade", "Grades (portal)"),
}
HELP_LINE = {
    "kk": "/grade — бағалар (университет порталы)",
    "ru": "/grade — оценки (портал университета)",
    "en": "/grade — grades (university portal)",
}


def t(key: str, lang: str) -> str:
    return G[key].get(core.normalize_lang(lang), G[key]["en"])


def _error_text(exc: PortalError, lang: str) -> str:
    return t(exc.key, lang) if exc.key in G else core.msg("unexpected", lang)


# ------------------------------------------------------- pending sign-ins ----


@dataclass
class _Pending:
    login: grade.PortalLogin
    student_id: str
    attempts: int = 0
    created: float = field(default_factory=time.monotonic)


# A PortalLogin holds a live HTTP session, which cannot be pickled, so it lives
# here (in memory, per Telegram user) rather than in user_data.
PENDING: dict[int, _Pending] = {}


async def _drop_pending(user_id: int) -> None:
    pending = PENDING.pop(user_id, None)
    if pending is not None:
        await pending.login.close()


async def _purge_stale() -> None:
    now = time.monotonic()
    for user_id in [u for u, p in PENDING.items() if now - p.created > PENDING_TTL]:
        await _drop_pending(user_id)


async def _forget(message) -> None:
    """Delete a message that contained a secret (password / 2FA code)."""
    try:
        await message.delete()
    except TelegramError as exc:
        print(f"[grade] could not delete a secret message: {exc}")


def _link(context: ContextTypes.DEFAULT_TYPE, student_id: str, cookies: dict[str, str]) -> None:
    context.user_data["portal"] = {"student_id": student_id, "cookies": cookies}


# --------------------------------------------------------------- handlers ----


async def cmd_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    await _purge_stale()
    await _drop_pending(update.effective_user.id)

    if context.user_data.get("portal"):
        await _show_grades(update, context, lang)
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("btn_register", lang), callback_data="grade:register"),
                InlineKeyboardButton(t("btn_cancel", lang), callback_data="grade:cancel"),
            ]
        ]
    )
    await update.message.reply_text(t("need_link", lang), reply_markup=keyboard)
    return CHOOSE


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    query = update.callback_query
    await query.answer()

    if query.data == "grade:cancel":
        await query.edit_message_text(t("cancelled", lang))
        return ConversationHandler.END

    await query.edit_message_text(t("ask_id", lang))
    return ASK_ID


async def on_student_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    value = (update.message.text or "").strip()
    if not value or len(value) > 32 or any(ch.isspace() for ch in value):
        await update.message.reply_text(t("invalid_id", lang))
        return ASK_ID

    context.user_data["grade_sid"] = value
    await update.message.reply_text(t("ask_password", lang))
    return ASK_PASSWORD


async def on_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    user_id = update.effective_user.id
    password = update.message.text or ""
    await _forget(update.message)

    student_id = context.user_data.pop("grade_sid", "")
    if not student_id or not password:
        await context.bot.send_message(update.effective_chat.id, t("session_lost", lang))
        return ConversationHandler.END

    await _drop_pending(user_id)
    status = await context.bot.send_message(update.effective_chat.id, t("logging_in", lang))
    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id))
    login = grade.PortalLogin()
    try:
        needs_code = await login.submit_credentials(student_id, password)
    except PortalError as exc:
        await login.close()
        await status.edit_text(_error_text(exc, lang))
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001 - the student still gets an answer
        await login.close()
        print(f"[grade] unexpected error during sign-in: {exc!r}")
        await status.edit_text(core.msg("unexpected", lang))
        return ConversationHandler.END
    finally:
        typing.cancel()
        password = ""  # never kept longer than this request

    if needs_code:
        PENDING[user_id] = _Pending(login=login, student_id=student_id)
        await status.edit_text(t("ask_code", lang))
        return ASK_CODE

    _link(context, student_id, login.cookies())
    await login.close()
    await status.edit_text(t("linked_ok", lang))
    return ConversationHandler.END


async def on_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    user_id = update.effective_user.id
    code = (update.message.text or "").strip()
    await _forget(update.message)

    pending = PENDING.get(user_id)
    if pending is None or time.monotonic() - pending.created > PENDING_TTL:
        await _drop_pending(user_id)
        await context.bot.send_message(update.effective_chat.id, t("session_lost", lang))
        return ConversationHandler.END

    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id))
    try:
        await pending.login.submit_code(code)
    except PortalError as exc:
        if exc.key == "bad_code":
            pending.attempts += 1
            if pending.attempts < MAX_CODE_ATTEMPTS:
                await context.bot.send_message(update.effective_chat.id, t("bad_code", lang))
                return ASK_CODE
            await _drop_pending(user_id)
            await context.bot.send_message(update.effective_chat.id, t("too_many_codes", lang))
            return ConversationHandler.END
        await _drop_pending(user_id)
        await context.bot.send_message(update.effective_chat.id, _error_text(exc, lang))
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001
        await _drop_pending(user_id)
        print(f"[grade] unexpected error during 2FA: {exc!r}")
        await context.bot.send_message(update.effective_chat.id, core.msg("unexpected", lang))
        return ConversationHandler.END
    finally:
        typing.cancel()

    _link(context, pending.student_id, pending.login.cookies())
    await _drop_pending(user_id)
    await context.bot.send_message(update.effective_chat.id, t("linked_ok", lang))
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    context.user_data.pop("grade_sid", None)
    await _drop_pending(update.effective_user.id)
    await update.message.reply_text(t("cancelled", lang))
    return ConversationHandler.END


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    _CACHE.pop(update.effective_user.id, None)
    if context.user_data.pop("portal", None) is None:
        await update.message.reply_text(t("not_linked", lang))
        return
    await update.message.reply_text(t("unlinked", lang))


# ------------------------------------------------------------- transcript ----

TRANSCRIPT_TTL = 300  # seconds a downloaded transcript is reused while the student flips pages
NUMBERS_PER_ROW = 6

# Kept in memory (not in user_data): parsed pages are cheap to re-download.
_CACHE: dict[int, tuple[float, list[grade.Semester]]] = {}


async def _load_semesters(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, force: bool = False
) -> list[grade.Semester]:
    cached = _CACHE.get(user_id)
    if cached and not force and time.monotonic() - cached[0] < TRANSCRIPT_TTL:
        return cached[1]
    portal = context.user_data.get("portal") or {}
    semesters = await grade.fetch_transcript(portal.get("cookies") or {})
    _CACHE[user_id] = (time.monotonic(), semesters)
    return semesters


def _e(value: str) -> str:
    return html.escape(value or "", quote=False)


# Course names come from the portal in one language. They are translated into
# the student's language (chosen in /start) with Gemini, once per name.
_NAMES: dict[tuple[str, str], str] = {}
TRANSLATE_BUDGET = 8.0  # seconds; after that the original names are shown


def _translate_blocking(names: list[str], lang: str) -> list[str]:
    language = core.LANGS[lang]
    system = (
        f"You translate university course names into {language}. Use the standard academic "
        f"wording a {language}-speaking university would use. If a name is already in "
        f"{language}, return it unchanged. Return ONLY a JSON array of strings with exactly "
        "the same number of items, in the same order. No comments and no code fences."
    )
    response = core.ask_gemini(
        [json.dumps(names, ensure_ascii=False)], system, deadline=time.monotonic() + TRANSLATE_BUDGET
    )
    text = (response.text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    result = json.loads(text)
    if not (isinstance(result, list) and len(result) == len(names) and all(isinstance(x, str) for x in result)):
        raise ValueError("translation came back in an unexpected shape")
    return [x.strip() or src for x, src in zip(result, names)]


async def _translate_names(names: list[str], lang: str) -> dict[str, str]:
    """original name -> name in `lang`. Never raises: on any problem the
    student simply sees the portal's original names."""
    todo = [n for n in dict.fromkeys(names) if (lang, n) not in _NAMES]
    if todo:
        try:
            for source, translated in zip(todo, await asyncio.to_thread(_translate_blocking, todo, lang)):
                _NAMES[(lang, source)] = translated
        except Exception as exc:  # noqa: BLE001
            print(f"[grade] could not translate course names, showing originals: {exc!r}")
    return {n: _NAMES.get((lang, n), n) for n in names}


def _title(sem: grade.Semester, lang: str) -> str:
    """"2024 - 2025. 2" -> "2024–2025, семестр 2" (or kk / en)."""
    match = re.match(r"^(\d{4})\s*-\s*(\d{4})\.\s*(\d+)$", sem.title)
    if not match:
        return sem.title
    return t("semester_title", lang).format(years=f"{match.group(1)}–{match.group(2)}", n=match.group(3))


def _render(
    sem: grade.Semester, index: int, total: int, lang: str, names: dict[str, str] | None = None
) -> str:
    """One line per course:  📌 CSS 217 | Software Architecture | 5 ECTS | B+"""
    names = names or {}
    lines = [f"📊 <b>{_e(_title(sem, lang))}</b>  ({index + 1}/{total})", ""]
    for course in sem.courses:
        mark = course.letter or course.score.replace("*", "").strip()
        parts = [f"<b>{_e(course.code)}</b>", _e(names.get(course.name, course.name))]
        if course.ects:
            parts.append(f"{_e(course.ects)} ECTS")
        if mark:
            parts.append(f"<b>{_e(mark)}</b>")
        lines.append("📌 " + " | ".join(parts))

    totals = [
        f"ECTS {sem.ects}" if sem.ects else "",
        f"SPA {sem.spa}" if sem.spa else "",
        f"GPA {sem.gpa}" if sem.gpa else "",
    ]
    totals = [x for x in totals if x]
    if totals:
        lines += ["", "<b>Σ</b>  " + "  ·  ".join(_e(x) for x in totals)]
    return "\n".join(lines)


async def _page(semesters: list[grade.Semester], index: int, lang: str):
    """Text + keyboard for one semester, with course names in the student's language."""
    sem = semesters[index]
    names = await _translate_names([c.name for c in sem.courses], lang)
    return (
        _render(sem, index, len(semesters), lang, names),
        _keyboard(index, len(semesters), lang),
    )


def _keyboard(index: int, total: int, lang: str) -> InlineKeyboardMarkup:
    """Top: Previous / Now. Below: the semester numbers, the open one marked."""
    top = []
    if index > 0:
        top.append(InlineKeyboardButton(t("btn_prev", lang), callback_data=f"gr:{index - 1}"))
    top.append(InlineKeyboardButton(t("btn_now", lang), callback_data="gr:now"))

    numbers = [
        InlineKeyboardButton(f"• {i + 1}" if i == index else str(i + 1), callback_data=f"gr:{i}")
        for i in range(total)
    ]
    rows = [top] + [numbers[i : i + NUMBERS_PER_ROW] for i in range(0, total, NUMBERS_PER_ROW)]
    return InlineKeyboardMarkup(rows)


async def _show_grades(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    """/grade for a linked student: open the latest semester."""
    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id))
    try:
        semesters = await _load_semesters(context, update.effective_user.id, force=True)
        page = await _page(semesters, len(semesters) - 1, lang) if semesters else None
    except PortalError as exc:
        if exc.key == "session_expired":
            context.user_data.pop("portal", None)
            _CACHE.pop(update.effective_user.id, None)
        await update.message.reply_text(_error_text(exc, lang))
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[grade] unexpected error while reading grades: {exc!r}")
        await update.message.reply_text(core.msg("unexpected", lang))
        return
    finally:
        typing.cancel()

    if page is None:
        await update.message.reply_text(t("grades_empty", lang))
        return
    text, keyboard = page
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def on_grade_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on Previous / Now / a semester number."""
    lang = user_lang(update, context)
    query = update.callback_query
    await query.answer()

    if not context.user_data.get("portal"):
        await query.edit_message_text(t("not_linked", lang))
        return
    try:
        semesters = await _load_semesters(context, update.effective_user.id)
        page = None
        if semesters:
            arg = query.data.split(":", 1)[1]
            index = len(semesters) - 1 if arg == "now" else min(int(arg), len(semesters) - 1)
            page = await _page(semesters, index, lang)
    except PortalError as exc:
        if exc.key == "session_expired":
            context.user_data.pop("portal", None)
            _CACHE.pop(update.effective_user.id, None)
        await query.edit_message_text(_error_text(exc, lang))
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[grade] unexpected error while flipping semesters: {exc!r}")
        await query.edit_message_text(core.msg("unexpected", lang))
        return

    if page is None:
        await query.edit_message_text(t("grades_empty", lang))
        return
    text, keyboard = page
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():  # tapping the open semester is harmless
            raise


# ----------------------------------------------------------- registration ----


def register_grade(app: Application) -> None:
    """Add /grade and /unlink to the bot. Call it BEFORE the generic text
    handler is added, so the sign-in steps get the student's messages first."""
    for lang, (name, label) in MENU.items():
        labels = COMMAND_LABELS[lang]
        if not any(existing == name for existing, _ in labels):
            labels.insert(2, (name, label))  # right after /start and /guide
        help_text = BOT[lang]["help"]
        if "/grade" not in help_text:
            BOT[lang]["help"] = help_text.replace("/lang", f"{HELP_LINE[lang]}\n/lang", 1)

    app.add_handler(CallbackQueryHandler(on_grade_nav, pattern=r"^gr:(now|\d+)$"))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
