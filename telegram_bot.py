"""Telegram bot for the University AI Knowledge Hub.

Covers the same user stories as the website, using the shared `core` module:

    US5  free-text questions answered from the approved knowledge base,
         with a fallback answer, a connection-error warning and a timing log
    US3  /guide - the step-by-step course registration guide
    US6  Kazakh / Russian / English interface

This module only builds the Application. Run it with `python bot.py`
(long polling) or let main.py serve it over a webhook.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import tempfile
import traceback
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

import core

# Telegram rejects messages longer than 4096; we leave a little headroom.
TELEGRAM_LIMIT = 4000
TYPING_REFRESH = 4  # seconds; Telegram's "typing…" indicator lasts about 5

PORTAL_URL = os.getenv("REGISTRATION_PORTAL_URL", "").strip()

# Vercel's filesystem is read-only except /tmp, and /tmp is wiped between
# cold starts - so per-user language/history won't *persist* there, but at
# least PicklePersistence won't crash trying to write next to the source
# code (core.BASE_DIR). On a persistent host (Railway, Render, a VPS) this
# still writes next to core.py as before, and survives restarts normally.
STATE_FILE = (
    Path(tempfile.gettempdir()) / "bot_state.pickle"
    if os.getenv("VERCEL")
    else core.BASE_DIR / "bot_state.pickle"
)

LANG_BUTTONS = [("kk", "🇰🇿 Қазақша"), ("ru", "🇷🇺 Русский"), ("en", "🇬🇧 English")]

# ------------------------------------------------------------ bot texts ----

BOT: dict[str, dict[str, str]] = {
    "kk": {
        "start": (
            'Сәлем, {name}! 👋\n'
            '\n'
            'Мен — **SDU Hub**, студенттік өмірдегі көмекшіңмін. Дедлайндарды тексеруге, бос кабинет табуға және кампустағы ивенттерден хабардар болуға көмектесемін.\n'
            '\n'
            '**📚 Оқу**\n'
            '/deadline — Moodle тапсырмаларының дедлайндары\n'
            '/grade — бағаларыңды көру\n'
            '/guide — сабақ кестесін құру нұсқаулығы\n'
            '\n'
            '**🏫 Кампус**\n'
            '/events — университеттегі ивенттер\n'
            '/rooms — бос кабинеттер\n'
            'Керек уақытты да көрсетуге болады: /rooms 14:30\n'
            '\n'
            '**⚙️ Баптаулар**\n'
            '/lang — Қазақша / Русский / English\n'
            '/help — барлық командалар мен көмек\n'
            '\n'
            '💬 Сұрағың бар ма? Жай ғана жаз:\n'
            '«Расписание қалай құрамын?»\n'
            '\n'
            'Бастау үшін төмендегі батырмалардың бірін таңда 👇'
        ),
        "help": (
            '**SDU Hub · Көмек**\n'
            '\n'
            '**📚 Оқу**\n'
            '/deadline — Moodle тапсырмаларының дедлайндары\n'
            '/grade — бағаларыңды көру\n'
            '/guide — сабақ кестесін құру нұсқаулығы\n'
            '\n'
            '**🏫 Кампус**\n'
            '/events — университеттегі ивенттер\n'
            '/rooms — бос кабинеттер\n'
            'Керек уақытты да көрсетуге болады: /rooms 14:30\n'
            '\n'
            '**⚙️ Баптаулар**\n'
            '/lang — Қазақша / Русский / English\n'
            '/help — барлық командалар мен көмек\n'
            '\n'
            '**💬 Әңгіме және аккаунттар**\n'
            '/start — негізгі мәзірге оралу\n'
            '/reset — AI әңгімесінің тарихын тазарту\n'
            '/cancel — аккаунтқа кіруді тоқтату\n'
            '/unlink — университет порталының байланысын өшіру\n'
            '/unlink_moodle — Moodle байланысын өшіру\n'
            '\n'
            'Бағалар мен дедлайндарды алғаш ашқанда жеке чатта аккаунтыңды байланыстыру ұсынылады.'
        ),
        "choose_lang": "Тілді таңдаңыз:",
        "lang_set": "Тіл қазақшаға ауыстырылды. Сұрағыңызды жазыңыз.",
        "reset": "Әңгіме тарихы тазартылды. Жаңа сұрақ қоя аласыз.",
        "portal": "🔗 Тіркеу порталын ашу",
        "not_text": "Мен әзірге тек мәтінді түсінемін. Сұрағыңызды жазып жіберіңізші.",
    },
    "ru": {
        "start": (
            'Привет, {name}! 👋\n'
            '\n'
            'Я — **SDU Hub**, твой помощник в студенческой жизни. Помогу проверить дедлайны, найти свободную аудиторию и узнать о событиях кампуса.\n'
            '\n'
            '**📚 Учёба**\n'
            '/deadline — дедлайны заданий в Moodle\n'
            '/grade — твои оценки\n'
            '/guide — инструкция по составлению расписания\n'
            '\n'
            '**🏫 Кампус**\n'
            '/events — события университета\n'
            '/rooms — свободные аудитории\n'
            'Можно указать время: /rooms 14:30\n'
            '\n'
            '**⚙️ Настройки**\n'
            '/lang — Қазақша / Русский / English\n'
            '/help — все команды и помощь\n'
            '\n'
            '💬 Есть вопрос? Просто напиши:\n'
            '«Как составить расписание?»\n'
            '\n'
            'Чтобы начать, выбери кнопку ниже 👇'
        ),
        "help": (
            '**SDU Hub · Помощь**\n'
            '\n'
            '**📚 Учёба**\n'
            '/deadline — дедлайны заданий в Moodle\n'
            '/grade — твои оценки\n'
            '/guide — инструкция по составлению расписания\n'
            '\n'
            '**🏫 Кампус**\n'
            '/events — события университета\n'
            '/rooms — свободные аудитории\n'
            'Можно указать время: /rooms 14:30\n'
            '\n'
            '**⚙️ Настройки**\n'
            '/lang — Қазақша / Русский / English\n'
            '/help — все команды и помощь\n'
            '\n'
            '**💬 Диалог и аккаунты**\n'
            '/start — вернуться в главное меню\n'
            '/reset — очистить историю диалога с AI\n'
            '/cancel — отменить вход в аккаунт\n'
            '/unlink — отвязать университетский портал\n'
            '/unlink_moodle — отвязать Moodle\n'
            '\n'
            'При первом открытии оценок или дедлайнов бот предложит связать аккаунт в личном чате.'
        ),
        "choose_lang": "Выберите язык:",
        "lang_set": "Язык переключён на русский. Задавайте вопрос.",
        "reset": "История диалога очищена. Можете задать новый вопрос.",
        "portal": "🔗 Открыть портал регистрации",
        "not_text": "Пока я понимаю только текст. Напишите, пожалуйста, ваш вопрос.",
    },
    "en": {
        "start": (
            'Hi, {name}! 👋\n'
            '\n'
            "I'm **SDU Hub**, your student-life assistant. I can help you check deadlines, find a free room and discover campus events.\n"
            '\n'
            '**📚 Study**\n'
            '/deadline — Moodle assignment deadlines\n'
            '/grade — your grades\n'
            '/guide — how to create your course schedule\n'
            '\n'
            '**🏫 Campus**\n'
            '/events — university events\n'
            '/rooms — free classrooms\n'
            'You can also choose a time: /rooms 14:30\n'
            '\n'
            '**⚙️ Settings**\n'
            '/lang — Қазақша / Русский / English\n'
            '/help — all commands and help\n'
            '\n'
            '💬 Have a question? Just type it:\n'
            '“How do I create my schedule?”\n'
            '\n'
            'Choose a button below to get started 👇'
        ),
        "help": (
            '**SDU Hub · Help**\n'
            '\n'
            '**📚 Study**\n'
            '/deadline — Moodle assignment deadlines\n'
            '/grade — your grades\n'
            '/guide — how to create your course schedule\n'
            '\n'
            '**🏫 Campus**\n'
            '/events — university events\n'
            '/rooms — free classrooms\n'
            'You can also choose a time: /rooms 14:30\n'
            '\n'
            '**⚙️ Settings**\n'
            '/lang — Қазақша / Русский / English\n'
            '/help — all commands and help\n'
            '\n'
            '**💬 Conversation and accounts**\n'
            '/start — return to the main menu\n'
            '/reset — clear your AI conversation history\n'
            '/cancel — cancel account sign-in\n'
            '/unlink — unlink the university portal\n'
            '/unlink_moodle — unlink Moodle\n'
            '\n'
            'When you first open grades or deadlines, the bot will offer to link your account in a private chat.'
        ),
        "choose_lang": "Choose a language:",
        "lang_set": "Language switched to English. Go ahead and ask.",
        "reset": "Conversation history cleared. You can ask a new question.",
        "portal": "🔗 Open the registration portal",
        "not_text": "I only understand text for now. Please type your question.",
    },
}

COMMAND_LABELS: dict[str, list[tuple[str, str]]] = {
    "kk": [
        ("start", "Ботты бастау"),
        ("guide", "Расписание құру нұсқаулығы"),
        ("lang", "Тілді ауыстыру"),
        ("reset", "Әңгімені тазарту"),
        ("help", "Анықтама"),
    ],
    "ru": [
        ("start", "Запустить бота"),
        ("guide", "Инструкция по расписанию"),
        ("lang", "Сменить язык"),
        ("reset", "Очистить диалог"),
        ("help", "Справка"),
    ],
    "en": [
        ("start", "Start the bot"),
        ("guide", "Course registration guide"),
        ("lang", "Change language"),
        ("reset", "Clear the conversation"),
        ("help", "Help"),
    ],
}


def text(key: str, lang: str) -> str:
    return BOT[core.normalize_lang(lang)][key]


def add_command(name: str, menu: dict[str, str], help_line: dict[str, str]) -> None:
    """Register a feature in all languages without duplicating menu entries."""
    for lang in core.LANGS:
        labels = COMMAND_LABELS[lang]
        if not any(existing == name for existing, _ in labels):
            position = next((i for i, (n, _) in enumerate(labels) if n == "lang"), len(labels))
            labels.insert(position, (name, menu[lang]))
        if f"/{name} " not in BOT[lang]["help"]:
            BOT[lang]["help"] = BOT[lang]["help"].replace("/lang", f"{help_line[lang]}\n/lang", 1)


# --------------------------------------------------------- text helpers ----


def tg_len(source: str) -> int:
    """Telegram measures messages in UTF-16 code units, so an emoji counts 2."""
    return len(source.encode("utf-16-le")) // 2


def render_line(source: str) -> str | None:
    """One plain-text line as self-contained Telegram HTML, or None to drop it.

    The text may contain **bold**, `- ` bullets and `# ` headings. Escaping
    happens before the bold conversion, so nothing in the content can be
    mistaken for a tag - and because bold never spans a line break, splitting
    a long answer at line boundaries can never leave a tag unclosed.
    """
    line = source.rstrip()
    if line.lstrip().startswith("<!--"):  # markdown comments in the guide
        return None
    heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
    if heading:
        line = f"**{heading.group(1).strip()}**"
    elif re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):  # --- horizontal rule
        line = "──────────"
    else:
        line = re.sub(r"^(\s*)[*+-]\s+", r"\1• ", line)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(line))


def plain_to_telegram(source: str) -> str:
    """Render a whole plain-text answer or guide as Telegram HTML."""
    rendered = (render_line(line) for line in source.split("\n"))
    return "\n".join(line for line in rendered if line is not None)


def _longest_fitting_prefix(source: str, limit: int) -> int:
    """How much of one plain line can be rendered without exceeding `limit`."""
    low, high, best = 1, len(source), 1
    while low <= high:
        middle = (low + high) // 2
        if tg_len(render_line(source[:middle]) or "") <= limit:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best


def render_chunks(source: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Telegram HTML, split into messages that each fit Telegram's size limit.

    Length is measured *after* escaping - a line of `&` grows five times - so
    a long answer full of special characters is never rejected as too long.
    """
    chunks: list[str] = []
    current = ""

    for raw in source.split("\n"):
        rendered = render_line(raw)
        if rendered is None:
            continue

        if tg_len(rendered) <= limit:
            pieces = [rendered]
        else:  # a single enormous line: cut the plain text, then re-render
            pieces, rest = [], raw.rstrip()
            while rest:
                cut = _longest_fitting_prefix(rest, limit)
                pieces.append(render_line(rest[:cut]) or "")
                rest = rest[cut:]

        for piece in pieces:
            if current and tg_len(current) + 1 + tg_len(piece) > limit:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece

    if current.strip():
        chunks.append(current)
    return chunks or [""]


