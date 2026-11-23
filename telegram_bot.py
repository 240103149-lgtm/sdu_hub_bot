"""Telegram bot for the University AI Knowledge Hub.

Covers the same user stories as the website, using the shared `core` module:

    US5  free-text questions answered from the approved knowledge base,
         with a fallback answer, a connection-error warning and a timing log
    US3  /guide - the step-by-step course registration guide
    US6  Kazakh / Russian / English interface

Every text message goes to the AI assistant (assistant_bot.py), which can also
use the commands' features itself - so "What deadlines do I have?" works
without typing /deadline. The commands keep working as before.

This module only builds the Application. Run it with `python bot.py`
(long polling) or let main.py serve it over a webhook.
"""
from __future__ import annotations

import asyncio
import html
import io
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
    KeyboardButton,
    ReplyKeyboardMarkup,
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
    TypeHandler,
    filters,
)

import core
import i18n

# Telegram rejects messages longer than 4096; we leave a little headroom.
TELEGRAM_LIMIT = 4000
TYPING_REFRESH = 4  # seconds; Telegram's "typing…" indicator lasts about 5

# US2: "provide the university registration portal link". Course registration
# happens on my.sdu.edu.kz (see the guide), so that is the default.
PORTAL_URL = os.getenv("REGISTRATION_PORTAL_URL", "").strip() or "https://my.sdu.edu.kz/"

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
if os.getenv("BOT_STATE_PATH", "").strip():
    STATE_FILE = Path(os.environ["BOT_STATE_PATH"]).expanduser()

LANG_BUTTONS = [("kk", "🇰🇿 Қазақша"), ("ru", "🇷🇺 Русский"), ("en", "🇬🇧 English")]

# ------------------------------------------------------------ bot texts ----

# All texts are in locales/<lang>.json (US6); only the order of menus is set here.
BOT: dict[str, dict[str, str]] = i18n.by_lang("bot")

# The Telegram "/" menu exposes the five everyday commands. Every feature is reached by
# asking in plain words (the AI assistant) or from the /start buttons, so a
# new feature adds an assistant tool and maybe a button, not a command.
# The older commands (/grade, /deadline, /rooms, /events, /documents,
# /guide, /login, /reset, /unlink, /unlink_moodle, /cancel) keep working
# for students who type them; they are just not listed.
COMMAND_ORDER = ("start", "account", "notify", "lang", "help")
COMMAND_LABELS: dict[str, list[tuple[str, str]]] = {
    lang: [(name, labels[name]) for name in COMMAND_ORDER]
    for lang, labels in i18n.by_lang("commands").items()
}
# Bump when COMMAND_ORDER changes. A chat menu saved by Telegram (set when the
# student picked a language) overrides the default menu, so each private chat
# gets its menu set again once, on its next update (refresh_chat_menu).
MENU_VERSION = 3
REVIEW_LABEL: dict[str, str] = {lang: labels["review"] for lang, labels in i18n.by_lang("commands").items()}


def text(key: str, lang: str) -> str:
    return BOT[core.normalize_lang(lang)][key]


def command_menu(lang: str, user_id: int | None = None) -> list[BotCommand]:
    """The "/" menu for one chat: the five commands, plus /review for the
    admissions staff (DOCUMENTS_REVIEWER_IDS)."""
    from documents_bot import reviewer_ids

    lang = core.normalize_lang(lang)
    commands = [BotCommand(name, label) for name, label in COMMAND_LABELS[lang]]
    if user_id is not None and user_id in reviewer_ids():
        commands.insert(-1, BotCommand("review", REVIEW_LABEL[lang]))
    return commands


# --------------------------------------------------------- text helpers ----


def tg_len(source: str) -> int:
    """Telegram measures messages in UTF-16 code units, so an emoji counts 2."""
    return len(source.encode("utf-16-le")) // 2


MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")        # Add \- Drop -> Add - Drop
MD_LINK = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")   # [text](https://...)
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")  # | :--- | :--- |


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip()]


