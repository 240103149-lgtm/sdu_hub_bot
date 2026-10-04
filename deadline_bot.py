"""/deadline - link the student's Moodle account and show upcoming deadlines.

Flow (all texts in kk / ru / en, following the language chosen in the bot):

    /deadline
      |- already linked ........... show deadlines
      '- not linked ............... "link Moodle?"  [Register] [Cancel]
            |- Cancel ............. "you cancelled" - end
            '- Register
                 -> Moodle username
                 -> password   (the message is deleted right away)
                 -> linked, and the deadlines are shown straight away

What is kept: the Moodle username and a session (an API token, or cookies if the
university has the token service off) in the student's own user_data. The
password is never stored. /unlink_moodle removes everything.

Sign-in routing is owned by accounts_bot.register_accounts.
register_deadline adds the menu and unlink command.
"""
from __future__ import annotations

import asyncio
import html
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
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
import moodle
from moodle import MoodleError
from telegram_bot import BOT, COMMAND_LABELS, keep_typing, user_lang, tg_len

CHOOSE, ASK_USER, ASK_PASS = range(10, 13)

FLOW_TTL = 600  # seconds a half-finished sign-in stays "open"
MAX_SHOWN = 20  # deadlines per message; the rest is summarised

# ------------------------------------------------------------------ texts ----

D: dict[str, dict[str, str]] = {
    "need_link": {
        "kk": "Дедлайндарды көру үшін Moodle (moodle.sdu.edu.kz) аккаунтыңызды ботпен "
        "байланыстыру керек. Қазір байланыстырамыз ба?",
        "ru": "Чтобы увидеть дедлайны, нужно связать ваш аккаунт Moodle (moodle.sdu.edu.kz) "
        "с ботом. Сделаем это сейчас?",
        "en": "To see your deadlines, you need to link your Moodle account "
        "(moodle.sdu.edu.kz) to the bot. Do it now?",
    },
    "btn_register": {"kk": "✅ Тіркелу", "ru": "✅ Зарегистрироваться", "en": "✅ Register"},
    "btn_cancel": {"kk": "✖️ Бас тарту", "ru": "✖️ Отмена", "en": "✖️ Cancel"},
    "cancelled": {
        "kk": "Сіз Moodle-ге кіру процесінен бас тарттыңыз. Қаласаңыз, /deadline арқылы қайта бастай аласыз.",
        "ru": "Вы отменили вход в Moodle. Если передумаете, отправьте /deadline снова.",
        "en": "You cancelled the Moodle sign-in. If you change your mind, send /deadline again.",
    },
    "ask_user": {
        "kk": "Moodle логиніңізді (username) жазыңыз.\nБас тарту: /cancel",
        "ru": "Введите ваш логин от Moodle (username).\nОтмена: /cancel",
        "en": "Enter your Moodle username.\nTo cancel: /cancel",
    },
    "invalid_user": {
        "kk": "Логин бос болмауы және бос орынсыз болуы керек. Қайта жазыңыз.",
        "ru": "Логин не должен быть пустым или содержать пробелы. Введите ещё раз.",
        "en": "The username can't be empty or contain spaces. Please enter it again.",
    },
    "ask_pass": {
        "kk": "Енді Moodle парольін жазыңыз.\n"
        "🔒 Пароль тек moodle.sdu.edu.kz сайтына жіберіледі: ботта сақталмайды, "
        "хабарлама бірден жойылады.",
        "ru": "Теперь введите пароль от Moodle.\n"
        "🔒 Пароль уходит только на moodle.sdu.edu.kz: бот его не сохраняет, "
        "а сообщение сразу удаляется.",
        "en": "Now enter your Moodle password.\n"
        "🔒 It is sent only to moodle.sdu.edu.kz: the bot doesn't store it, "
        "and the message is deleted right away.",
    },
    "logging_in": {
        "kk": "⏳ Moodle-ге кіріп жатырмын…",
        "ru": "⏳ Вхожу в Moodle…",
        "en": "⏳ Signing in to Moodle…",
    },
    "linked_ok": {
        "kk": "✅ Moodle ботпен байланыстырылды! Байланысты өшіру: /unlink_moodle",
        "ru": "✅ Moodle связан с ботом! Отвязать: /unlink_moodle",
        "en": "✅ Moodle is linked to the bot! To unlink: /unlink_moodle",
    },
    "bad_credentials": {
        "kk": "Логин немесе пароль қате. /deadline арқылы қайта көріңіз.",
        "ru": "Неверный логин или пароль. Попробуйте снова: /deadline.",
        "en": "Wrong username or password. Try again with /deadline.",
    },
    "unavailable": {
        "kk": "Moodle қазір жауап бермейді. Кейінірек қайталап көріңіз.",
        "ru": "Moodle сейчас не отвечает. Попробуйте позже.",
        "en": "Moodle isn't responding right now. Please try again later.",
    },
    "unexpected_page": {
        "kk": "Moodle жауабы күтілгеннен басқаша болып шықты. Бұл туралы әзірлеушілерге хабарлаңыз.",
        "ru": "Ответ Moodle выглядит не так, как ожидалось. Сообщите об этом разработчикам.",
        "en": "Moodle's response doesn't look as expected. Please tell the developers.",
    },
    "session_expired": {
        "kk": "Moodle сессиясы аяқталды. /deadline арқылы қайта кіріңіз.",
        "ru": "Сессия в Moodle закончилась. Войдите заново: /deadline.",
        "en": "Your Moodle session has expired. Sign in again with /deadline.",
    },
    "session_lost": {
        "kk": "Кіру сессиясы ескірді. /deadline арқылы қайта бастаңыз.",
        "ru": "Сессия входа устарела. Начните заново: /deadline.",
        "en": "The sign-in session expired. Start again with /deadline.",
    },
    "title": {"kk": "📅 Дедлайндар", "ru": "📅 Дедлайны", "en": "📅 Deadlines"},
    "none": {
        "kk": "Жақын арада тапсыратын тапсырма жоқ 🎉",
        "ru": "Ближайших дедлайнов нет 🎉",
        "en": "No upcoming deadlines 🎉",
    },
    "overdue": {"kk": "⚠️ Мерзімі өткен", "ru": "⚠️ Просрочено", "en": "⚠️ Overdue"},
    "more": {"kk": "…және тағы {n}", "ru": "…и ещё {n}", "en": "…and {n} more"},
    "in": {"kk": "{x} қалды", "ru": "через {x}", "en": "in {x}"},
    "unlinked": {
        "kk": "Moodle байланысы өшірілді, сақталған деректер жойылды.",
        "ru": "Связь с Moodle удалена, сохранённые данные стёрты.",
        "en": "Moodle is unlinked and the saved data has been deleted.",
    },
    "not_linked": {
        "kk": "Moodle байланыстырылмаған. Бастау: /deadline",
        "ru": "Moodle не привязан. Начать: /deadline",
        "en": "No Moodle account is linked. Start with /deadline",
    },
}

