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
import traceback
from typing import Any

from telegram import (
    BotCommand,
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
STATE_FILE = core.BASE_DIR / "bot_state.pickle"

LANG_BUTTONS = [("kk", "🇰🇿 Қазақша"), ("ru", "🇷🇺 Русский"), ("en", "🇬🇧 English")]

# ------------------------------------------------------------ bot texts ----

BOT: dict[str, dict[str, str]] = {
    "kk": {
        "start": (
            "Сәлем, {name}! 👋\n\n"
            "Мен — университеттің AI көмекшісімін. Оқу процесі туралы сұрағыңызды "
            "жай ғана мәтінмен жазыңыз — жауапты университеттің бекітілген "
            "құжаттарынан ғана іздеймін.\n\n"
            "Мысалы: «Расписание қалай құрамын?»"
        ),
        "help": (
            "<b>Командалар</b>\n"
            "/guide — расписание құру нұсқаулығы\n"
            "/lang — тілді ауыстыру\n"
            "/reset — әңгіме тарихын тазарту\n"
            "/help — осы анықтама\n\n"
            "Сұрағыңызды жай ғана жазып жіберсеңіз болды."
        ),
        "choose_lang": "Тілді таңдаңыз:",
        "lang_set": "Тіл қазақшаға ауыстырылды. Сұрағыңызды жазыңыз.",
        "reset": "Әңгіме тарихы тазартылды. Жаңа сұрақ қоя аласыз.",
        "portal": "🔗 Тіркеу порталын ашу",
        "not_text": "Мен әзірге тек мәтінді түсінемін. Сұрағыңызды жазып жіберіңізші.",
    },
    "ru": {
        "start": (
            "Привет, {name}! 👋\n\n"
            "Я — AI-помощник университета. Задайте вопрос об учебном процессе "
            "обычным текстом — я ищу ответ только в утверждённых документах "
            "университета.\n\n"
            "Например: «Как составить расписание?»"
        ),
        "help": (
            "<b>Команды</b>\n"
            "/guide — инструкция по составлению расписания\n"
            "/lang — сменить язык\n"
            "/reset — очистить историю диалога\n"
            "/help — эта справка\n\n"
            "Чтобы задать вопрос, просто напишите его."
        ),
        "choose_lang": "Выберите язык:",
        "lang_set": "Язык переключён на русский. Задавайте вопрос.",
        "reset": "История диалога очищена. Можете задать новый вопрос.",
        "portal": "🔗 Открыть портал регистрации",
        "not_text": "Пока я понимаю только текст. Напишите, пожалуйста, ваш вопрос.",
    },
    "en": {
        "start": (
            "Hi, {name}! 👋\n\n"
            "I'm the university's AI assistant. Ask about anything in the study "
            "process in plain text — I only look for answers in the university's "
            "approved documents.\n\n"
            "For example: \"How do I create my schedule?\""
        ),
        "help": (
            "<b>Commands</b>\n"
            "/guide — course registration guide\n"
            "/lang — change language\n"
            "/reset — clear the conversation history\n"
            "/help — this help\n\n"
            "To ask a question, just type it."
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


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"lang:{code}") for code, label in LANG_BUTTONS]]
    )


def portal_keyboard(lang: str) -> InlineKeyboardMarkup | None:
    if not PORTAL_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(text("portal", lang), url=PORTAL_URL)]])


def user_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """US6: the student's chosen language, defaulting to their Telegram locale."""
    stored = context.user_data.get("lang") if context.user_data is not None else None
    if stored in core.LANGS:
        return stored
    user = update.effective_user
    lang = core.normalize_lang(user.language_code if user else None)
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
        update.message,
        text("start", lang).format(name=name),
        reply_markup=language_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    # The help text is already HTML, so it goes out directly.
    await update.message.reply_text(text("help", lang), parse_mode=ParseMode.HTML)


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    await update.message.reply_text(text("choose_lang", lang), reply_markup=language_keyboard())


async def on_lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chosen = core.normalize_lang(query.data.split(":", 1)[-1])
    context.user_data["lang"] = chosen
    try:
        await query.edit_message_text(text("lang_set", chosen))
    except TelegramError:
        await query.message.reply_text(text("lang_set", chosen))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    context.user_data["history"] = []
    await update.message.reply_text(text("reset", lang))


async def cmd_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """US3: show the approved step-by-step registration guide."""
    lang = user_lang(update, context)
    guide = core.load_guide(lang)
    if not guide or not guide.strip():
        await update.message.reply_text(core.msg("guide_missing", lang))
        return
    await reply(update.message, guide.strip(), reply_markup=portal_keyboard(lang))


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
    """Register the command menu, in all three languages (US6)."""
    default = [BotCommand(name, label) for name, label in COMMAND_LABELS["en"]]
    await app.bot.set_my_commands(default)
    for lang in ("kk", "ru"):
        commands = [BotCommand(name, label) for name, label in COMMAND_LABELS[lang]]
        await app.bot.set_my_commands(commands, language_code=lang)

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
        .persistence(PicklePersistence(filepath=str(STATE_FILE)))
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("guide", cmd_guide))
    app.add_handler(CallbackQueryHandler(on_lang_chosen, pattern=r"^lang:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_question))
    app.add_handler(
        MessageHandler(
            ~filters.TEXT & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_unsupported
        )
    )
    app.add_error_handler(on_error)
    return app