def strip_tags(rendered: str) -> str:
    """The plain-text version of a rendered chunk, for the fallback send."""
    return html.unescape(re.sub(r"</?b>", "", rendered))


async def reply(message, source: str, reply_markup: Any = None) -> None:
    """Send plain text as HTML, split across messages if it is long.

    If Telegram ever rejects our markup we resend that chunk without it, so a
    formatting slip can never swallow a student's answer.
    """
    chunks = render_chunks(source)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            await message.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except TelegramError as exc:
            print(f"[bot] HTML send failed ({exc}); resending as plain text")
            await message.reply_text(
                strip_tags(chunk), disable_web_page_preview=True, reply_markup=markup
            )


HOME_BUTTONS = {
    "kk": [("deadline", "📅 Дедлайндар"), ("grade", "📊 Бағалар"),
           ("events", "🎉 Ивенттер"), ("rooms", "🚪 Бос кабинеттер"),
           ("guide", "📚 Кесте құру"), ("lang", "🌐 Тіл")],
    "ru": [("deadline", "📅 Дедлайны"), ("grade", "📊 Оценки"),
           ("events", "🎉 События"), ("rooms", "🚪 Аудитории"),
           ("guide", "📚 Расписание"), ("lang", "🌐 Язык")],
    "en": [("deadline", "📅 Deadlines"), ("grade", "📊 Grades"),
           ("events", "🎉 Events"), ("rooms", "🚪 Free rooms"),
           ("guide", "📚 Schedule guide"), ("lang", "🌐 Language")],
}
MENU_PATTERN = r"^menu:(start|help|events|rooms|guide|lang)$"