def tables_to_lines(source: str) -> str:
    """Markdown tables, which Telegram cannot show, as lines it can:
    the header row in bold, then one bullet per row - "• a — b — c"."""
    lines, out, i = source.split("\n"), [], 0
    while i < len(lines):
        following = lines[i + 1] if i + 1 < len(lines) else ""
        if lines[i].lstrip().startswith("|") and "|" in following and TABLE_RULE.match(following):
            header = [cell.replace("**", "") for cell in _cells(lines[i])]
            out.append(f"**{' · '.join(header)}**")
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if _cells(lines[i]):
                    out.append("• " + " — ".join(_cells(lines[i])))
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def render_line(source: str) -> str | None:
    """One plain-text line as self-contained Telegram HTML, or None to drop it.

    The text may contain **bold**, [links](https://...), `- ` bullets, `# `
    headings and Markdown escapes such as `\\-`. Escaping happens before the
    tags are made, so nothing in the content can be mistaken for a tag - and
    because no tag spans a line break, splitting a long answer at line
    boundaries can never leave a tag unclosed.
    """
    line = source.rstrip()
    if line.lstrip().startswith("<!--"):  # markdown comments in the guide
        return None
    heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
    if heading:  # "# **Overview**" -> one bold heading, not "**Overview**" in bold
        line = f"**{heading.group(1).strip().replace('**', '')}**"
    elif re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):  # --- horizontal rule
        line = "──────────"
    else:
        line = re.sub(r"^(\s*)[*+-]\s+", r"\1• ", line)
    escaped = html.escape(MD_ESCAPE.sub(r"\1", line))
    escaped = MD_LINK.sub(r'<a href="\2">\1</a>', escaped)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)


def plain_to_telegram(source: str) -> str:
    """Render a whole plain-text answer or guide as Telegram HTML."""
    rendered = (render_line(line) for line in tables_to_lines(source).split("\n"))
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

    for raw in tables_to_lines(source).split("\n"):
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
    return html.unescape(re.sub(r'</?b>|<a href="[^"]*">|</a>', "", rendered))


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


HOME_ACTIONS = ("deadline", "grade", "events", "rooms", "guide", "documents", "lang")
HOME_BUTTONS = {
    lang: [(action, labels[action]) for action in HOME_ACTIONS]
    for lang, labels in i18n.by_lang("home").items()
}
# grade, deadline and documents are entry points of the sign-in conversation
# in accounts_bot, so their buttons are handled there, not by on_menu.
MENU_PATTERN = r"^menu:(start|help|events|rooms|guide|lang|reset)$"


def home_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Main actions, two per row (language last). Callback data is language-independent."""
    buttons = [InlineKeyboardButton(label, callback_data=f"menu:{action}")
               for action, label in HOME_BUTTONS[core.normalize_lang(lang)]]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


# The keyboard under the message field in private chats. A tap sends the
# label as a message; the handlers below (and accounts_bot for the actions
# that may need a sign-in) open the feature without the AI.
KEYBOARD_ACTIONS = ("account", "calendar", "rooms", "events", "canteen", "guide", "documents")
# Grades and deadlines are buttons inside 👤 Account now. Their labels still
# work when typed or tapped on an older keyboard.
OLD_KEYBOARD_ACTIONS = ("grade", "deadline")
KEYBOARD_LABELS: dict[str, dict[str, str]] = i18n.by_lang("keyboard")
# Bump when KEYBOARD_ACTIONS or the labels change: chats then get the new
# keyboard with their next answer.
KEYBOARD_VERSION = 3


def reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    labels = KEYBOARD_LABELS[core.normalize_lang(lang)]
    buttons = [KeyboardButton(labels[action]) for action in KEYBOARD_ACTIONS]
    return ReplyKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)],
                               resize_keyboard=True, is_persistent=True)


def keyboard_labels(*actions: str) -> list[str]:
    """The labels of these keyboard actions, in every language."""
    return [KEYBOARD_LABELS[lang][action] for lang in core.LANGS for action in actions]


def keyboard_action(label: str) -> str | None:
    for lang in core.LANGS:
        for action in KEYBOARD_ACTIONS:
            if KEYBOARD_LABELS[lang][action] == (label or "").strip():
                return action
    return None


def keyboard_key(lang: str) -> str:
    """What user_data["keyboard"] holds once this chat has the current keyboard."""
    return f"{KEYBOARD_VERSION}:{core.normalize_lang(lang)}"


def help_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The main menu plus "New chat", which replaces the old /reset."""
    rows = list(home_keyboard(lang).inline_keyboard)
    rows.append([InlineKeyboardButton(text("btn_new_chat", lang), callback_data="menu:reset")])
    return InlineKeyboardMarkup(rows)


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"lang:{code}") for code, label in LANG_BUTTONS]]
    )


