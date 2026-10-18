"""/documents - US8 Document Submission for new students.

    /documents  or the 📄 menu button (private chat only)
      |- portal not linked ...... sign in first (the shared portal sign-in in
      |                           accounts_bot), then the checklist opens
      |- no track yet ........... [🎓 State grant] [💳 Tuition-based]
      '- checklist .............. a button per document, [📤 Submit]
            tap a document, send the file -> format and size are checked
            Submit -> "Please upload the missing document: ..." (blocked)
                   or "Submission successful" -> admissions staff notified

Admissions staff (DOCUMENTS_REVIEWER_IDS) receive each submitted file with
[✅ Verify] [❌ Reject]; the student is told about every decision right away.
/review sends staff the files that are still waiting.

The rules, statuses and the database live in documents.py.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import core
import documents
import i18n
import grade_bot
from documents import MISSING, REJECTED, UNDER_REVIEW, UPLOADED, VERIFIED
from rooms import CAMPUS_TZ
from telegram_bot import add_command, on_unsupported, render_chunks, user_lang

UPLOAD_TTL = 600     # seconds a chosen document waits for its file
REVIEW_BATCH = 30    # files /review sends at once

ICONS = {MISSING: "⬜", UPLOADED: "📎", UNDER_REVIEW: "⏳", VERIFIED: "✅", REJECTED: "❌"}
TRACK_ICONS = {"grant": "🎓", "paid": "💳"}

# ------------------------------------------------------------------ texts ----

T: dict[str, dict[str, str]] = i18n.by_key("documents")  # locales/<lang>.json, section "documents"

MENU = T["menu"]
HELP_LINE = T["help_line"]


def t(key: str, lang: str) -> str:
    return T[key].get(core.normalize_lang(lang), T[key]["en"])


# ---------------------------------------------------------------- helpers ----


def reviewer_ids() -> set[int]:
    """Telegram user IDs of the admissions staff (DOCUMENTS_REVIEWER_IDS)."""
    ids: set[int] = set()
    for part in os.getenv("DOCUMENTS_REVIEWER_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def _student_id(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """The student number confirmed by the portal sign-in (US7), if any."""
    portal = (context.user_data or {}).get("portal") or {}
    value = str(portal.get("student_id") or "").strip()
    return value or None


def _lang_of(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    """The language another user (a student or a reviewer) chose in the bot."""
    data = context.application.user_data.get(user_id) or {}
    return core.normalize_lang(data.get("lang"))


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(CAMPUS_TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


SIZE_UNITS = {lang: tuple(units) for lang, units in T["size_units"].items()}


def _mb(size: int | float) -> str:
    """Megabytes without a needless '.0': 10485760 -> '10', 1300000 -> '1.2'."""
    return f"{size / (1024 * 1024):.1f}".removesuffix(".0")


def _size(size: int, lang: str) -> str:
    kb, mb = SIZE_UNITS[core.normalize_lang(lang)]
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} {kb}"
    return f"{_mb(size)} {mb}"


def _formats(formats) -> str:
    return ", ".join(fmt.upper() for fmt in formats)


def _file_label(name: str | None, size: int | None, kind: str, lang: str) -> str:
    """'scan.pdf, 240 KB' - asterisks removed so a file name can't turn bold."""
    label = (name or ("photo.jpg" if kind == "photo" else "file")).replace("*", "")
    if len(label) > 60:
        label = label[:57] + "…"
    return f"{label}, {_size(size, lang)}" if size else label