def home_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Six main actions, two per row. Callback data is language-independent."""
    buttons = [InlineKeyboardButton(label, callback_data=f"menu:{action}")
               for action, label in HOME_BUTTONS[core.normalize_lang(lang)]]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"lang:{code}") for code, label in LANG_BUTTONS]]
    )


def portal_keyboard(lang: str) -> InlineKeyboardMarkup | None:
    if not PORTAL_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(text("portal", lang), url=PORTAL_URL)]])


def user_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Use the saved language choice; new users always start in English."""
    stored = context.user_data.get("lang") if context.user_data is not None else None
    if stored in core.LANGS:
        return stored
    lang = core.DEFAULT_LANG
    if context.user_data is not None:
        context.user_data["lang"] = lang
    return lang


async def keep_typing(bot, chat_id: int) -> None:
    """Hold the 'typing…' indicator until the answer is ready."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(TYPING_REFRESH)
    except asyncio.CancelledError:
        pass
    except TelegramError:
        pass


# ------------------------------------------------------------- handlers ----


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    user = update.effective_user
    name = (user.first_name if user and user.first_name else "").strip() or "студент"
    await reply(
        update.effective_message,
        text("start", lang).format(name=name),
        reply_markup=home_keyboard(lang),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    await reply(update.effective_message, text("help", lang), reply_markup=home_keyboard(lang))


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    await update.effective_message.reply_text(text("choose_lang", lang), reply_markup=language_keyboard())


async def on_lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chosen = core.normalize_lang(query.data.split(":", 1)[-1])
    context.user_data["lang"] = chosen
    if update.effective_chat.type == "private":
        commands = [BotCommand(name, label) for name, label in COMMAND_LABELS[chosen]]
        try:
            await context.bot.set_my_commands(
                commands, scope=BotCommandScopeChat(chat_id=update.effective_chat.id)
            )
        except TelegramError as exc:
            print(f"[bot] could not update the chat command menu: {exc}")
    try:
        await query.edit_message_text(text("lang_set", chosen))
    except TelegramError:
        await query.message.reply_text(text("lang_set", chosen))
    await cmd_start(update, context)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    context.user_data["history"] = []
    await update.effective_message.reply_text(text("reset", lang))


async def cmd_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """US3: show the approved step-by-step registration guide."""
    lang = user_lang(update, context)
    guide = core.load_guide(lang)
    if not guide or not guide.strip():
        await update.effective_message.reply_text(core.msg("guide_missing", lang))
        return
    await reply(update.effective_message, guide.strip(), reply_markup=portal_keyboard(lang))


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main-menu buttons use the same feature handlers as slash commands."""
    from events_bot import cmd_events
    from rooms_bot import cmd_rooms

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    handlers = {
        "start": cmd_start, "help": cmd_help, "guide": cmd_guide,
        "lang": cmd_lang, "events": cmd_events, "rooms": cmd_rooms,
    }
    # A button should open rooms for now even if an earlier command had args.
    context.args = []
    await handlers[action](update, context)