def portal_keyboard(lang: str) -> InlineKeyboardMarkup | None:
    if not PORTAL_URL:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(text("portal", lang), url=PORTAL_URL)]])


def guide_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Under the short guide: the full guide as a file, and the portal."""
    rows = [[InlineKeyboardButton(text("btn_guide_file", lang), callback_data="guide:file")]]
    if PORTAL_URL:
        rows.append([InlineKeyboardButton(text("portal", lang), url=PORTAL_URL)])
    return InlineKeyboardMarkup(rows)


_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _plain(line: str) -> str:
    """Markdown inline marks out: **bold**, \\- escapes, [text](link) -> text (link)."""
    line = _LINK.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", line)
    return re.sub(r"\\(.)", r"\1", line.replace("**", "")).strip()


def guide_docx(source: str, title: str) -> bytes:
    """The registration guide (Markdown) as a Word file to open on any phone."""
    from docx import Document

    doc = Document()
    titled = False
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        if not table_rows:
            return
        table = doc.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        table.style = "Table Grid"
        for r, row in enumerate(table_rows):
            for c, cell in enumerate(row[: len(table_rows[0])]):
                table.cell(r, c).text = cell
                if r == 0:
                    for run in table.cell(r, c).paragraphs[0].runs:
                        run.bold = True
        table_rows.clear()

    for raw in source.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [_plain(cell) for cell in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells if cell):
                table_rows.append(cells)
            continue
        flush_table()
        if not line.strip():
            continue
        heading = re.match(r"^(#+)\s*(.*)", line)
        bullet = re.match(r"^\s*[*-]\s+(.*)", line)
        number = re.match(r"^\s*(\d+)\.\s+(.*)", line)
        if heading and not titled:
            doc.add_heading(title, level=0)  # the file's first heading: its title
            titled = True
        elif heading:
            doc.add_heading(_plain(heading.group(2)), level=min(len(heading.group(1)), 3))
        elif bullet:
            doc.add_paragraph(_plain(bullet.group(1)), style="List Bullet")
        elif number:
            # The number as written: Word's "List Number" would continue across sections.
            doc.add_paragraph(f"{number.group(1)}. {_plain(number.group(2))}")
        else:
            doc.add_paragraph(_plain(line))
    flush_table()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def retry_keyboard(callback_data: str, lang: str) -> InlineKeyboardMarkup:
    """A single "🔄 Retry" button, shown after a temporary loading failure."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(text("btn_retry", lang), callback_data=callback_data)]])


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