async def _send(bot, chat_id: int, source: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Plain text with **bold** as Telegram HTML; long texts are split."""
    chunks = render_chunks(source)
    for index, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup if index == len(chunks) - 1 else None,
        )


async def _edit(update: Update, source: str, markup: InlineKeyboardMarkup | None) -> None:
    """Redraw the message under a tapped button; send a new one if that fails."""
    chunks = render_chunks(source)
    if len(chunks) == 1:
        try:
            await update.callback_query.edit_message_text(
                chunks[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
    await _send(update.get_bot(), update.effective_chat.id, source, markup)


def guarded(handler):
    """A missing checklist file or a database failure becomes a polite message."""

    @wraps(handler)
    async def run(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await handler(update, context)
        except documents.CatalogError as exc:
            key, detail = "catalog_missing", exc
        except sqlite3.Error as exc:
            key, detail = "storage_error", repr(exc)
        print(f"[documents] {detail}")
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except TelegramError:
                pass
        if update.effective_chat:
            await update.get_bot().send_message(
                chat_id=update.effective_chat.id, text=t(key, user_lang(update, context))
            )
        return ConversationHandler.END

    return run


# -------------------------------------------------------------- rendering ----


def _doc_line(doc: documents.DocType, stored: documents.StoredDocument | None, lang: str) -> str:
    status = stored.status if stored else MISSING
    if status == MISSING:
        state = doc.condition(lang) if not doc.required and doc.when else t("doc_missing", lang)
    elif status == REJECTED:
        state = t("doc_rejected", lang).format(reason=t(f"reason_{stored.reason or 'invalid'}", lang))
    else:
        state = t(f"doc_{status}", lang)
    return f"{ICONS.get(status, '•')} {doc.label(lang)} — {state}"


def checklist_text(catalog: documents.Catalog, submission: documents.Submission, lang: str) -> str:
    lang = core.normalize_lang(lang)
    track = submission.track
    lines = [
        f"📄 **{t('title', lang)}** · {TRACK_ICONS.get(track, '')} {catalog.track_name(track, lang)}",
        t("student", lang).format(id=submission.student_id),
        "",
        t(f"status_{submission.status}", lang).format(since=_local_time(submission.submitted_at)),
        "",
        f"**{t('required', lang)}**",
    ]
    docs = catalog.for_track(track)
    lines += [_doc_line(doc, submission.documents.get(doc.id), lang) for doc in docs if doc.required]
    optional = [doc for doc in docs if not doc.required]
    if optional:
        lines += ["", f"**{t('optional', lang)}**"]
        lines += [_doc_line(doc, submission.documents.get(doc.id), lang) for doc in optional]
    all_formats = list(dict.fromkeys(fmt for doc in docs for fmt in doc.formats))
    checked = catalog.checked
    try:
        checked = datetime.strptime(catalog.checked, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        pass
    lines += [
        "",
        t("rules", lang).format(formats=_formats(all_formats), mb=_mb(catalog.max_bytes)),
        t("source", lang).format(url=catalog.source, date=checked),
    ]
    return "\n".join(lines)


def checklist_keyboard(catalog: documents.Catalog, submission: documents.Submission, lang: str) -> InlineKeyboardMarkup:
    """A button for every document that can still be uploaded or replaced."""
    buttons = []
    for doc in catalog.for_track(submission.track):
        stored = submission.documents.get(doc.id)
        status = stored.status if stored else MISSING
        if status == VERIFIED:
            continue
        label = f"{ICONS.get(status, '•')} {doc.label(lang, short=True)}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"docs:up:{doc.id}"))
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    refresh = InlineKeyboardButton(t("btn_refresh", lang), callback_data="docs:refresh")
    if submission.status == VERIFIED:
        rows.append([refresh])
    else:
        rows.append([InlineKeyboardButton(t("btn_submit", lang), callback_data="docs:submit"), refresh])
        rows.append([InlineKeyboardButton(t("btn_track", lang), callback_data="docs:tracks")])
    return InlineKeyboardMarkup(rows)


def track_keyboard(catalog: documents.Catalog, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"{TRACK_ICONS.get(track, '')} {catalog.track_name(track, lang)}".strip(),
            callback_data=f"docs:track:{track}",
        )
        for track in catalog.tracks
    ]])


def _board(student_id: str, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """The checklist (or the track question) for a signed-in student."""
    catalog = documents.load_catalog()
    submission = documents.store().get(student_id)
    if submission is None or submission.track not in catalog.tracks:
        return t("choose_track", lang), track_keyboard(catalog, lang)
    return checklist_text(catalog, submission, lang), checklist_keyboard(catalog, submission, lang)


# --------------------------------------------------------- student flow ----


@guarded
async def cmd_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/documents and the 📄 menu button. An entry point of the shared sign-in
    conversation in accounts_bot: without a linked portal it asks to sign in."""
    lang = user_lang(update, context)
    student_id = _student_id(context)
    if student_id is None:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(t("btn_signin", lang), callback_data="docs:signin"),
            InlineKeyboardButton(t("btn_cancel", lang), callback_data="docs:nosignin"),
        ]])
        await update.effective_message.reply_text(t("need_link", lang), reply_markup=keyboard)
        return grade_bot.CHOOSE
    text, markup = _board(student_id, lang)
    await _send(context.bot, update.effective_chat.id, text, markup)
    return ConversationHandler.END