UNITS = {"kk": ("күн", "сағ", "мин"), "ru": ("д", "ч", "мин"), "en": ("d", "h", "m")}
WEEKDAYS = {
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
    "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
MONTHS = {
    "kk": ["қаң", "ақп", "нау", "сәу", "мам", "мау", "шіл", "там", "қыр", "қаз", "қар", "жел"],
    "ru": ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}

MENU = {
    "kk": ("deadline", "Дедлайндар (Moodle)"),
    "ru": ("deadline", "Дедлайны (Moodle)"),
    "en": ("deadline", "Deadlines (Moodle)"),
}
HELP_LINE = {
    "kk": "/deadline — дедлайндар (Moodle)",
    "ru": "/deadline — дедлайны (Moodle)",
    "en": "/deadline — deadlines (Moodle)",
}


def t(key: str, lang: str) -> str:
    return D[key].get(core.normalize_lang(lang), D[key]["en"])


def _error_text(exc: MoodleError, lang: str) -> str:
    return t(exc.key, lang) if exc.key in D else core.msg("unexpected", lang)


# ------------------------------------------------------------- rendering ----


def _tz():
    try:
        return ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Almaty").strip() or "Asia/Almaty")
    except Exception:  # noqa: BLE001 - e.g. Windows without the tzdata package
        return timezone(timedelta(hours=5))


def _e(value: str) -> str:
    return html.escape(value or "", quote=False)


def _relative(seconds: int, lang: str) -> str:
    d_unit, h_unit, m_unit = UNITS[lang]
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        text = f"{days}{d_unit} {hours}{h_unit}" if hours else f"{days}{d_unit}"
    elif hours:
        text = f"{hours}{h_unit} {minutes}{m_unit}" if minutes else f"{hours}{h_unit}"
    else:
        text = f"{max(minutes, 1)}{m_unit}"
    return t("in", lang).format(x=text)


def _day_label(moment: datetime, lang: str) -> str:
    weekday = WEEKDAYS[lang][moment.weekday()]
    return f"{weekday}, {moment.day} {MONTHS[lang][moment.month - 1]}"


def _line(item: moodle.Deadline, moment: datetime, lang: str, now: int, with_date: bool) -> str:
    title = _e((item.title[:160] + "…" if len(item.title) > 160 else item.title) or "—")
    if item.url.startswith(("http://", "https://")) and len(html.escape(item.url, quote=True)) <= 800:
        title = f'<a href="{html.escape(item.url, quote=True)}">{title}</a>'
    clock = moment.strftime("%H:%M")
    when = f"{moment.day} {MONTHS[lang][moment.month - 1]}, {clock}" if with_date else clock
    if item.due >= now:
        when += f" · {_relative(item.due - now, lang)}"
    return f"📌 <b>{_e(item.course[:80])}</b> | {title} | {when}"


def render(deadlines: list[moodle.Deadline], lang: str, now: int | None = None) -> str:
    """Overdue first, then upcoming grouped by day:

        📌 CSS 206 | Lab 3 | 23:59 · in 2d 4h
    """
    now = int(time.time()) if now is None else now
    zone = _tz()
    if not deadlines:
        return t("none", lang)

    shown, hidden = deadlines[:MAX_SHOWN], max(0, len(deadlines) - MAX_SHOWN)
    overdue = [d for d in shown if d.due < now]
    upcoming = [d for d in shown if d.due >= now]

    lines = [f"<b>{t('title', lang)}</b> ({len(deadlines)})"]
    if overdue:
        lines += ["", f"<b>{t('overdue', lang)}</b>"]
        for item in overdue:
            lines.append(_line(item, datetime.fromtimestamp(item.due, zone), lang, now, with_date=True))

    current_day = None
    for item in upcoming:
        moment = datetime.fromtimestamp(item.due, zone)
        if moment.date() != current_day:
            current_day = moment.date()
            lines += ["", f"🗓 <b>{_day_label(moment, lang)}</b>"]
        lines.append(_line(item, moment, lang, now, with_date=False))

    if hidden:
        lines += ["", t("more", lang).format(n=hidden)]
    return "\n".join(lines)


def render_pages(deadlines: list[moodle.Deadline], lang: str, now: int | None = None) -> list[str]:
    """Keep valid HTML and split at whole events before Telegram's limit."""
    now = int(time.time()) if now is None else now
    if not deadlines:
        return [render([], lang, now)]
    pages, batch = [], []
    for item in deadlines[:MAX_SHOWN]:
        candidate = batch + [item]
        if batch and tg_len(render(candidate, lang, now)) > 3800:
            pages.append(render(batch, lang, now))
            batch = [item]
        else:
            batch = candidate
    pages.append(render(batch, lang, now))
    if len(deadlines) > MAX_SHOWN:
        pages[-1] += "\n\n" + t("more", lang).format(n=len(deadlines) - MAX_SHOWN)
    return pages


# ------------------------------------------------------------ flow state ----

# Moodle flow bookkeeping; accounts_bot owns the shared sign-in lifetime.
ACTIVE: dict[int, float] = {}


def _open_flow(user_id: int) -> None:
    ACTIVE[user_id] = time.monotonic()


def _close_flow(user_id: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    ACTIVE.pop(user_id, None)
    if context is not None:
        context.user_data.pop("deadline_user", None)


async def _forget(message) -> None:
    """Delete a message that contained a secret (the password)."""
    try:
        await message.delete()
    except TelegramError as exc:
        print(f"[deadline] could not delete a secret message: {exc}")


# --------------------------------------------------------------- handlers ----


async def _send_deadlines(bot, chat_id: int, context, user_id: int, lang: str) -> None:
    session = context.user_data.get("moodle")
    typing = asyncio.create_task(keep_typing(bot, chat_id))
    try:
        deadlines = await moodle.fetch_deadlines(session or {})
        pages = render_pages(deadlines, lang)
    except MoodleError as exc:
        if exc.key == "session_expired":
            context.user_data.pop("moodle", None)
        pages = [_error_text(exc, lang)]
    except Exception as exc:  # noqa: BLE001 - the student still gets an answer
        print(f"[deadline] unexpected error while reading deadlines: {exc!r}")
        pages = [core.msg("unexpected", lang)]
    finally:
        typing.cancel()
    for page in pages:
        await bot.send_message(chat_id, page, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    _close_flow(update.effective_user.id, context)

    if context.user_data.get("moodle"):
        await _send_deadlines(context.bot, update.effective_chat.id, context, update.effective_user.id, lang)
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("btn_register", lang), callback_data="deadline:register"),
                InlineKeyboardButton(t("btn_cancel", lang), callback_data="deadline:cancel"),
            ]
        ]
    )
    await update.message.reply_text(t("need_link", lang), reply_markup=keyboard)
    return CHOOSE


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    query = update.callback_query
    await query.answer()

    if query.data == "deadline:cancel":
        _close_flow(update.effective_user.id, context)
        await query.edit_message_text(t("cancelled", lang))
        return ConversationHandler.END

    _open_flow(update.effective_user.id)
    await query.edit_message_text(t("ask_user", lang))
    return ASK_USER