def welcome_text(update: Update, lang: str) -> str:
    user = update.effective_user
    name = (user.first_name if user and user.first_name else "").strip() or text("default_name", lang)
    return text("start", lang).format(name=name)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Private chat: the welcome with the keyboard under the message field.
    A group: the welcome with the inline main menu (no keyboard for everyone)."""
    lang = user_lang(update, context)
    if update.effective_chat.type == "private":
        context.user_data.pop("reminders_blocked", None)
        arg = (context.args or [""])[0]
        if re.fullmatch(r"section_[a-f0-9]{16}", arg):
            from notifications_bot import show_schedule
            # Legacy section links now open the authenticated timetable.
            await show_schedule(update, context)
            return
        if arg == "reminders":
            from notifications_bot import cmd_notifications

            await cmd_notifications(update, context)
            return
        context.user_data["keyboard"] = keyboard_key(lang)
        await reply(update.effective_message, welcome_text(update, lang), reply_markup=reply_keyboard(lang))
        return
    await reply(update.effective_message, welcome_text(update, lang), reply_markup=home_keyboard(lang))


async def on_keyboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A keyboard tap that needs no sign-in (grades, deadlines and documents
    are entry points of the sign-in conversation in accounts_bot)."""
    from accounts_bot import cmd_account
    from events_bot import cmd_events
    from rooms_bot import cmd_rooms

    from assistant_bot import answer_question

    action = keyboard_action(update.effective_message.text)
    if action == "calendar":
        # The dates are answered in the student's language from the academic
        # calendar (and the portal's System Calendar card when signed in).
        lang = user_lang(update, context)
        await answer_question(update, context, question=KEYBOARD_LABELS[lang]["calendar_question"])
        return
    if action == "canteen":
        import canteen

        for card in canteen.render_places(user_lang(update, context)):  # one message per counter
            await reply(update.effective_message, card)
        return
    handlers = {"rooms": cmd_rooms, "events": cmd_events, "guide": cmd_guide, "account": cmd_account}
    if action in handlers:
        context.args = []
        await handlers[action](update, context)


async def edit_or_reply(update: Update, source: str, reply_markup: Any = None) -> None:
    """Show `source` in place of the message whose button was pressed.

    Used for language switching, so the chat keeps one menu in the current
    language instead of piling up copies in the old one. Falls back to a new
    message when there is nothing to edit (a typed command), the text needs
    more than one message, or Telegram refuses the edit.
    """
    query = update.callback_query
    chunks = render_chunks(source)
    if query is not None and query.message is not None and len(chunks) == 1:
        try:
            await query.edit_message_text(
                chunks[0],
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            return
        except TelegramError as exc:
            print(f"[bot] could not edit the menu message ({exc}); sending a new one")
    await reply(update.effective_message, source, reply_markup=reply_markup)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    await reply(update.effective_message, text("help", lang), reply_markup=help_keyboard(lang))


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lang or the 🌐 button. From the button, the menu itself turns into the picker."""
    lang = user_lang(update, context)
    await edit_or_reply(update, text("choose_lang", lang), reply_markup=language_keyboard())


async def set_chat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str) -> None:
    """Give this private chat the current "/" menu in `lang`."""
    try:
        await context.bot.set_my_commands(
            command_menu(lang, update.effective_user.id),
            scope=BotCommandScopeChat(chat_id=update.effective_chat.id),
        )
        context.user_data["menu_version"] = MENU_VERSION
    except TelegramError as exc:
        print(f"[bot] could not update the chat command menu: {exc}")


async def refresh_chat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler: replace a chat menu from an older version
    (for example the 13 commands before the four-command menu). Once per chat."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type != "private" or context.user_data is None:
        return
    if context.user_data.get("menu_version") == MENU_VERSION:
        return
    await set_chat_menu(update, context, user_lang(update, context))


async def on_lang_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chosen = core.normalize_lang(query.data.split(":", 1)[-1])
    context.user_data["lang"] = chosen
    # "Language switched" is a short pop-up, not one more message in the chat.
    await query.answer(text("lang_set", chosen))
    if update.effective_chat.type == "private":
        await set_chat_menu(update, context, chosen)
        # The keyboard under the message field can only come with a new message:
        # the picker turns into a one-line confirmation, then the welcome follows.
        await edit_or_reply(update, f"✅ {text('lang_set', chosen)}")
        context.user_data["keyboard"] = keyboard_key(chosen)
        await reply(update.effective_message, welcome_text(update, chosen), reply_markup=reply_keyboard(chosen))
        return
    # A group: the picker becomes the home menu in the new language.
    await edit_or_reply(update, welcome_text(update, chosen), reply_markup=home_keyboard(chosen))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    context.user_data["history"] = []
    await update.effective_message.reply_text(text("reset", lang))


async def cmd_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """US2: show the approved step-by-step registration guide.

    Not published -> "Guide not available for this semester" (never another
    semester's guide); a temporary read failure -> a message with Retry.
    """
    lang = user_lang(update, context)
    try:
        guide = core.load_guide(lang)
    except core.GuideError as exc:
        print(f"[bot] registration guide: {exc}")
        await update.effective_message.reply_text(
            core.msg("guide_error", lang), reply_markup=retry_keyboard("guide:retry", lang)
        )
        return
    if not guide or not guide.strip():
        await update.effective_message.reply_text(core.msg("guide_missing", lang))
        return
    # A short summary in the student's language; the full guide is a file.
    await reply(update.effective_message, text("guide_short", lang), reply_markup=guide_keyboard(lang))


async def on_guide_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[📄 Full guide]: the whole guide as a Word file (as text if that fails)."""
    query = update.callback_query
    await query.answer()
    lang = user_lang(update, context)
    try:
        guide = core.load_guide(lang)
    except core.GuideError as exc:
        print(f"[bot] registration guide: {exc}")
        guide = None
    if not guide or not guide.strip():
        await update.effective_message.reply_text(core.msg("guide_error", lang),
                                                  reply_markup=retry_keyboard("guide:retry", lang))
        return
    try:
        data = guide_docx(guide, text("guide_file_title", lang))
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=io.BytesIO(data),
            filename="SDU_course_registration_guide.docx",
            caption=text("guide_file_caption", lang),
        )
    except Exception as exc:  # noqa: BLE001 - the student still gets the guide
        print(f"[bot] could not send the guide file ({exc!r}); sending it as text")
        await reply(update.effective_message, guide.strip(), reply_markup=portal_keyboard(lang))


async def on_guide_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await cmd_guide(update, context)


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
        "reset": cmd_reset,
    }
    # A button should open rooms for now even if an earlier command had args.
    context.args = []
    await handlers[action](update, context)