async def on_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """US5: answer a free-text question from the approved knowledge base."""
    lang = user_lang(update, context)
    question = (update.message.text or "").strip()
    if not question:
        return

    history: list[dict[str, str]] = context.user_data.setdefault("history", [])
    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id))
    try:
        answer = await core.answer_async(question, history, lang)
    except core.AssistantError as exc:
        await update.message.reply_text(exc.localized(lang))
        return
    except Exception as exc:  # noqa: BLE001 - the student still gets an answer
        print(f"[bot] unexpected error: {exc!r}")
        await update.message.reply_text(core.msg("unexpected", lang))
        return
    finally:
        typing.cancel()

    history.extend(
        [{"role": "user", "text": question}, {"role": "assistant", "text": answer}]
    )
    del history[: -core.HISTORY_TURNS]  # keep only the most recent turns
    await reply(update.message, answer)


async def on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    await update.message.reply_text(text("not_text", lang))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("[bot] handler error:")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)


# ---------------------------------------------------------- application ----


async def _post_init(app: Application) -> None:
    """English default menu; explicit language choices get a private-chat menu."""
    default = [BotCommand(name, label) for name, label in COMMAND_LABELS["en"]]
    await app.bot.set_my_commands(default)
    for lang in ("kk", "ru"):
        # Remove old locale-based menus so Telegram locale does not override English.
        await app.bot.delete_my_commands(language_code=lang)

    me = await app.bot.get_me()
    print(f"[bot] connected as @{me.username}")
    if not core.knowledge_files():
        print("[bot] WARNING: knowledge/ is empty - the bot has nothing to answer from.")


def build_application() -> Application:
    """Build the bot. Used by both bot.py (polling) and main.py (webhook)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            ".env файлында TELEGRAM_BOT_TOKEN болуы керек. "
            "Токенді Telegram-дағы @BotFather-ден алыңыз (/newbot)."
        )

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(False)
        .persistence(PicklePersistence(filepath=str(STATE_FILE)))
        .post_init(_post_init)
        .build()
    )
    from accounts_bot import register_accounts
    from events_bot import register_events
    from rooms_bot import register_rooms

    register_accounts(app)
    register_events(app)
    register_rooms(app)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("guide", cmd_guide))
    app.add_handler(CallbackQueryHandler(on_lang_chosen, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=MENU_PATTERN))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_question))
    app.add_handler(
        MessageHandler(
            ~filters.TEXT & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_unsupported
        )
    )
    app.add_error_handler(on_error)
    return app