async def on_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    value = (update.message.text or "").strip()
    if not value or len(value) > 64 or any(ch.isspace() for ch in value):
        await update.message.reply_text(t("invalid_user", lang))
        return ASK_USER

    context.user_data["deadline_user"] = value
    await update.message.reply_text(t("ask_pass", lang))
    return ASK_PASS


async def on_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    user_id, chat_id = update.effective_user.id, update.effective_chat.id
    password = update.message.text or ""
    await _forget(update.message)

    username = context.user_data.pop("deadline_user", "")
    if not username or not password:
        _close_flow(user_id, context)
        await context.bot.send_message(chat_id, t("session_lost", lang))
        return ConversationHandler.END

    status = await context.bot.send_message(chat_id, t("logging_in", lang))
    typing = asyncio.create_task(keep_typing(context.bot, chat_id))
    try:
        session = await moodle.login(username, password)
    except MoodleError as exc:
        _close_flow(user_id, context)
        await status.edit_text(_error_text(exc, lang))
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001
        _close_flow(user_id, context)
        print(f"[deadline] unexpected error during sign-in: {exc!r}")
        await status.edit_text(core.msg("unexpected", lang))
        return ConversationHandler.END
    finally:
        typing.cancel()
        password = ""  # never kept longer than this request

    context.user_data["moodle"] = session
    _close_flow(user_id, context)
    await status.edit_text(t("linked_ok", lang))
    await _send_deadlines(context.bot, chat_id, context, user_id, lang)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = user_lang(update, context)
    _close_flow(update.effective_user.id, context)
    await update.message.reply_text(t("cancelled", lang))
    return ConversationHandler.END


async def cmd_unlink_moodle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    _close_flow(update.effective_user.id, context)
    if context.user_data.pop("moodle", None) is None:
        await update.message.reply_text(t("not_linked", lang))
        return
    await update.message.reply_text(t("unlinked", lang))


# ----------------------------------------------------------- registration ----


def register_deadline(app: Application) -> None:
    """Add /deadline and /unlink_moodle to the bot. Call it BEFORE the generic
    text handler is added (right after register_grade)."""
    for lang, (name, label) in MENU.items():
        labels = COMMAND_LABELS[lang]
        if not any(existing == name for existing, _ in labels):
            after_grade = next((i + 1 for i, (n, _) in enumerate(labels) if n == "grade"), 2)
            labels.insert(after_grade, (name, label))
        help_text = BOT[lang]["help"]
        if "/deadline" not in help_text:
            BOT[lang]["help"] = help_text.replace("/lang", f"{HELP_LINE[lang]}\n/lang", 1)

    app.add_handler(CommandHandler("unlink_moodle", cmd_unlink_moodle))