async def on_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """US1 and every service in plain words: the AI assistant answers from the
    knowledge base or opens the student's grades, deadlines, free rooms, events,
    the guide or the documents itself (assistant_bot.py)."""
    from assistant_bot import answer_question

    await answer_question(update, context)


async def on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = user_lang(update, context)
    await update.message.reply_text(text("not_text", lang))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("[bot] handler error:")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)


# ---------------------------------------------------------- application ----


async def _post_init(app: Application) -> None:
    """English default menu; explicit language choices get a private-chat menu."""
    import notifications

    notifications.install(app)
    await app.bot.set_my_commands(command_menu("en"))
    from documents_bot import reviewer_ids

    for reviewer in sorted(reviewer_ids()):
        # Staff also see /review. A reviewer who never opened the bot cannot
        # get a chat menu yet; the command still works when typed.
        try:
            await app.bot.set_my_commands(
                command_menu("en", reviewer), scope=BotCommandScopeChat(chat_id=reviewer)
            )
        except TelegramError as exc:
            print(f"[bot] could not set the /review menu for {reviewer}: {exc}")
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

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(False)
        .persistence(PicklePersistence(filepath=str(STATE_FILE)))
        .post_init(_post_init)
        .build()
    )
    from accounts_bot import register_accounts
    from assistant_bot import register_assistant
    from documents_bot import register_documents
    from events_bot import register_events
    from rooms_bot import register_rooms
    from notifications_bot import register as register_notifications

    app.add_handler(TypeHandler(Update, refresh_chat_menu), group=-2)
    register_accounts(app)
    register_events(app)
    register_rooms(app)
    register_documents(app)
    register_assistant(app)
    register_notifications(app)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("guide", cmd_guide))
    app.add_handler(CallbackQueryHandler(on_guide_retry, pattern=r"^guide:retry$"))
    app.add_handler(CallbackQueryHandler(on_guide_file, pattern=r"^guide:file$"))
    app.add_handler(CallbackQueryHandler(on_lang_chosen, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=MENU_PATTERN))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text(keyboard_labels("calendar", "rooms", "events", "canteen", "guide", "account")),
        on_keyboard_button,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_question))
    app.add_handler(
        MessageHandler(
            ~filters.TEXT & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_unsupported
        )
    )
    app.add_error_handler(on_error)
    return app