async def on_signin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """[🔐 Sign in] continues with the portal sign-in steps of grade_bot."""
    lang = user_lang(update, context)
    query = update.callback_query
    await query.answer()
    if query.data == "docs:nosignin":
        await query.edit_message_text(t("cancelled", lang))
        return ConversationHandler.END
    await query.edit_message_text(grade_bot.t("ask_id", lang))
    return grade_bot.ASK_ID


@guarded
async def show_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the checklist in a new message (used right after the sign-in)."""
    student_id = _student_id(context)
    if student_id is None:
        return
    text, markup = _board(student_id, user_lang(update, context))
    await _send(context.bot, update.effective_chat.id, text, markup)


@guarded
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """docs:track:<id> · docs:tracks · docs:up:<doc> · docs:stop · docs:submit · docs:refresh"""
    query = update.callback_query
    lang = user_lang(update, context)
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private":
        await query.answer(t("private_only", lang), show_alert=True)
        return
    student_id = _student_id(context)
    action, _, arg = query.data.removeprefix("docs:").partition(":")
    if student_id is None or action in ("signin", "nosignin"):  # signed out, or an old button
        await query.answer()
        await _send(context.bot, chat_id, t("start_again", lang))
        return

    catalog = documents.load_catalog()
    db = documents.store()
    if action == "stop":
        context.user_data.pop("doc_pending", None)
        await query.answer()
        await _edit(update, t("upload_cancelled", lang), None)
        return
    if action == "track" and arg in catalog.tracks:
        db.start(catalog, student_id, chat_id, arg)
    if action == "tracks":
        await query.answer()
        await _edit(update, t("choose_track", lang), track_keyboard(catalog, lang))
        return
    if action == "up":
        await _ask_for_file(update, context, catalog, student_id, arg, lang)
        return
    if action == "submit":
        await _submit(update, context, catalog, student_id, lang)
        return
    await query.answer()
    await _edit(update, *_board(student_id, lang))


async def _ask_for_file(update, context, catalog, student_id: str, doc_id: str, lang: str) -> None:
    query = update.callback_query
    submission = documents.store().get(student_id)
    doc = catalog.get(doc_id)
    if submission is None or doc is None or submission.track not in doc.tracks:
        await query.answer()
        await _send(context.bot, update.effective_chat.id, *_board(student_id, lang))
        return
    stored = submission.documents.get(doc.id)
    if stored and stored.status == VERIFIED:
        await query.answer(t("already_verified", lang).format(doc=doc.label(lang)), show_alert=True)
        return
    context.user_data["doc_pending"] = {"type": doc.id, "at": time.time()}
    await query.answer()
    cancel = InlineKeyboardMarkup([[InlineKeyboardButton(t("btn_cancel", lang), callback_data="docs:stop")]])
    text = t("ask_file", lang).format(doc=doc.label(lang), formats=_formats(doc.formats), mb=_mb(catalog.max_bytes))
    await _send(context.bot, update.effective_chat.id, text, cancel)


async def _submit(update, context, catalog, student_id: str, lang: str) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    db = documents.store()
    if db.get(student_id) is None:  # an old button: no track chosen yet
        await query.answer()
        await _send(context.bot, chat_id, *_board(student_id, lang))
        return
    result = db.submit(catalog, student_id, chat_id)
    await query.answer()
    if result.missing:
        names = ", ".join(f"«{doc.label(lang)}»" for doc in result.missing)
        upload = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⬆️ {doc.label(lang, short=True)}", callback_data=f"docs:up:{doc.id}")]
            for doc in result.missing
        ])
        await _send(context.bot, chat_id, t("missing", lang).format(docs=names), upload)
        return
    if not result.sent:
        await _send(context.bot, chat_id, t("nothing_new", lang))
        return
    submission = result.submission
    text = f"{t('submitted', lang)}\n\n{checklist_text(catalog, submission, lang)}"
    await _send(context.bot, chat_id, text, checklist_keyboard(catalog, submission, lang))
    await _notify_reviewers(context, catalog, submission, result.sent)


@guarded
async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A document or photo in the private chat: the file for the chosen document."""
    message = update.effective_message
    lang = user_lang(update, context)
    chat_id = update.effective_chat.id
    pending = context.user_data.get("doc_pending")
    if pending and time.time() - pending.get("at", 0) > UPLOAD_TTL:
        context.user_data.pop("doc_pending", None)
        pending = None

    student_id = _student_id(context)
    submission = documents.store().get(student_id) if student_id else None
    if submission is None:
        await on_unsupported(update, context)  # not using /documents: the old answer
        return
    catalog = documents.load_catalog()
    doc = catalog.get(pending["type"]) if pending else None
    if doc is None or submission.track not in doc.tracks:
        context.user_data.pop("doc_pending", None)
        text = f"{t('no_type', lang)}\n\n{checklist_text(catalog, submission, lang)}"
        await _send(context.bot, chat_id, text, checklist_keyboard(catalog, submission, lang))
        return

    if message.document:
        kind, item = "document", message.document
        file_name, mime_type = item.file_name, item.mime_type
    else:  # a photo Telegram has compressed: always JPEG
        kind, item = "photo", message.photo[-1]
        file_name, mime_type = None, "image/jpeg"
    problem = documents.check_file(doc, file_name, mime_type, item.file_size, catalog.max_bytes)
    if problem == "bad_format":
        got = documents.describe_format(file_name, mime_type)
        text = t("bad_format", lang).format(doc=doc.label(lang), got=got, formats=_formats(doc.formats))
        await _send(context.bot, chat_id, text)
        return  # the document stays chosen, so the right file can follow
    if problem == "too_large":
        text = t("too_large", lang).format(doc=doc.label(lang), size=_mb(item.file_size), mb=_mb(catalog.max_bytes))
        await _send(context.bot, chat_id, text)
        return

    submission = documents.store().save(
        catalog, student_id, chat_id, doc.id,
        kind=kind, file_id=item.file_id, file_name=file_name, mime_type=mime_type, file_size=item.file_size,
    )
    context.user_data.pop("doc_pending", None)
    saved = t("saved", lang).format(doc=doc.label(lang), file=_file_label(file_name, item.file_size, kind, lang))
    text = f"{saved}\n\n{checklist_text(catalog, submission, lang)}"
    await _send(context.bot, chat_id, text, checklist_keyboard(catalog, submission, lang))


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel while a file is awaited (runs before the sign-in /cancel)."""
    if context.user_data is None or context.user_data.pop("doc_pending", None) is None:
        return
    if context.user_data.get("login_flow"):
        return  # a sign-in is open too: its own /cancel answers
    await update.effective_message.reply_text(t("upload_cancelled", user_lang(update, context)))
    raise ApplicationHandlerStop


# ------------------------------------------------------- admissions staff ----


def _review_keyboard(doc_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t("rv_ok", lang), callback_data=f"docr:ok:{doc_id}"),
        InlineKeyboardButton(t("rv_no", lang), callback_data=f"docr:no:{doc_id}"),
    ]])


def _reasons_keyboard(doc_id: int, lang: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"❌ {t(f'reason_{reason}', lang)}", callback_data=f"docr:no:{doc_id}:{reason}")]
        for reason in documents.REJECT_REASONS
    ]
    rows.append([InlineKeyboardButton(t("rv_back", lang), callback_data=f"docr:back:{doc_id}")])
    return InlineKeyboardMarkup(rows)


async def _send_for_review(bot, chat_id: int, catalog, doc: documents.StoredDocument, track: str, lang: str) -> None:
    doc_type = catalog.get(doc.doc_type)
    caption = "\n".join([
        f"{doc.student_id} · {catalog.track_name(track, lang)}",
        doc_type.label(lang) if doc_type else doc.doc_type,
        _file_label(doc.file_name, doc.file_size, doc.kind, lang),
    ])
    markup = _review_keyboard(doc.id, lang)
    if doc.kind == "photo":
        await bot.send_photo(chat_id=chat_id, photo=doc.file_id, caption=caption, reply_markup=markup)
    else:
        await bot.send_document(chat_id=chat_id, document=doc.file_id, caption=caption, reply_markup=markup)


async def _notify_reviewers(context, catalog, submission: documents.Submission, sent) -> None:
    """Real time for staff: every submitted file arrives with its decision buttons."""
    for reviewer in sorted(reviewer_ids()):
        lang = _lang_of(context, reviewer)
        header = t("rv_new", lang).format(
            id=submission.student_id, track=catalog.track_name(submission.track, lang), n=len(sent)
        )
        try:
            await context.bot.send_message(chat_id=reviewer, text=header)
            for doc in sent:
                await _send_for_review(context.bot, reviewer, catalog, doc, submission.track, lang)
        except TelegramError as exc:
            print(f"[documents] could not notify reviewer {reviewer}: {exc}")


async def _tell_student(context, catalog, result: documents.ReviewResult) -> None:
    """Real time for the student: the decision arrives the moment it is made."""
    submission, doc = result.submission, result.document
    lang = _lang_of(context, submission.chat_id)
    doc_type = catalog.get(doc.doc_type)
    name = doc_type.label(lang) if doc_type else doc.doc_type
    markup = None
    if doc.status == VERIFIED:
        text = t("verified_one", lang).format(doc=name)
    else:
        text = t("rejected_one", lang).format(doc=name, reason=t(f"reason_{doc.reason}", lang))
        if doc_type:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                f"⬆️ {doc_type.label(lang, short=True)}", callback_data=f"docs:up:{doc.doc_type}"
            )]])
    try:
        await _send(context.bot, submission.chat_id, text, markup)
        if result.completed:
            await _send(context.bot, submission.chat_id, t("all_verified", lang))
    except TelegramError as exc:
        print(f"[documents] could not notify student {submission.student_id}: {exc}")


@guarded
async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """docr:ok:<id> · docr:no:<id> (reasons) · docr:no:<id>:<reason> · docr:back:<id>"""
    query = update.callback_query
    lang = user_lang(update, context)
    user = update.effective_user
    if user is None or user.id not in reviewer_ids():
        await query.answer(t("rv_denied", lang), show_alert=True)
        return
    parts = query.data.split(":")
    try:
        action, doc_id = parts[1], int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return
    if action in ("no", "back") and len(parts) == 3:
        await query.answer()
        keyboard = _reasons_keyboard(doc_id, lang) if action == "no" else _review_keyboard(doc_id, lang)
        await _set_buttons(query, keyboard)
        return

    verified = action == "ok"
    reason = None if verified else (parts[3] if len(parts) > 3 else "")
    if not verified and reason not in documents.REJECT_REASONS:
        await query.answer()
        return
    catalog = documents.load_catalog()
    result = documents.store().review(
        catalog, doc_id, verified=verified, reviewer=f"{user.full_name} ({user.id})", reason=reason
    )
    if result is None:
        await query.answer(t("rv_stale", lang), show_alert=True)
        await _set_buttons(query, None)
        return
    await query.answer()
    who = user.full_name or str(user.id)
    if verified:
        decision = t("rv_done_ok", lang).format(who=who)
    else:
        decision = t("rv_done_no", lang).format(reason=t(f"reason_{reason}", lang), who=who)
    try:
        caption = (query.message.caption if query.message else "") or ""
        await query.edit_message_caption(caption=f"{caption}\n\n{decision}"[:1024], reply_markup=None)
    except TelegramError as exc:
        print(f"[documents] could not mark the review message: {exc}")
    await _tell_student(context, catalog, result)


async def _set_buttons(query, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            print(f"[documents] could not change review buttons: {exc}")


@guarded
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/review (admissions staff): send again the files still waiting for a decision."""
    lang = user_lang(update, context)
    user = update.effective_user
    if user is None or user.id not in reviewer_ids():
        await update.effective_message.reply_text(t("rv_denied", lang))
        return
    catalog = documents.load_catalog()
    pending = documents.store().pending_reviews(limit=REVIEW_BATCH)
    if not pending:
        await update.effective_message.reply_text(t("rv_none", lang))
        return
    await update.effective_message.reply_text(t("rv_queue", lang).format(n=len(pending)))
    for doc, track in pending:
        await _send_for_review(context.bot, update.effective_chat.id, catalog, doc, track, lang)


# ----------------------------------------------------------- registration ----


def register_documents(app: Application) -> None:
    """/documents itself and the 📄 menu button are entry points of the shared
    sign-in conversation (accounts_bot). This adds the buttons, the file
    handler and the staff commands. Call it before the generic handlers."""
    add_command("documents", MENU, HELP_LINE)
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("cancel", on_cancel), group=-1)
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^docs:"))
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^docr:"))
    app.add_handler(CommandHandler("review", cmd_review, filters=private))
    app.add_handler(MessageHandler(private & (filters.Document.ALL | filters.PHOTO), on_file))
    if not reviewer_ids():
        print("[documents] DOCUMENTS_REVIEWER_IDS is empty: submitted documents will wait for review")
